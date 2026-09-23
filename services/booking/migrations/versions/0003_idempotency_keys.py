"""idempotency keys

The table behind the Idempotency-Key header of POST /api/v1/bookings. The DDL
lives in the chassis next to the model it mirrors.

Revision ID: 0003_idempotency_keys
Revises: 0002_booking_tables
"""

from collections.abc import Sequence

from barber_common.db.migrations import create_idempotency_keys, drop_idempotency_keys

revision: str = "0003_idempotency_keys"
down_revision: str | None = "0002_booking_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_idempotency_keys()


def downgrade() -> None:
    drop_idempotency_keys()
