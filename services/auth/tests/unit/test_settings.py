"""How the service is told which key to sign with."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from barber_auth.settings import AuthSettings

SettingsFactory = Callable[..., AuthSettings]
KeyFactory = Callable[[], str]


def test_a_key_given_inline_is_returned_as_a_pem(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory
) -> None:
    pem = make_private_key_pem()

    settings = make_settings(jwt_private_key=pem)

    assert settings.signing_key_pem() == pem


def test_a_key_given_on_one_line_is_unescaped(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory
) -> None:
    pem = make_private_key_pem()

    # How scripts/gen_keys.py --env writes it, because an environment variable
    # is one line and a PEM is many.
    settings = make_settings(jwt_private_key=pem.replace("\n", "\\n"))

    assert settings.signing_key_pem().splitlines() == pem.splitlines()


def test_a_key_is_read_from_the_file_it_is_mounted_at(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory, tmp_path: Path
) -> None:
    pem = make_private_key_pem()
    key_file = tmp_path / "auth-signing-key.pem"
    key_file.write_text(pem, encoding="utf-8")

    settings = make_settings(jwt_private_key_path=key_file)

    assert settings.signing_key_pem() == pem


def test_a_service_without_a_key_refuses_to_start(make_settings: SettingsFactory) -> None:
    with pytest.raises(ValidationError, match="JWT_PRIVATE_KEY"):
        make_settings(jwt_private_key=None, jwt_private_key_path=None)


def test_a_service_told_about_two_keys_refuses_to_start(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory, tmp_path: Path
) -> None:
    # Otherwise the answer to "which key signs the tokens" depends on the order
    # the fields happen to be read in.
    with pytest.raises(ValidationError, match="exactly one"):
        make_settings(
            jwt_private_key=make_private_key_pem(),
            jwt_private_key_path=tmp_path / "key.pem",
        )


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_variable_counts_as_unset(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory, blank: str
) -> None:
    # .env.example lists every variable, including the ones a deployment leaves
    # empty, and the file has to survive being copied and edited.
    settings = make_settings(
        jwt_private_key=make_private_key_pem(),
        jwt_private_key_path=blank,
    )

    assert settings.jwt_private_key_path is None


def test_a_missing_key_file_is_reported_with_its_path(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    settings = make_settings(jwt_private_key_path=tmp_path / "absent.pem")

    with pytest.raises(Exception, match="absent.pem"):
        settings.signing_key_pem()


def test_the_settings_never_print_the_key(
    make_settings: SettingsFactory, make_private_key_pem: KeyFactory
) -> None:
    pem = make_private_key_pem()

    settings = make_settings(jwt_private_key=pem)

    # SecretStr is what stands between a key and a log line that dumps the
    # settings object (docs/CODING_STANDARDS.md section 12).
    assert pem not in repr(settings)
    assert pem not in str(settings)
