"""Turning a template and its fields into a message.

A template is a text file in ``templates/``: the first line is the subject, a
blank line follows, the rest is the body. Fields are ``$name`` placeholders of
``string.Template``.

``substitute`` rather than ``safe_substitute``: a field missing from the
payload is an error that names the field, not a message that reaches a client
with ``$start_at`` in it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

from barber_notification.providers.base import Message

__all__ = [
    "SENSITIVE_TEMPLATES",
    "TemplateFieldMissing",
    "UnknownTemplate",
    "render",
]

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Templates whose text carries a credential. Their text is never logged outside
# local and test, and their fields are cleared from the journal once sent.
SENSITIVE_TEMPLATES = frozenset({"email_confirmation"})


class UnknownTemplate(LookupError):
    """No template of this name ships with the service."""


class TemplateFieldMissing(KeyError):
    """The payload lacks a field the template needs."""

    def __init__(self, template: str, field: str) -> None:
        super().__init__(field)
        self.template = template
        self.field = field

    def __str__(self) -> str:
        return f"template {self.template!r} needs field {self.field!r}, and the payload lacks it"


def render(template: str, fields: dict[str, object]) -> Message:
    """The message this template makes from these fields."""
    subject, body = _load(template)
    return Message(
        template=template,
        subject=_substitute(template, subject, fields),
        body=_substitute(template, body, fields),
        is_sensitive=template in SENSITIVE_TEMPLATES,
    )


def _substitute(name: str, text: Template, fields: dict[str, object]) -> str:
    try:
        return text.substitute(fields)
    except KeyError as error:
        raise TemplateFieldMissing(name, str(error.args[0])) from error


@cache
def _load(name: str) -> tuple[Template, Template]:
    """Read a template once per process; they ship with the code."""
    # The name comes from code, never from a message, but a path is still
    # built from it: refuse anything that is not a plain file name.
    if not name.replace("_", "").isalnum():
        raise UnknownTemplate(name)
    path = _TEMPLATES_DIR / f"{name}.txt"
    if not path.is_file():
        raise UnknownTemplate(name)

    subject, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    return Template(subject.strip()), Template(body.strip() + "\n")
