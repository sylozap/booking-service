"""The stand-in for an email provider, until ``notification`` takes over.

T1.4 needs the confirmation link to reach a developer, and the service that
sends letters does not exist before T5.4. So the letter is written to the log
of its own channel and the flow can be exercised end to end.

**About the token in the output.** docs/CODING_STANDARDS.md section 11 forbids
one-time confirmation tokens in the logs, and that rule is about the service
log -- the records that go to Loki and are read by everyone with access to it.
What this class writes is the letter itself: it is the delivery channel, the
same bytes an SMTP server would carry, and the link is the entire content. The
distinction is kept visible by a logger of its own, ``barber_auth.mail``, and
by :meth:`is_enabled`: outside ``local`` and ``test`` the mailer refuses to
print anything, so a production deployment cannot leak a token through it even
if it is left wired in by mistake.

From T5.4 ``auth`` only publishes ``user.email_confirmation_requested`` and
this module is deleted.
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
