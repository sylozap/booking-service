"""Settings must fail loudly and never expose a secret."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from barber_common.config import (
    BaseAppSettings,
    ConfigurationError,
    Environment,
    LogLevel,
)

PASSWORD = "s3cret-in-the-dsn"

REQUIRED_VARIABLES = {
    "SERVICE_NAME": "booking",
    "ENVIRONMENT": "local",
    "DATABASE_DSN": f"postgresql+asyncpg://booking:{PASSWORD}@localhost:5432/booking",
    "REDIS_DSN": "redis://localhost:6379/0",
    "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
}

OPTIONAL_VARIABLES = (
    "LOG_LEVEL",
    "DATABASE_POOL_SIZE",
    "DATABASE_ECHO",
    "OTLP_ENABLED",
    "OTLP_ENDPOINT",
    "HEALTH_CHECK_TIMEOUT_SECONDS",
)


class Settings(BaseAppSettings):
    """Settings under test, detached from any .env file of the developer."""

    model_config = SettingsConfigDict(env_file=None)


def build() -> Settings:
    """Instantiate from the environment, the way a service does at startup."""
    return Settings()


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a complete, valid configuration into the environment."""
    for name, value in REQUIRED_VARIABLES.items():
        monkeypatch.setenv(name, value)
    for name in OPTIONAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_settings_are_built_from_environment_variables(environment: None) -> None:
    settings = build()

    assert settings.service_name == "booking"
    assert settings.environment is Environment.LOCAL
    assert settings.kafka_bootstrap_servers == "localhost:9092"


def test_settings_raise_validation_error_when_required_variable_is_missing(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_DSN")

    with pytest.raises(ValidationError) as error:
        build()

    assert "database_dsn" in str(error.value)


def test_load_reports_the_name_of_the_missing_environment_variable(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS")

    with pytest.raises(ConfigurationError) as error:
        Settings.load()

    assert "KAFKA_BOOTSTRAP_SERVERS" in str(error.value)


def test_settings_reject_a_database_dsn_with_a_synchronous_scheme(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_DSN", "postgresql://booking:pwd@localhost:5432/booking")

    with pytest.raises(ValidationError) as error:
        build()

    assert "postgresql+asyncpg" in str(error.value)


def test_settings_reject_a_redis_dsn_with_an_unexpected_scheme(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_DSN", "http://localhost:6379/0")

    with pytest.raises(ValidationError):
        build()


def test_environment_overrides_the_default_log_level(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = build()

    assert settings.log_level is LogLevel.DEBUG


def test_default_log_level_is_info(environment: None) -> None:
    settings = build()

    assert settings.log_level is LogLevel.INFO


def test_repr_of_settings_hides_the_database_password(environment: None) -> None:
    settings = build()

    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings.database_dsn)
    assert PASSWORD in str(settings.database_dsn.get_secret_value())
