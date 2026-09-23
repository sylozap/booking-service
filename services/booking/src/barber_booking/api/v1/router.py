"""The versioned API of the booking service."""

from __future__ import annotations

from fastapi import APIRouter

from barber_booking.api.v1 import availability, bookings, schedule

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(availability.router)
router.include_router(bookings.router)
router.include_router(schedule.router)
