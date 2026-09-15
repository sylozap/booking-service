"""Cursor pagination: the round trip, the refusals and the page boundary."""

from __future__ import annotations

import base64
import json

import pytest

from barber_common.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursor,
    Page,
    PageRequest,
    decode_cursor,
    encode_cursor,
    page_request,
)


def test_a_sort_key_survives_the_round_trip() -> None:
    key = ("Barber One", "0192f3c1-0000-7000-8000-000000000001")

    restored = decode_cursor(encode_cursor(key), arity=2)

    assert restored == key


def test_a_value_containing_a_separator_survives() -> None:
    """The reason the cursor carries JSON and not a delimited string.

    Salon names contain commas, colons and pipes, and a delimiter that appears
    inside a value splits the key in the wrong place.
    """
    key = ("Barber, Bath & Beyond: the |original|", "id")

    assert decode_cursor(encode_cursor(key), arity=2) == key


def test_a_cursor_carries_no_padding() -> None:
    """It travels in a query string, where ``=`` has a meaning of its own."""
    assert "=" not in encode_cursor(("a",))


def test_a_cursor_does_not_show_the_client_the_sort_key() -> None:
    """Opaque, but not secret: it is base64, and the test says so on purpose.

    The point is the contract, not concealment. A client that decodes this has
    learned a detail of the ORDER BY that will change without warning.
    """
    cursor = encode_cursor(("Barber One", "id"))

    assert "Barber One" not in cursor


def test_a_cursor_from_another_listing_is_refused() -> None:
    """A two column key replayed against a three column ordering.

    Without the arity check it would silently compare a salon name against a
    city and return a page that is wrong rather than empty.
    """
    cursor = encode_cursor(("Barber One", "id"))

    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, arity=3)


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 at all !!",
        base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        base64.urlsafe_b64encode(json.dumps({"a": 1}).encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode().rstrip("="),
        "",
    ],
)
def test_a_cursor_the_service_did_not_produce_is_refused(cursor: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, arity=2)


def test_an_invalid_cursor_is_a_422_with_a_domain_code() -> None:
    """Input errors are answered with 422, never 400."""
    assert InvalidCursor.http_status == 422
    assert InvalidCursor.code == "validation_error"


def test_the_error_never_quotes_the_cursor_back() -> None:
    """It is client supplied text, and it would land in a log and a response."""
    cursor = encode_cursor(("secret-looking-value",))

    with pytest.raises(InvalidCursor) as failure:
        decode_cursor(cursor, arity=9)

    assert cursor not in failure.value.detail


def test_the_window_is_one_row_wider_than_the_page() -> None:
    """That row is what answers "is there more" without a second query."""
    assert PageRequest(limit=20).window() == 21


def test_a_full_window_becomes_a_page_and_a_cursor() -> None:
    request = PageRequest(limit=2)

    page = Page.of(["a", "b", "c"], request=request, cursor_of=lambda row: (row,))

    assert page.items == ["a", "b"]
    assert page.next_cursor == encode_cursor(("b",))


def test_a_short_window_is_the_last_page() -> None:
    request = PageRequest(limit=2)

    page = Page.of(["a", "b"], request=request, cursor_of=lambda row: (row,))

    assert page.items == ["a", "b"]
    assert page.next_cursor is None


def test_an_empty_listing_is_a_page_with_no_cursor() -> None:
    rows: list[str] = []

    page = Page.of(rows, request=PageRequest(limit=20), cursor_of=lambda row: (row,))

    assert page.items == []
    assert page.next_cursor is None


def test_the_first_page_resumes_from_nowhere() -> None:
    assert PageRequest(limit=20).key(arity=2) is None


def test_a_later_page_resumes_from_its_cursor() -> None:
    request = PageRequest(limit=20, cursor=encode_cursor(("Barber One", "id")))

    assert request.key(arity=2) == ("Barber One", "id")


def test_the_dependency_defaults_to_the_documented_page_size() -> None:
    request = page_request()

    assert request.limit == DEFAULT_PAGE_SIZE
    assert request.cursor is None
    assert DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE
