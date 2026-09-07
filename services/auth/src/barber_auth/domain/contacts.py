"""Normalisation of the two contacts a user is identified by.

Both are normalised **before** the row is written, never on the way out. The
database enforces uniqueness on what it stores, so a phone kept as the user
typed it is a phone that is unique in four different spellings, and an email
compared case-sensitively lets the same person register twice.

The phone rules are deliberately small: strip what humans use as separators,
accept the Russian national form, and check the result against E.164. A full
national numbering plan is a library and a dependency of the domain; the
platform serves one country and the check that matters here is that two
spellings of one number cannot both reach the database.
"""

from __future__ import annotations

import re

from barber_auth.domain.errors import InvalidEmailAddress, InvalidPhoneNumber

__all__ = ["normalize_email", "normalize_phone"]

# E.164: a plus, a country code that never starts with zero, up to 15 digits in
# total. The lower bound of 8 rejects fragments such as "+7495" without
# rejecting the short national plans that do exist.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# What people type between the digits, and nothing else: a letter inside a
# phone number means the input was not a phone number.
_SEPARATORS = re.compile(r"[\s\-()./]")

_RUSSIAN_TRUNK_PREFIX = "8"
_RUSSIAN_COUNTRY_CODE = "+7"
_RUSSIAN_NATIONAL_LENGTH = 11

MAX_EMAIL_LENGTH = 254


def normalize_email(raw: str) -> str:
    """Return the email in the form the uniqueness index compares.

    Lower case and stripped, and nothing else: the separators a phone number
    tolerates are meaningful inside an address, and removing them would turn
    ``ivan@example.com`` into a different mailbox. The address itself is
    validated at the edge by pydantic; what happens here is the part the
    database depends on, and it has to happen for every write, including the
    ones that never pass through a request body.
    """
    email = raw.strip().lower()
    if not email or "@" not in email or len(email) > MAX_EMAIL_LENGTH:
        raise InvalidEmailAddress("Email address is not valid")
    return email


def normalize_phone(raw: str) -> str:
    """Return the phone in E.164, or reject it.

    ``+7 (999) 123-45-67``, ``8 999 123 45 67`` and ``+79991234567`` are one
    number and must produce one string; anything that is not a phone number
    raises rather than reaching the database in a shape nothing can match.
    """
    digits = _SEPARATORS.sub("", raw)
    if digits.startswith(_RUSSIAN_TRUNK_PREFIX) and len(digits) == _RUSSIAN_NATIONAL_LENGTH:
        digits = _RUSSIAN_COUNTRY_CODE + digits[1:]

    if not _E164.match(digits):
        raise InvalidPhoneNumber("Phone number is not in a recognised format")
    return digits
