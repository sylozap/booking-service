"""Requests and responses of linking Telegram."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = ["TelegramLinkCodeResponse"]


class TelegramLinkCodeResponse(BaseModel):
    """A code to send to the bot, and the link that sends it."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="One-time code. The bot expects it as `/start <code>`.")
    link: str = Field(description="`https://t.me/<bot>?start=<code>`: opens the bot with the code")
    expires_at: AwareDatetime = Field(description="After this moment the code links nothing")
