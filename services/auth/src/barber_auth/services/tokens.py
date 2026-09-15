"""Issuing, rotating and revoking token pairs.

* Login checks the password and a confirmed address, issues an access token
  and opens a new refresh token family.
* Refresh exchanges a live refresh token for a new pair under a row lock. A
  token that was already exchanged revokes its whole family.
* Logout revokes one family or every family of a user; issued access tokens
  keep working until they expire.

None of them publishes an event.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.domain.contacts import normalize_email
from barber_auth.domain.errors import (
    EmailNotConfirmed,
    InvalidCredentials,
    RefreshTokenInvalid,
)
from barber_auth.domain.identifiers import SalonId, UserId
from barber_auth.domain.passwords import PasswordHasher
from barber_auth.domain.roles import Role, RoleGrant
from barber_auth.domain.signing import TokenSigner
from barber_auth.domain.tokens import (
    REFRESH_TOKEN_BYTES,
    build_access_claims,
    hash_refresh_token,
    is_refresh_token_reused,
    is_refresh_token_usable,
)
from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_auth.repositories.refresh_tokens import RefreshTokenRepository
from barber_auth.repositories.users import UserRepository
from barber_common.db.session import transaction
from barber_common.logging import get_logger

__all__ = ["IssueTokenPair", "RefreshTokenPair", "RevokeSessions", "TokenPair"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a successful login or refresh hands back.

    ``refresh_token`` is the only moment the plain value exists: the database
    keeps its hash. Neither field is ever logged.
    """

    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Where a session was opened from.

    Stored for display and investigation only, never used as a security check.
    """

    user_agent: str | None = None
    ip: str | None = None


class _TokenMinter:
    """The part of issuing a pair that login and refresh have in common."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        signer: TokenSigner,
        issuer: str,
        access_ttl_minutes: int,
        refresh_ttl_days: int,
    ) -> None:
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)
        self._signer = signer
        self._issuer = issuer
        self._access_ttl_minutes = access_ttl_minutes
        self._refresh_ttl = timedelta(days=refresh_ttl_days)

    async def mint(
        self,
        *,
        user_id: UserId,
        family_id: UUID,
        issued_at: datetime,
        context: SessionContext,
    ) -> tuple[TokenPair, UUID]:
        """Sign an access token and open one refresh row. Returns the new row id.

        The caller supplies ``family_id``: a login starts a family, a rotation
        continues one, and that is the only difference between the two.
        """
        roles = await self._grants_of(user_id)

        access_expires_at = issued_at + timedelta(minutes=self._access_ttl_minutes)
        claims = build_access_claims(
            user_id=user_id,
            roles=roles,
            issuer=self._issuer,
            token_id=uuid4(),
            issued_at=issued_at,
            ttl_minutes=self._access_ttl_minutes,
        )
        access_token = self._signer.sign(claims)

        refresh_token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
        refresh_expires_at = issued_at + self._refresh_ttl
        row = await self._tokens.add(
            user_id=user_id,
            family_id=family_id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=refresh_expires_at,
            user_agent=context.user_agent,
            ip=context.ip,
        )

        pair = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        return pair, row.id

    async def _grants_of(self, user_id: UserId) -> tuple[RoleGrant, ...]:
        """The roles that go into the token, read at the moment it is signed.

        Read on every issue, so role changes reach the user on the next refresh.
        """
        return tuple(
            RoleGrant(
                role=Role(grant.role),
                salon_id=SalonId(grant.salon_id) if grant.salon_id is not None else None,
            )
            for grant in await self._users.roles_of(user_id)
        )


class IssueTokenPair:
    """Log a user in: verify the password, open a session."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        hasher: PasswordHasher,
        signer: TokenSigner,
        issuer: str,
        access_ttl_minutes: int,
        refresh_ttl_days: int,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._hasher = hasher
        self._minter = _TokenMinter(
            session=session,
            signer=signer,
            issuer=issuer,
            access_ttl_minutes=access_ttl_minutes,
            refresh_ttl_days=refresh_ttl_days,
        )

    async def execute(
        self,
        *,
        email: str,
        password: str,
        context: SessionContext | None = None,
        now: datetime | None = None,
    ) -> TokenPair:
        """Exchange an address and a password for a pair of tokens."""
        issued_at = now or datetime.now(UTC)

        # The lookup runs in its own transaction, closed before the hash is
        # verified. Otherwise the implicit transaction of the query would be
        # joined and never committed, and argon2 would hold a connection.
        async with transaction(self._session):
            user = await self._find_user(email)

        if not await self._password_matches(user=user, password=password):
            raise InvalidCredentials("Email or password is not correct")

        if user is None:  # pragma: no cover - _password_matches rejects a missing user
            raise InvalidCredentials("Email or password is not correct")

        if user.email_confirmed_at is None:
            raise EmailNotConfirmed("Confirm your email address before signing in")

        user_id = UserId(user.id)
        async with transaction(self._session):
            pair, _ = await self._minter.mint(
                user_id=user_id,
                # A login is a new family: the sessions of two devices must be
                # revocable separately, and reuse detection on one must not
                # touch the other.
                family_id=uuid4(),
                issued_at=issued_at,
                context=context or SessionContext(),
            )

        _logger.info("user signed in", user_id=str(user_id))
        return pair

    async def _find_user(self, email: str) -> User | None:
        """The account behind an address, or nothing if the address is unusable.

        A malformed address is not a validation error here. Reporting one would
        answer faster and differently than a well-formed address that is simply
        not registered, and that difference is a way to probe the service.
        """
        try:
            normalized = normalize_email(email)
        except ValueError:
            return None
        return await self._users.get_by_email(normalized)

    async def _password_matches(self, *, user: User | None, password: str) -> bool:
        """Verify, spending the same time whether or not the account exists.

        A deactivated account takes the same path as a missing one: it verifies
        nothing and answers False after paying the full cost, so "deactivated"
        and "never existed" are indistinguishable from the outside.
        """
        if user is None or not user.is_active:
            await asyncio.to_thread(self._hasher.verify_dummy)
            return False
        return await asyncio.to_thread(self._hasher.verify, password, user.password_hash)


class RefreshTokenPair:
    """Rotate a refresh token, detecting the reuse that means it was stolen."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        signer: TokenSigner,
        issuer: str,
        access_ttl_minutes: int,
        refresh_ttl_days: int,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)
        self._minter = _TokenMinter(
            session=session,
            signer=signer,
            issuer=issuer,
            access_ttl_minutes=access_ttl_minutes,
            refresh_ttl_days=refresh_ttl_days,
        )

    async def execute(
        self,
        *,
        refresh_token: str,
        context: SessionContext | None = None,
        now: datetime | None = None,
    ) -> TokenPair:
        """Exchange a live refresh token for a new pair in the same family.

        The lookup, reuse check, revocation and new row happen in one
        transaction with the old row locked. A refusal is raised only after
        that transaction commits, so a family revoked on reuse stays revoked.
        """
        rotated_at = now or datetime.now(UTC)
        token_hash = hash_refresh_token(refresh_token)

        async with transaction(self._session):
            stored = await self._tokens.lock_by_token_hash(token_hash)
            pair = (
                None
                if stored is None
                else await self._rotate(
                    stored=stored,
                    rotated_at=rotated_at,
                    context=context or SessionContext(),
                )
            )

        if stored is None or pair is None:
            # One answer for unknown, expired and reused. Naming the reuse
            # would tell whoever stole the token that it was noticed.
            raise RefreshTokenInvalid("Refresh token is not valid")

        _logger.info("refresh token rotated", user_id=str(stored.user_id))
        return pair

    async def _rotate(
        self,
        *,
        stored: RefreshToken,
        rotated_at: datetime,
        context: SessionContext,
    ) -> TokenPair | None:
        """Issue the successor of a live token, or return ``None`` to refuse.

        Returning instead of raising lets the revocations a refusal performs be
        committed by the caller's transaction.
        """
        if is_refresh_token_reused(revoked_at=stored.revoked_at):
            await self._kill_family(token=stored, revoked_at=rotated_at)
            return None

        if not is_refresh_token_usable(
            revoked_at=stored.revoked_at,
            expires_at=stored.expires_at,
            now=rotated_at,
        ):
            # Expired, not stolen. The session ended on its own and the rest of
            # the family -- there is none -- is left alone.
            return None

        user = await self._users.get_by_id(UserId(stored.user_id))
        if user is None or not user.is_active:
            # The account went away or was deactivated while the session was
            # open. The family is closed so that the tokens still held by a
            # deactivated user stop working.
            await self._tokens.revoke_family(family_id=stored.family_id, revoked_at=rotated_at)
            return None

        pair, issued_row_id = await self._minter.mint(
            user_id=UserId(stored.user_id),
            family_id=stored.family_id,
            issued_at=rotated_at,
            context=context,
        )
        await self._tokens.rotate(
            token=stored,
            replaced_by=issued_row_id,
            revoked_at=rotated_at,
        )
        return pair

    async def _kill_family(self, *, token: RefreshToken, revoked_at: datetime) -> None:
        """Revoke every live token of the family a reused token belongs to.

        Logged at ``WARNING`` with identifiers only, never a token or a hash.
        """
        ended = await self._tokens.revoke_family(family_id=token.family_id, revoked_at=revoked_at)
        _logger.warning(
            "refresh token reuse detected, family revoked",
            user_id=str(token.user_id),
            family_id=str(token.family_id),
            sessions_ended=ended,
        )


class RevokeSessions:
    """End a session, or every session of a user."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tokens = RefreshTokenRepository(session)

    async def execute(
        self,
        *,
        refresh_token: str,
        all_devices: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Revoke the family behind this token, or all families of its owner.

        An unknown or already revoked token is not an error and not treated as
        reuse, so logout is safe to repeat.
        """
        revoked_at = now or datetime.now(UTC)

        async with transaction(self._session):
            stored = await self._tokens.get_by_token_hash(hash_refresh_token(refresh_token))
            if stored is None:
                return

            if all_devices:
                ended = await self._tokens.revoke_all_of_user(
                    user_id=UserId(stored.user_id), revoked_at=revoked_at
                )
            else:
                ended = await self._tokens.revoke_family(
                    family_id=stored.family_id, revoked_at=revoked_at
                )

        _logger.info(
            "sessions revoked",
            user_id=str(stored.user_id),
            all_devices=all_devices,
            sessions_ended=ended,
        )
