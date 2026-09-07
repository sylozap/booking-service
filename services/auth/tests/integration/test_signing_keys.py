"""Registering the signing key: what a restart and a rotation do to the table."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from barber_auth.adapters.rsa_signer import RsaTokenSigner
from barber_auth.models.signing_key import SigningKey
from barber_auth.services.keys import PublishedKeys, RegisterSigningKey

pytestmark = pytest.mark.integration


async def stored_keys(session: AsyncSession) -> list[SigningKey]:
    return list((await session.execute(select(SigningKey))).scalars().all())


async def test_registering_stores_the_public_half_under_its_kid(
    session: AsyncSession, signer: RsaTokenSigner
) -> None:
    kid = await RegisterSigningKey(session=session, signer=signer).execute()

    stored = (await stored_keys(session))[0]
    assert kid == signer.kid
    assert stored.kid == signer.kid
    assert stored.public_pem == signer.public_pem
    assert stored.is_active is True


async def test_the_private_key_never_reaches_the_table(
    session: AsyncSession, signer: RsaTokenSigner
) -> None:
    await RegisterSigningKey(session=session, signer=signer).execute()

    stored = (await stored_keys(session))[0]
    assert "PRIVATE KEY" not in stored.public_pem


async def test_registering_twice_writes_one_row(
    session: AsyncSession, signer: RsaTokenSigner
) -> None:
    scenario = RegisterSigningKey(session=session, signer=signer)

    await scenario.execute()
    await scenario.execute()

    # What makes startup idempotent: the kid is derived from the key, so a
    # restart or a second replica registers the row it already owns.
    assert len(await stored_keys(session)) == 1


async def test_a_rotation_leaves_both_keys_active(
    session: AsyncSession, signer: RsaTokenSigner, make_private_key_pem: Callable[[], str]
) -> None:
    incoming = RsaTokenSigner(make_private_key_pem())

    await RegisterSigningKey(session=session, signer=signer).execute()
    await RegisterSigningKey(session=session, signer=incoming).execute()

    published = await PublishedKeys(session).execute()
    # The tokens signed a minute ago keep verifying while the new ones are
    # issued -- the transitional period ADR-0010 requires.
    assert {key["kid"] for key in published.keys} == {signer.kid, incoming.kid}


async def test_a_retired_key_is_not_brought_back_by_a_restart(
    session: AsyncSession, signer: RsaTokenSigner
) -> None:
    await RegisterSigningKey(session=session, signer=signer).execute()
    retired = (await stored_keys(session))[0]
    retired.is_active = False
    await session.flush()

    await RegisterSigningKey(session=session, signer=signer).execute()

    # An operator retired this key on purpose. A pod restarting must not undo
    # that decision, which is why the conflict does nothing rather than update.
    published = await PublishedKeys(session).execute()
    assert published.keys == ()


async def test_nothing_is_published_before_a_key_is_registered(session: AsyncSession) -> None:
    published = await PublishedKeys(session).execute()

    assert published.keys == ()
