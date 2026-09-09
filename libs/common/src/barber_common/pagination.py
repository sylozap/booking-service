"""Cursor pagination, the only kind of listing this platform has.

``offset`` is forbidden (docs/CODING_STANDARDS.md section 9) and the reason is
not style. Between two page requests rows are inserted and deleted, and an
offset counts rows: a salon added while a client reads page one pushes one
entry off the edge of it, and page two starts after the row that was pushed --
so that entry is never shown. Deleting a row does the opposite and shows one
twice. On a list that clients page through while other clients write, the two
happen constantly and silently.

A keyset cursor names the last row seen instead of counting rows before it::

    ORDER BY name, id
    WHERE (name, id) > ('Barber One', '0192f3c1-...')

Rows appearing before that point do not move it, and rows appearing after it
are simply seen on the next page. That needs two things and this module
provides both: the sort key must be **unique**, which is why every ordering
here ends in the primary key, and the comparison must be a **row value** --
``(a, b) > (x, y)``, not ``a > x AND b > y``, which is a different and wrong
predicate.

The cursor is opaque to the client: a base64url string with no padding. Opaque
because it is a sort key, and a client that learns to build one has learned a
detail of the ORDER BY that then cannot change without breaking them.
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

    Not a ``404`` and not a ``400``: a cursor arrives in the query string, so a
    malformed one is a request that does not match the contract, and the
    platform answers those with ``422`` and a domain code
    (docs/04-api-contracts.md).

    The message never quotes the cursor back. It is client-supplied text that
    would end up in a log line and in an error body.
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

    Every keyset in this platform ends in the primary key, which is a UUID, and
    a cursor that survived :func:`decode_cursor` can still carry a string that
    is not one. Refusing it here keeps a malformed cursor a ``422`` rather than
    a driver error at the point the statement is bound.
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
    """One page of a listing, in the shape docs/04-api-contracts.md fixes.

    ``next_cursor`` is null on the last page. It is deliberately not a
    "has_more" boolean next to a total count: a total is a second query whose
    answer is stale before it is serialised, and no client of this platform
    renders one.
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
