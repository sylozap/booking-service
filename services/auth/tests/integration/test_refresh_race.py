"""Two requests, one refresh token, real connections.

This is the test the row lock exists for, and it cannot be written against the
ordinary fixtures: those put every session on one connection inside one
transaction that is rolled back at the end, so ``SELECT ... FOR UPDATE`` would
either block against itself or read its own uncommitted work. Here the sessions
are on separate connections and commit for real, which means the test also has
to clean up after itself.

What it proves: a token presented twice at the same moment never produces two
valid pairs. Either the second request loses the race and finds the token
already spent -- which is reuse, and kills the family -- or both fail. What must
never happen is two live sessions where there was one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.domain.errors import RefreshTokenInvalid
from barber_auth.domain.identifiers import UserId
from barber_auth.domain.roles import Role
from barber_auth.domain.tokens import hash_refresh_token
from barber_auth.models.refresh_token import RefreshToken
from barber_auth.models.user import User
from barber_auth.repositories.users import UserRepository
from barber_auth.services.tokens import RefreshTokenPair, TokenPair
from barber_common.db.session import transaction

pytestmark = pytest.mark.integration

ISSUER = "https://barber.local/auth"
PASSWORD_HASH = "$argon2id$v=19$m=8,t=1,p=1$c29tZXNhbHQ$0000000000000000000000000000"


@pytest.fixture
async def contender_user(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
) -> User:
    """A committed user, removed again when the test ends.

    Committed because the two racing sessions have to be able to see it, which
    rules out the rolled-back fixture the rest of the suite uses.
    """
    async with concurrent_session_factory() as session, transaction(session):
        users = UserRepository(session)
        user = await users.add(
            User(
                email=f"racer-{uuid4().hex}@example.com",
                phone=f"+7999{uuid4().int % 10**7:07d}",
                password_hash=PASSWORD_HASH,
                email_confirmed_at=datetime.now(UTC),
            )
        )
        await users.grant_role(user_id=UserId(user.id), role=Role.CLIENT)
    return user


async def _issue_token(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> str:
    """Open one session for the user and return its refresh token."""
    token = uuid4().hex + uuid4().hex
    async with session_factory() as session, transaction(session):
        session.add(
            RefreshToken(
                user_id=user.id,
                family_id=uuid4(),
                token_hash=hash_refresh_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
    return token


async def _rotate(
    session_factory: async_sessionmaker[AsyncSession],
    signer: RsaTokenSigner,
    token: str,
) -> TokenPair | RefreshTokenInvalid:
    """One refresh request, on a connection of its own."""
    async with session_factory() as session:
        scenario = RefreshTokenPair(
            session=session,
            signer=signer,
            issuer=ISSUER,
            access_ttl_minutes=15,
            refresh_ttl_days=30,
        )
        try:
            return await scenario.execute(refresh_token=token)
        except RefreshTokenInvalid as error:
            return error


async def test_two_requests_with_one_token_never_yield_two_valid_pairs(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    signer: RsaTokenSigner,
    contender_user: User,
) -> None:
    token = await _issue_token(concurrent_session_factory, contender_user)

    try:
        first, second = await asyncio.gather(
            _rotate(concurrent_session_factory, signer, token),
            _rotate(concurrent_session_factory, signer, token),
        )

        succeeded = [outcome for outcome in (first, second) if isinstance(outcome, TokenPair)]
        # At most one. Two would mean the lock did not serialise the requests
        # and the family mechanism is detecting nothing.
        assert len(succeeded) <= 1
    finally:
        await _remove_user(concurrent_session_factory, contender_user)


async def test_the_loser_of_the_race_kills_the_family(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    signer: RsaTokenSigner,
    contender_user: User,
) -> None:
    token = await _issue_token(concurrent_session_factory, contender_user)

    try:
        await asyncio.gather(
            _rotate(concurrent_session_factory, signer, token),
            _rotate(concurrent_session_factory, signer, token),
        )

        async with concurrent_session_factory() as session:
            statement = select(RefreshToken).where(RefreshToken.user_id == contender_user.id)
            tokens = (await session.execute(statement)).scalars().all()

        # Whichever request lost, it presented a token that had just been
        # spent, and that is theft as far as the service can tell: nothing of
        # this family survives, including the pair the winner was handed.
        assert tokens
        assert all(row.revoked_at is not None for row in tokens)
    finally:
        await _remove_user(concurrent_session_factory, contender_user)


async def test_a_sequential_rotation_still_works(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    signer: RsaTokenSigner,
    contender_user: User,
) -> None:
    token = await _issue_token(concurrent_session_factory, contender_user)

    try:
        outcome = await _rotate(concurrent_session_factory, signer, token)

        # The control: the locking must not have made the ordinary path fail.
        assert isinstance(outcome, TokenPair)
    finally:
        await _remove_user(concurrent_session_factory, contender_user)


async def _remove_user(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    """Undo what this file committed. The tokens go with the user by cascade."""
    async with session_factory() as session, transaction(session):
        await session.execute(delete(User).where(User.id == user.id))
