"""The stand-in mailer: what it writes, and where it refuses to."""

from __future__ import annotations

from barber_auth.adapters.dev_mailer import DevMailer
from barber_common.config import Environment

TOKEN = "kJ9-token-value"  # noqa: S105 - a fixture, not a credential


def build_mailer(environment: Environment) -> DevMailer:
    return DevMailer(
        environment=environment,
        confirmation_url="http://localhost:8080/confirm-email",
    )


def test_the_link_carries_the_token_in_the_query_string() -> None:
    link = build_mailer(Environment.LOCAL).confirmation_link(TOKEN)

    assert link == f"http://localhost:8080/confirm-email?token={TOKEN}"


def test_a_token_with_url_unsafe_characters_is_escaped() -> None:
    link = build_mailer(Environment.LOCAL).confirmation_link("a+b/c=")

    assert link.endswith("?token=a%2Bb%2Fc%3D")


def test_letters_are_written_where_a_developer_can_read_them() -> None:
    assert build_mailer(Environment.LOCAL).is_enabled is True
    assert build_mailer(Environment.TEST).is_enabled is True


def test_the_mailer_refuses_to_print_outside_local_and_test() -> None:
    # Printing a one-time token is acceptable while it stands in for an email
    # provider; in a deployed environment it would be a token in the logs.
    assert build_mailer(Environment.DEV).is_enabled is False
    assert build_mailer(Environment.PROD).is_enabled is False


def test_sending_never_raises_even_where_it_is_disabled() -> None:
    build_mailer(Environment.PROD).send_confirmation(email="ivan@example.com", token=TOKEN)
