"""What a user is told about their account: the confirmation letter."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from barber_common.events.users import UserEmailConfirmationRequested
from barber_notification.services.booking_messages import TemplateAndFields, format_moment

__all__ = ["confirmation_link", "confirmation_message"]


def confirmation_link(confirmation_url: str, token: str) -> str:
    """The link the letter carries: the page, with the token in the query."""
    return f"{confirmation_url}?{urlencode({'token': token})}"


def confirmation_message(
    event: UserEmailConfirmationRequested, *, confirmation_url: str
) -> TemplateAndFields:
    """The letter with the one-time link. The token is in it and nowhere else."""
    return "email_confirmation", {
        "link": confirmation_link(confirmation_url, event.token),
        "expires_at": _expiry(event.expires_at),
    }


def _expiry(raw: str) -> str:
    """When the link stops working, in UTC; as sent if it cannot be read."""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if moment.tzinfo is None:
        return raw
    return format_moment(moment, None)
