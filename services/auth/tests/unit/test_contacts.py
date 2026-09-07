"""Normalisation of the two contacts a user is identified by."""

from __future__ import annotations

import pytest

from barber_auth.domain.contacts import normalize_email, normalize_phone
from barber_auth.domain.errors import InvalidEmailAddress, InvalidPhoneNumber


@pytest.mark.parametrize(
    "written",
    [
        "+79991234567",
        "+7 999 123-45-67",
        "+7 (999) 123 45 67",
        "8 999 123 45 67",
        "8(999)123-45-67",
    ],
)
def test_one_number_written_five_ways_normalises_to_one_string(written: str) -> None:
    normalized = normalize_phone(written)

    # The uniqueness of a phone is an index over this value: if the spellings
    # disagreed here, one person could register five times.
    assert normalized == "+79991234567"


def test_a_number_of_another_country_keeps_its_country_code() -> None:
    assert normalize_phone("+49 30 123456789") == "+4930123456789"


@pytest.mark.parametrize(
    "written",
    [
        "",
        "99912345",
        "8 999 123",
        "+7999123456789012",
        "+0 999 123 45 67",
        "+7 999 123 45 6a",
        "not a phone",
    ],
)
def test_a_string_that_is_not_a_phone_number_is_rejected(written: str) -> None:
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(written)


def test_the_national_prefix_is_only_applied_to_a_full_national_number() -> None:
    # "8" is a trunk prefix, not a country code: applying it to anything that
    # merely starts with 8 would turn foreign numbers into Russian ones.
    assert normalize_phone("+8613912345678") == "+8613912345678"


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("Ivan@Example.COM", "ivan@example.com"),
        (" ivan@example.com ", "ivan@example.com"),
        ("IVAN@EXAMPLE.COM", "ivan@example.com"),
    ],
)
def test_an_address_normalises_to_what_the_unique_index_compares(
    written: str, expected: str
) -> None:
    assert normalize_email(written) == expected


@pytest.mark.parametrize("written", ["", "ivan", "   ", "i" * 250 + "@example.com"])
def test_a_string_that_is_not_an_address_is_rejected(written: str) -> None:
    with pytest.raises(InvalidEmailAddress):
        normalize_email(written)
