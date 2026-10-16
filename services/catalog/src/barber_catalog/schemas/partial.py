"""Fields of a partial update that may be left out but not cleared."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, TypeVar

from pydantic import BeforeValidator

__all__ = ["Kept"]

T = TypeVar("T")


def _not_null(value: object) -> object:
    if value is None:
        raise ValueError("may be left out, but not null: the field has no empty value")
    return value


# A field a change may leave out -- absent means untouched -- but not set to
# ``null``: the column behind it has no empty value. The default ``None``
# stands for "absent"; pydantic does not validate defaults, so it never meets
# the validator, while an explicit ``null`` does and is refused with ``422``
# instead of reaching the database as a NOT NULL violation. The schema is that
# of ``T`` alone, without ``null``, and the constraints of the field apply to
# ``T``. For the type checker the value is ``T | None``, which is what the code
# reading an absent field gets.
if TYPE_CHECKING:
    Kept = Annotated[T | None, ...]
else:
    Kept = Annotated[T, BeforeValidator(_not_null)]
