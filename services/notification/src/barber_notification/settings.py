"""Settings of the notification service."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from barber_common.config import BaseAppSettings

__all__ = ["ALEMBIC_INI", "NotificationSettings"]

# services/notification/alembic.ini, two directories above the package. The startup
# check and the migration Job read the same file, so they cannot disagree about
# which revision is the head.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class NotificationSettings(BaseAppSettings):
    """Everything the chassis needs, plus what only this service has."""

    service_name: str = "notification"

    # --- delivery -----------------------------------------------------------
    # How often the worker looks for due notifications when it found none, and
    # how many it takes per pass.
    delivery_interval_seconds: float = 1.0
    delivery_batch_size: int = 20
    # How long a notification taken for sending is left alone. A worker that
    # dies mid-send leaves it to be taken again after this.
    delivery_lease_seconds: int = 60
    # Attempts on a temporary failure before the notification is given up on,
    # and the first pause between them; each next one doubles.
    delivery_max_attempts: int = 3
    delivery_retry_delay_seconds: float = 30.0

    # --- email confirmation -------------------------------------------------
    # Where the confirmation link points: the page that reads the token out of
    # the query string and posts it to /api/v1/auth/confirm-email.
    email_confirmation_url: str = "http://localhost:8080/confirm-email"

    # --- telegram -----------------------------------------------------------
    # Optional on purpose: without a token the telegram channel is delivered to
    # the log and linking is refused, but the service starts and works.
    telegram_bot_token: SecretStr | None = None
    # The bot's @name without the @, for the t.me link a user follows.
    telegram_bot_username: str | None = None
    telegram_api_url: str = "https://api.telegram.org"
    # One Bot API call. Long polling waits longer, on its own timeout below.
    telegram_timeout_seconds: float = 5.0
    # How long one getUpdates call waits for a message before answering empty.
    telegram_poll_timeout_seconds: int = 25
    # How long a code for linking a chat stays valid.
    telegram_link_code_ttl_minutes: int = 15

    @property
    def telegram_enabled(self) -> bool:
        """Whether a bot is configured, and so the channel really goes to Telegram."""
        # An empty variable is how compose passes "not set", so it counts as absent.
        has_token = (
            self.telegram_bot_token is not None and self.telegram_bot_token.get_secret_value() != ""
        )
        return has_token and bool(self.telegram_bot_username)
