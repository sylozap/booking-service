"""cancel deadline kept with the booking

The salon's cancellation deadline, copied into the booking when it is made,
beside the rest of the snapshot. A cancellation then needs nothing from
``catalog``: it works when ``catalog`` is down, when the master no longer
offers the service, and on the terms the client booked under.

Rows written before this revision take the platform default of 240 minutes.
The server default stays in place, so a version of the code that does not know
the column yet keeps inserting while this one is being rolled out.

Revision ID: 0004_booking_cancel_deadline
Revises: 0003_idempotency_keys
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_booking_cancel_deadline"
down_revision: str | None = "0003_idempotency_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_CANCEL_DEADLINE_MIN = 240


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column(
            "cancel_deadline_min",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_CANCEL_DEADLINE_MIN)),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "cancel_deadline_min_not_negative", "bookings", "cancel_deadline_min >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("cancel_deadline_min_not_negative", "bookings", type_="check")
    op.drop_column("bookings", "cancel_deadline_min")
