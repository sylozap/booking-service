"""Repeating an answer instead of an effect, against a real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from barber_common.db.errors import is_unique_violation
from barber_common.db.session import unit_of_work
from barber_common.errors import install_error_handlers
from barber_common.idempotency import (
    IDEMPOTENCY_HEADER,
    IdempotencyKeyReuse,
    IdempotencyRepository,
    IdempotentRequest,
    RequiredIdempotencyKey,
    StoredResponse,
    fingerprint,
)
from barber_common.testing.fixtures import app_client

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
CREATED = StoredResponse(status=201, body={"id": "booking-1"})


def asked(
    body: bytes = b'{"start_at": "2026-10-05T10:00:00Z"}', key: str = "key-1"
) -> IdempotentRequest:
    return IdempotentRequest(key=key, fingerprint=fingerprint(body))


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session whose writes are rolled back when the test ends."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def user_id() -> UUID:
    return uuid4()


async def test_a_key_nobody_has_used_is_absent(session: AsyncSession, user_id: UUID) -> None:
    repository = IdempotencyRepository(session)

    assert await repository.find(user_id=user_id, request=asked(), now=NOW) is None


async def test_the_stored_answer_comes_back_for_the_same_request(
    session: AsyncSession, user_id: UUID
) -> None:
    repository = IdempotencyRepository(session)
    request = asked()
    await repository.remember(user_id=user_id, request=request, response=CREATED, now=NOW)

    found = await repository.find(user_id=user_id, request=request, now=NOW + timedelta(minutes=1))

    assert found == CREATED


async def test_the_same_key_with_another_body_is_refused(
    session: AsyncSession, user_id: UUID
) -> None:
    repository = IdempotencyRepository(session)
    await repository.remember(user_id=user_id, request=asked(), response=CREATED, now=NOW)

    with pytest.raises(IdempotencyKeyReuse) as failure:
        await repository.find(
            user_id=user_id, request=asked(body=b'{"start_at": "2026-10-05T12:00:00Z"}'), now=NOW
        )

    assert failure.value.code == "idempotency_key_reuse"
    assert failure.value.http_status == 422


async def test_the_key_of_another_caller_is_another_request(
    session: AsyncSession, user_id: UUID
) -> None:
    repository = IdempotencyRepository(session)
    await repository.remember(user_id=user_id, request=asked(), response=CREATED, now=NOW)

    assert await repository.find(user_id=uuid4(), request=asked(), now=NOW) is None


async def test_an_expired_key_is_handled_as_a_new_request(
    session: AsyncSession, user_id: UUID
) -> None:
    repository = IdempotencyRepository(session, ttl=timedelta(hours=1))
    await repository.remember(user_id=user_id, request=asked(), response=CREATED, now=NOW)

    found = await repository.find(user_id=user_id, request=asked(), now=NOW + timedelta(hours=2))

    assert found is None
    # Removed rather than kept: the key is free to be used again.
    await repository.remember(user_id=user_id, request=asked(), response=CREATED, now=NOW)


async def test_two_requests_holding_one_key_leave_one_answer(
    concurrent_session_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> None:
    """The loser of the insert is what makes a double click safe."""

    async def handle(response: StoredResponse) -> str:
        async with unit_of_work(concurrent_session_factory) as session:
            repository = IdempotencyRepository(session)
            request = asked()
            stored = await repository.find(user_id=user_id, request=request, now=NOW)
            if stored is not None:
                return "repeated"
            await asyncio.sleep(0.05)
            await repository.remember(user_id=user_id, request=request, response=response, now=NOW)
            return "created"

    outcomes = await asyncio.gather(
        handle(CREATED),
        handle(StoredResponse(status=201, body={"id": "booking-2"})),
        return_exceptions=True,
    )

    created = [outcome for outcome in outcomes if outcome == "created"]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, IntegrityError)]
    assert len(created) == 1
    assert len(conflicts) == 1
    assert is_unique_violation(conflicts[0])

    async with unit_of_work(concurrent_session_factory) as session:
        kept = await IdempotencyRepository(session).find(user_id=user_id, request=asked(), now=NOW)
    assert kept is not None


# --- the header -------------------------------------------------------------


def build_app() -> FastAPI:
    """An application whose one endpoint demands the header."""
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/things")
    async def create(request: RequiredIdempotencyKey, body: dict[str, str]) -> dict[str, str]:
        return {"key": request.key, "fingerprint": request.fingerprint, "sent": body["value"]}

    return app


async def test_the_header_arrives_beside_the_parsed_body() -> None:
    async with app_client(build_app()) as client:
        response = await client.post(
            "/things", json={"value": "hello"}, headers={IDEMPOTENCY_HEADER: "key-1"}
        )

    assert response.status_code == 200
    assert response.json()["key"] == "key-1"
    # Reading the raw body in the dependency leaves it readable for the model.
    assert response.json()["sent"] == "hello"


async def test_a_request_without_the_header_is_422() -> None:
    async with app_client(build_app()) as client:
        response = await client.post("/things", json={"value": "hello"})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_two_bodies_under_one_key_have_different_fingerprints() -> None:
    async with app_client(build_app()) as client:
        first = await client.post(
            "/things", json={"value": "one"}, headers={IDEMPOTENCY_HEADER: "key-1"}
        )
        second = await client.post(
            "/things", json={"value": "two"}, headers={IDEMPOTENCY_HEADER: "key-1"}
        )

    assert first.json()["fingerprint"] != second.json()["fingerprint"]
