"""Making a repeated request repeat its answer instead of its effect.

A client that loses the connection retries, and a double click sends the same
request twice. The defence is the header ``Idempotency-Key`` plus a table: the
key, a fingerprint of the request body and the answer that was given are
written in the same transaction as the effect, so either both are there or
neither is.

What the scenario does with it, in order: ask :meth:`IdempotencyRepository.find`
for the key, return the stored answer if the fingerprints match, refuse with
``idempotency_key_reuse`` if they differ, otherwise do the work and store the
answer beside it. Two identical requests racing each other both get that far;
the second one loses the insert on the primary key, rolls back, and reads the
answer the first one stored.

Reading the header is a dependency, because it happens before the scenario and
needs the raw body. Reading and writing the row is not: a dependency finishes
before the transaction of the scenario opens, and the point of the table is
that the key and the effect are committed together.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import String, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from starlette.requests import Request

from barber_common.db.base import Base
from barber_common.errors import DomainError, ValidationFailed

__all__ = [
    "DEFAULT_TTL",
    "IDEMPOTENCY_HEADER",
    "IdempotencyKey",
    "IdempotencyKeyReuse",
    "IdempotencyRepository",
    "IdempotentRequest",
    "RequiredIdempotencyKey",
    "StoredResponse",
    "fingerprint",
    "idempotent_request",
]

IDEMPOTENCY_HEADER = "Idempotency-Key"

# How long an answer is worth repeating. Long enough to cover a client
# retrying after an outage, short enough that the table stays small.
DEFAULT_TTL = timedelta(hours=24)

MAX_KEY_LENGTH = 255


class IdempotencyKeyReuse(DomainError):
    """The same key was sent with a different request.

    ``422`` rather than a silent second booking: the client has a bug, and
    answering the stored response would hide it.
    """

    code = "idempotency_key_reuse"
    http_status = 422
    title = "Idempotency key was used for another request"


class IdempotencyKey(Base):
    """One answer, kept for as long as it is worth repeating."""

    __tablename__ = "idempotency_keys"

    # Scoped to the caller: two clients picking the same key are two requests.
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), primary_key=True)

    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int]
    response_body: Mapped[dict[str, object]] = mapped_column(JSONB)

    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """What was answered the first time."""

    status: int
    body: dict[str, object]


@dataclass(frozen=True, slots=True)
class IdempotentRequest:
    """The key a request carries, and a fingerprint of what it asked for."""

    key: str
    fingerprint: str


def fingerprint(body: bytes) -> str:
    """Hash of the request body, to tell one request from another.

    SHA-256 of the bytes as they arrived: not a security boundary, but a
    collision here would return the answer of another request.
    """
    return hashlib.sha256(body).hexdigest()


async def idempotent_request(request: Request) -> IdempotentRequest:
    """Read the header a repeatable request has to carry.

    Missing or malformed is ``422``, never a quietly processed request: a
    client that forgot the header is a client whose retry creates a second
    booking.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if not key:
        raise ValidationFailed(f"The {IDEMPOTENCY_HEADER} header is required")
    if len(key) > MAX_KEY_LENGTH:
        raise ValidationFailed(f"The {IDEMPOTENCY_HEADER} header is too long")

    # Starlette caches the body, so reading it here does not stop FastAPI from
    # parsing it into the request model afterwards.
    return IdempotentRequest(key=key, fingerprint=fingerprint(await request.body()))


RequiredIdempotencyKey = Annotated[IdempotentRequest, Depends(idempotent_request)]


class IdempotencyRepository:
    """The stored answers of one caller."""

    def __init__(self, session: AsyncSession, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._session = session
        self._ttl = ttl

    async def find(
        self, *, user_id: UUID, request: IdempotentRequest, now: datetime
    ) -> StoredResponse | None:
        """The answer given to this key before, if it still counts.

        An expired row is removed and reported as absent: the client is free to
        reuse a key a day later, and the row would otherwise block it forever.
        Raises :class:`IdempotencyKeyReuse` when the key was used for something
        else.
        """
        row = await self._session.get(IdempotencyKey, (user_id, request.key))
        if row is None:
            return None

        if row.expires_at <= now:
            await self._session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.user_id == user_id, IdempotencyKey.key == request.key
                )
            )
            await self._session.flush()
            return None

        if row.request_hash != request.fingerprint:
            raise IdempotencyKeyReuse("This key was used for a different request")

        return StoredResponse(status=row.response_status, body=row.response_body)

    async def remember(
        self,
        *,
        user_id: UUID,
        request: IdempotentRequest,
        response: StoredResponse,
        now: datetime,
    ) -> None:
        """Store the answer, inside the transaction that produced it.

        A concurrent request holding the same key makes this fail on the
        primary key. That failure is the point: the caller rolls back and reads
        the answer the winner stored.
        """
        self._session.add(
            IdempotencyKey(
                user_id=user_id,
                key=request.key,
                request_hash=request.fingerprint,
                response_status=response.status,
                response_body=response.body,
                created_at=now,
                expires_at=now + self._ttl,
            )
        )
        await self._session.flush()
