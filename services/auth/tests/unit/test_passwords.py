"""The password policy and the argon2 hasher behind its port."""

from __future__ import annotations

import time
from collections.abc import Callable
from statistics import median

import pytest

from barber_auth.adapters.argon2_hasher import Argon2Hasher
from barber_auth.domain.errors import WeakPassword
from barber_auth.domain.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordHasher,
    check_password_policy,
)

PASSWORD = "correct-horse-9"


def test_a_password_that_satisfies_the_policy_is_accepted() -> None:
    check_password_policy(PASSWORD)


@pytest.mark.parametrize(
    ("password", "rejected_because"),
    [
        ("short1", "shorter than the minimum"),
        ("a" * MIN_PASSWORD_LENGTH, "no digit"),
        ("1" * MIN_PASSWORD_LENGTH, "no letter"),
        (" password1 ", "surrounded by whitespace"),
        ("x1" + "y" * MAX_PASSWORD_LENGTH, "longer than the maximum"),
    ],
)
def test_a_weak_password_is_rejected(password: str, rejected_because: str) -> None:
    with pytest.raises(WeakPassword) as failure:
        check_password_policy(password)

    # The rule that failed is named; the password itself never is.
    assert password not in str(failure.value), rejected_because


def test_the_minimum_length_comes_from_the_caller() -> None:
    check_password_policy("abcdefg1", min_length=8)

    with pytest.raises(WeakPassword):
        check_password_policy("abcdefg1", min_length=20)


def test_the_argon2_hasher_satisfies_the_port(hasher: Argon2Hasher) -> None:
    assert isinstance(hasher, PasswordHasher)


def test_hashing_the_same_password_twice_gives_different_hashes(hasher: Argon2Hasher) -> None:
    first = hasher.hash(PASSWORD)
    second = hasher.hash(PASSWORD)

    # The salt is random, so an attacker cannot tell from the database that two
    # users chose the same password.
    assert first != second
    assert PASSWORD not in first


def test_a_hash_verifies_against_the_password_that_made_it(hasher: Argon2Hasher) -> None:
    assert hasher.verify(PASSWORD, hasher.hash(PASSWORD)) is True


def test_a_wrong_password_does_not_verify(hasher: Argon2Hasher) -> None:
    assert hasher.verify("wrong-password-1", hasher.hash(PASSWORD)) is False


def test_a_damaged_hash_is_a_mismatch_rather_than_a_crash(hasher: Argon2Hasher) -> None:
    # A row broken by a bad migration must not turn every login into a 500.
    assert hasher.verify(PASSWORD, "not-an-argon2-hash") is False


def test_verifying_against_nothing_costs_what_a_real_verification_costs(
    hasher: Argon2Hasher,
) -> None:
    password_hash = hasher.hash(PASSWORD)

    real = _duration_of(lambda: hasher.verify("wrong-password-1", password_hash))
    dummy = _duration_of(hasher.verify_dummy)

    # The two are within an order of magnitude of each other. A tighter bound
    # would be a flaky test on a loaded machine; what this rules out is the
    # difference that matters -- a missing user answering in microseconds while
    # a wrong password costs a full argon2 pass.
    assert 0.1 < dummy / real < 10.0


def _duration_of(action: Callable[[], object]) -> float:
    """Median wall time of five calls, so one scheduling hiccup cannot decide."""
    durations: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        action()
        durations.append(time.perf_counter() - started)
    return median(durations)
