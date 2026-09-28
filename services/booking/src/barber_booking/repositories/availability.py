"""Free slots of one master, computed by the database.

Raw SQL rather than the ORM, and deliberately so. This is the hot path of the
platform, and the whole calculation -- unfolding local hours into instants,
cutting breaks out of them, laying a grid over what is left and dropping what
overlaps a booking -- is set arithmetic that PostgreSQL does in one pass.
Pulling bookings into Python to subtract intervals there would not scale, and
would put a second implementation of the invariant next to the constraint that
holds it.

The reference implementation lives in ``domain/slots.py`` and
``domain/schedule.py``; a test runs both over the same cases and requires the
same answers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from barber_booking.domain.identifiers import BookingId, MasterId
from barber_booking.models.booking import ACTIVE_BOOKING_STATUSES

__all__ = ["AvailabilityRepository"]


# Every parameter is cast explicitly: asyncpg infers the type of a placeholder
# from its first use, and most of these appear in more than one context.
#
# The shape of the query, in order: the dates asked about; the exceptions of
# each of them; the days that are not days off; the template version in force
# on each day, which is the one with the greatest valid_from; the hours of each
# day, where custom hours replace the template; those hours as instants, joined
# into multiranges so that touching intervals are one shift; breaks subtracted
# from them; a grid of starts over what is left; and finally the starts where
# the service fits inside the working time and the service together with the
# buffer touches no active booking.
_SLOTS = text(
    """
WITH params AS (
    SELECT CAST(:master_id AS uuid)        AS master_id,
           CAST(:timezone AS text)         AS tz,
           CAST(:duration_min AS int)      AS duration_min,
           CAST(:buffer_min AS int)        AS buffer_min,
           CAST(:step_min AS int)          AS step_min,
           CAST(:not_before AS timestamptz) AS not_before,
           CAST(:not_after AS timestamptz)  AS not_after
),
days AS (
    SELECT generate_series(
               CAST(:date_from AS date),
               CAST(:date_to AS date),
               interval '1 day'
           )::date AS day
),
day_exceptions AS (
    SELECT d.day, e.kind, e.start_time, e.end_time
    FROM days d
    JOIN params p ON TRUE
    JOIN schedule_exceptions e
      ON e.master_id = p.master_id
     AND e.effective_on = d.day
),
open_days AS (
    SELECT d.day
    FROM days d
    WHERE NOT EXISTS (
        SELECT 1 FROM day_exceptions x WHERE x.day = d.day AND x.kind = 'day_off'
    )
),
custom_hours AS (
    SELECT x.day, x.start_time, x.end_time
    FROM day_exceptions x
    JOIN open_days o ON o.day = x.day
    WHERE x.kind = 'custom_hours'
),
template_version AS (
    SELECT o.day, max(t.valid_from) AS valid_from
    FROM open_days o
    JOIN params p ON TRUE
    JOIN schedule_templates t
      ON t.master_id = p.master_id
     AND t.weekday = EXTRACT(isodow FROM o.day)::int - 1
     AND o.day >= t.valid_from
     AND (t.valid_to IS NULL OR o.day <= t.valid_to)
    GROUP BY o.day
),
template_hours AS (
    SELECT v.day, t.start_time, t.end_time
    FROM template_version v
    JOIN params p ON TRUE
    JOIN schedule_templates t
      ON t.master_id = p.master_id
     AND t.weekday = EXTRACT(isodow FROM v.day)::int - 1
     AND t.valid_from = v.valid_from
    WHERE NOT EXISTS (SELECT 1 FROM custom_hours c WHERE c.day = v.day)
),
hours AS (
    SELECT day, start_time, end_time FROM custom_hours
    UNION ALL
    SELECT day, start_time, end_time FROM template_hours
),
work AS (
    SELECT h.day,
           range_agg(
               tstzrange((h.day + h.start_time) AT TIME ZONE p.tz,
                         (h.day + h.end_time) AT TIME ZONE p.tz, '[)')
           ) AS spans
    FROM hours h
    JOIN params p ON TRUE
    GROUP BY h.day
),
pauses AS (
    SELECT x.day,
           range_agg(
               tstzrange((x.day + x.start_time) AT TIME ZONE p.tz,
                         (x.day + x.end_time) AT TIME ZONE p.tz, '[)')
           ) AS spans
    FROM day_exceptions x
    JOIN params p ON TRUE
    WHERE x.kind = 'break'
    GROUP BY x.day
),
free AS (
    SELECT w.day,
           unnest(CASE WHEN b.spans IS NULL THEN w.spans ELSE w.spans - b.spans END) AS span
    FROM work w
    LEFT JOIN pauses b ON b.day = w.day
),
grid AS (
    SELECT f.day,
           f.span,
           generate_series(
               lower(f.span),
               upper(f.span) - make_interval(mins => p.duration_min),
               make_interval(mins => p.step_min)
           ) AS slot
    FROM free f
    JOIN params p ON TRUE
)
SELECT g.day, g.slot
FROM grid g
JOIN params p ON TRUE
WHERE tstzrange(g.slot, g.slot + make_interval(mins => p.duration_min), '[)') <@ g.span
  AND g.slot >= p.not_before
  AND g.slot < p.not_after
  AND NOT EXISTS (
      SELECT 1
      FROM bookings b
      WHERE b.master_id = p.master_id
        AND b.status = ANY(CAST(:active_statuses AS text[]))
        AND b.id IS DISTINCT FROM CAST(:ignored_booking_id AS uuid)
        AND b.occupied_range && tstzrange(
                g.slot,
                g.slot + make_interval(mins => p.duration_min + p.buffer_min),
                '[)'
            )
  )
ORDER BY g.day, g.slot
"""
)


class AvailabilityRepository:
    """The free starts of one master, day by day."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def slots(
        self,
        *,
        master_id: MasterId,
        date_from: date,
        date_to: date,
        timezone: str,
        duration_min: int,
        buffer_min: int,
        step_min: int,
        not_before: datetime,
        not_after: datetime,
        ignoring: BookingId | None = None,
    ) -> dict[date, list[datetime]]:
        """Free starts of each date from ``date_from`` to ``date_to``.

        ``not_before`` is usually ``now`` plus the salon's minimum lead, and
        ``not_after`` the end of its booking horizon; both are passed in rather
        than read from the clock here, so a test can place them where it needs
        them. Dates without a single free start are absent from the answer.

        ``ignoring`` leaves one booking out of the busy time: a booking being
        moved does not stand in its own way.
        """
        result = await self._session.execute(
            _SLOTS,
            {
                "master_id": master_id,
                "timezone": timezone,
                "duration_min": duration_min,
                "buffer_min": buffer_min,
                "step_min": step_min,
                "not_before": not_before,
                "not_after": not_after,
                "date_from": date_from,
                "date_to": date_to,
                "active_statuses": list(ACTIVE_BOOKING_STATUSES),
                "ignored_booking_id": ignoring,
            },
        )

        slots: dict[date, list[datetime]] = defaultdict(list)
        for day, slot in result.all():
            slots[day].append(slot)
        return dict(slots)
