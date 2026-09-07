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
from barber_auth.services.email_confirmation import ConfirmEmail
from barber_auth.services.registration import RegisterUser
from barber_auth.settings import AuthSettings
from barber_common.db.session import get_session

__all__ = ["ConfirmEmailScenario", "RegisterUserScenario", "get_mailer", "get_password_hasher"]

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


RegisterUserScenario = Annotated[RegisterUser, Depends(build_register_user)]
ConfirmEmailScenario = Annotated[ConfirmEmail, Depends(build_confirm_email)]
