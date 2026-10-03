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

    # SecretStr keeps the key out of a log line that dumps the settings.
    assert pem not in repr(settings)
    assert pem not in str(settings)


# --- the first administrator --------------------------------------------------

BOOTSTRAP = {
    "bootstrap_admin_email": "admin@barber.example",
    "bootstrap_admin_phone": "+79990000001",
    "bootstrap_admin_password": "admin-password-of-the-stand-7",
}


def test_an_administrator_is_given_by_three_settings(make_settings: SettingsFactory) -> None:
    settings = make_settings(**BOOTSTRAP)

    admin = settings.bootstrap_admin()

    assert admin is not None
    assert (admin.email, admin.phone) == ("admin@barber.example", "+79990000001")
    assert admin.password.get_secret_value() == "admin-password-of-the-stand-7"


def test_no_administrator_by_default(make_settings: SettingsFactory) -> None:
    assert make_settings().bootstrap_admin() is None


def test_empty_variables_name_no_administrator(make_settings: SettingsFactory) -> None:
    settings = make_settings(**dict.fromkeys(BOOTSTRAP, ""))

    assert settings.bootstrap_admin() is None


@pytest.mark.parametrize("missing", sorted(BOOTSTRAP))
def test_an_administrator_described_by_half_refuses_to_start(
    make_settings: SettingsFactory, missing: str
) -> None:
    partial = {key: value for key, value in BOOTSTRAP.items() if key != missing}

    with pytest.raises(ValidationError, match="set together or not at all"):
        make_settings(**partial)


def test_the_password_does_not_show_in_the_settings(make_settings: SettingsFactory) -> None:
    settings = make_settings(**BOOTSTRAP)

    assert "admin-password-of-the-stand-7" not in repr(settings)


def test_an_administrator_the_login_form_would_refuse_refuses_to_start(
    make_settings: SettingsFactory,
) -> None:
    # .local is a reserved name: the address would be stored, and the login
    # form would refuse it forever after.
    with pytest.raises(ValidationError, match="bootstrap_admin_email"):
        make_settings(**{**BOOTSTRAP, "bootstrap_admin_email": "admin@barber.local"})
