"""Assembling the scenarios a request needs.

A scenario takes its dependencies through the constructor, so it can be built
in a test without an application behind it. This module is where the ones a
request has -- its session, the hasher and the mailer of the running service --
are put together (docs/CODING_STANDARDS.md section 7).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from barber_auth.adapters.dev_mailer import DevMailer
from barber_auth.domain.passwords import PasswordHasher
from barber_auth.domain.signing import TokenSigner
from barber_auth.services.email_confirmation import ConfirmEmail
from barber_auth.services.keys import PublishedKeys
from barber_auth.services.registration import RegisterUser
from barber_auth.services.tokens import (
    IssueTokenPair,
    RefreshTokenPair,
    RevokeSessions,
    SessionContext,
)
from barber_auth.settings import AuthSettings
from barber_common.db.session import get_session

__all__ = [
    "ConfirmEmailScenario",
    "IssueTokenPairScenario",
    "PublishedKeysScenario",
    "RefreshTokenPairScenario",
    "RegisterUserScenario",
    "RevokeSessionsScenario",
    "get_mailer",
    "get_password_hasher",
    "get_signer",
]

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def get_settings(request: Request) -> AuthSettings:
    """The settings of the running service."""
    settings = request.app.state.settings
    if not isinstance(settings, AuthSettings):  # pragma: no cover - set by create_app
        raise RuntimeError("application state has no auth settings")
    return settings


def get_password_hasher(request: Request) -> PasswordHasher:
    """The hasher built once at startup.

    Once, because the constructor of the argon2 hasher builds the hash used to
    equalise the timing of a missing user, and paying for that per request
    would double the cost of every login.
    """
    hasher = request.app.state.password_hasher
    if not isinstance(hasher, PasswordHasher):  # pragma: no cover - set by create_application
        raise RuntimeError("application state has no password hasher")
    return hasher


def get_mailer(request: Request) -> DevMailer:
    """The development mailer standing in for ``notification`` until T5.4."""
    mailer = request.app.state.mailer
    if not isinstance(mailer, DevMailer):  # pragma: no cover - set by create_application
        raise RuntimeError("application state has no mailer")
    return mailer


def get_signer(request: Request) -> TokenSigner:
    """The signing key loaded once at startup.

    Once, because parsing a PEM and deriving a thumbprint per request would be
    work repeated for a value that cannot change while the process runs. The
    private key lives in this object and nowhere else.
    """
    signer = request.app.state.signer
    if not isinstance(signer, TokenSigner):  # pragma: no cover - set by create_application
        raise RuntimeError("application state has no token signer")
    return signer


def get_session_context(request: Request) -> SessionContext:
    """Where the request came from, recorded on the session it opens.

    Both fields are informational. They are never compared against anything on
    a later request: an address changes when a phone leaves wifi, and a user
    agent changes when a browser updates.
    """
    return SessionContext(
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client is not None else None,
    )


def build_register_user(
    session: SessionDependency,
    settings: Annotated[AuthSettings, Depends(get_settings)],
    hasher: Annotated[PasswordHasher, Depends(get_password_hasher)],
    mailer: Annotated[DevMailer, Depends(get_mailer)],
) -> RegisterUser:
    """The registration scenario for this request."""
    return RegisterUser(
        session=session,
        hasher=hasher,
        mailer=mailer,
        confirmation_ttl_hours=settings.email_confirmation_ttl_hours,
        password_min_length=settings.password_min_length,
    )


def build_confirm_email(session: SessionDependency) -> ConfirmEmail:
    """The confirmation scenario for this request."""
    return ConfirmEmail(session)


def build_issue_token_pair(
    session: SessionDependency,
    settings: Annotated[AuthSettings, Depends(get_settings)],
    hasher: Annotated[PasswordHasher, Depends(get_password_hasher)],
    signer: Annotated[TokenSigner, Depends(get_signer)],
) -> IssueTokenPair:
    """The login scenario for this request."""
    return IssueTokenPair(
        session=session,
        hasher=hasher,
        signer=signer,
        issuer=settings.jwt_issuer,
        access_ttl_minutes=settings.access_token_ttl_minutes,
        refresh_ttl_days=settings.refresh_token_ttl_days,
    )


def build_refresh_token_pair(
    session: SessionDependency,
    settings: Annotated[AuthSettings, Depends(get_settings)],
    signer: Annotated[TokenSigner, Depends(get_signer)],
) -> RefreshTokenPair:
    """The rotation scenario for this request."""
    return RefreshTokenPair(
        session=session,
        signer=signer,
        issuer=settings.jwt_issuer,
        access_ttl_minutes=settings.access_token_ttl_minutes,
        refresh_ttl_days=settings.refresh_token_ttl_days,
    )


def build_revoke_sessions(session: SessionDependency) -> RevokeSessions:
    """The logout scenario for this request."""
    return RevokeSessions(session)


def build_published_keys(session: SessionDependency) -> PublishedKeys:
    """The JWKS read scenario for this request."""
    return PublishedKeys(session)


RegisterUserScenario = Annotated[RegisterUser, Depends(build_register_user)]
ConfirmEmailScenario = Annotated[ConfirmEmail, Depends(build_confirm_email)]
IssueTokenPairScenario = Annotated[IssueTokenPair, Depends(build_issue_token_pair)]
RefreshTokenPairScenario = Annotated[RefreshTokenPair, Depends(build_refresh_token_pair)]
RevokeSessionsScenario = Annotated[RevokeSessions, Depends(build_revoke_sessions)]
PublishedKeysScenario = Annotated[PublishedKeys, Depends(build_published_keys)]
RequestSessionContext = Annotated[SessionContext, Depends(get_session_context)]
