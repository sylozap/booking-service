"""Development mailer that writes confirmation letters to a log.

The letter, including its one-time link, is written by a dedicated logger,
``barber_auth.mail``, separate from the service log. :meth:`is_enabled` limits
it to the ``local`` and ``test`` environments, so it never prints a token
anywhere else.
"""

from __future__ import annotations

from urllib.parse import urlencode

from barber_common.config import Environment
from barber_common.logging import get_logger

__all__ = ["DevMailer"]

# Not get_logger(__name__): the name is what separates the outgoing letters
# from the service log in every query that reads them.
_mail_logger = get_logger("barber_auth.mail")
_logger = get_logger(__name__)


class DevMailer:
    """Writes the confirmation letter where a developer can read it."""

    def __init__(self, *, environment: Environment, confirmation_url: str) -> None:
        self._environment = environment
        self._confirmation_url = confirmation_url

    @property
    def is_enabled(self) -> bool:
        """Whether printing letters is acceptable in this environment."""
        return self._environment in (Environment.LOCAL, Environment.TEST)

    def confirmation_link(self, token: str) -> str:
        """The link the letter carries."""
        return f"{self._confirmation_url}?{urlencode({'token': token})}"

    def send_confirmation(self, *, email: str, token: str) -> None:
        """Deliver the confirmation letter.

        Never raises: a mailer that cannot write must not undo a registration
        that is already committed. The user asks for a new link instead.
        """
        if not self.is_enabled:
            _logger.warning(
                "development mailer is wired in outside local and test; letter not sent",
                environment=self._environment.value,
            )
            return

        _mail_logger.info(
            "confirmation letter",
            recipient=email,
            link=self.confirmation_link(token),
        )
