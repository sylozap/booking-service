"""Keyset (cursor) pagination for listings.

Offsets are not used: rows inserted or deleted between page requests would make
an offset skip or repeat entries. A cursor names the last row seen instead::

    ORDER BY name, id
    WHERE (name, id) > ('Barber One', '0192f3c1-...')

Every ordering ends in the primary key so the sort key is unique, and the
comparison is a row value. The cursor is an opaque base64url string.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict

from barber_common.errors import DomainError

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "InvalidCursor",
    "Page",
    "PageRequest",
    "Pagination",
    "cursor_uuid",
    "decode_cursor",
    "encode_cursor",
    "page_request",
]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class InvalidCursor(DomainError):
    """The cursor is not one this service handed out.

    Answered with ``422`` like any other invalid input. The message never
    quotes the cursor back.
    """

    code = "validation_error"
    http_status = 422
    title = "Cursor is not valid"


def encode_cursor(values: Sequence[str]) -> str:
    """Pack a sort key into the opaque string a client carries back.

    JSON rather than a separator, because the values are names and addresses
    and any separator would eventually appear inside one. base64url without
    padding, because the result travels in a query string.
    """
    raw = json.dumps(list(values), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, arity: int) -> tuple[str, ...]:
    """Unpack a cursor, refusing anything this service did not produce.

    ``arity`` is how many columns the ordering has. Checking it is what keeps a
    cursor from one listing from being replayed against another, where it would
    silently compare a salon name against a city.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise InvalidCursor("The cursor is not readable") from error

    if not isinstance(decoded, list) or len(decoded) != arity:
        raise InvalidCursor("The cursor does not belong to this listing")
    if not all(isinstance(value, str) for value in decoded):
        raise InvalidCursor("The cursor does not belong to this listing")

    return tuple(str(value) for value in decoded)


def cursor_uuid(value: str) -> UUID:
    """Read the primary key half of a sort key.

    A value that is not a UUID raises :class:`InvalidCursor`, so a malformed
    cursor is answered with ``422``.
    """
    try:
        return UUID(value)
    except ValueError as error:
        raise InvalidCursor("The cursor does not belong to this listing") from error


class PageRequest(BaseModel):
    """What the caller asked for: how many, and from where.

    ``limit`` is bounded on both sides. Without an upper bound one request
    reads the table into memory, and "give me everything" is how a listing
    endpoint becomes an outage.
    """

    model_config = ConfigDict(frozen=True)

    limit: int = DEFAULT_PAGE_SIZE
    cursor: str | None = None

    def key(self, *, arity: int) -> tuple[str, ...] | None:
        """The sort key to resume after, or nothing on the first page."""
        if self.cursor is None:
            return None
        return decode_cursor(self.cursor, arity=arity)

    def window(self) -> int:
        """How many rows to actually fetch.

        One more than asked for. That extra row is never returned; its presence
        is the answer to "is there a next page", which is otherwise a second
        query or a count that is wrong the moment it is taken.
        """
        return self.limit + 1


def page_request(
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description="How many items to return."),
    ] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None,
        Query(description="Opaque position from the `next_cursor` of a previous page."),
    ] = None,
) -> PageRequest:
    """FastAPI dependency behind ``?limit=&cursor=``."""
    return PageRequest(limit=limit, cursor=cursor)


# Ready-made annotation, so an endpoint does not repeat the Depends call.
Pagination = Annotated[PageRequest, Depends(page_request)]


class Page[ItemT](BaseModel):
    """One page of a listing.

    ``next_cursor`` is null on the last page. No total count is returned.
    """

    items: list[ItemT]
    next_cursor: str | None = None

    @classmethod
    def of(
        cls,
        rows: Sequence[ItemT],
        *,
        request: PageRequest,
        cursor_of: Callable[[ItemT], Sequence[str]],
    ) -> Page[ItemT]:
        """Cut a fetched window down to a page and name the next position.

        ``rows`` is what :meth:`PageRequest.window` asked for -- one more than
        the caller wants. If it arrived, there is another page and the cursor
        points at the last row of this one; if it did not, this is the end.
        """
        if len(rows) <= request.limit:
            return cls(items=list(rows))

        page = list(rows[: request.limit])
        return cls(items=page, next_cursor=encode_cursor(cursor_of(page[-1])))
