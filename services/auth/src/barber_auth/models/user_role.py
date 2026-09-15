"""What a user is allowed to be, and where."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from barber_common.db.base import Base

__all__ = ["UserRole"]

ROLES = ("client", "master", "salon_admin", "super_admin")


class UserRole(Base):
    """One grant: this user holds this role, globally or in one salon.

    A surrogate key with two partial unique indexes, one for global and one for
    scoped grants, because a primary key cannot contain a null ``salon_id``.
    """

    __tablename__ = "user_roles"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32))
    # Owned by catalog, so there is no foreign key.
    salon_id: Mapped[UUID | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"role IN ({', '.join(repr(role) for role in ROLES)})", name="role"),
        Index(
            "uq_user_roles_user_id_role_global",
            "user_id",
            "role",
            unique=True,
            postgresql_where=text("salon_id IS NULL"),
        ),
        Index(
            "uq_user_roles_user_id_role_salon_id",
            "user_id",
            "role",
            "salon_id",
            unique=True,
            postgresql_where=text("salon_id IS NOT NULL"),
        ),
        Index("ix_user_roles_user_id", "user_id"),
    )
