"""A booking as every endpoint of ``/api/v1/bookings`` shows it."""

from __future__ import annotations

from barber_booking.domain.booking import Booking
from barber_booking.schemas.bookings import BookingResponse

__all__ = ["booking_response"]


def booking_response(booking: Booking) -> BookingResponse:
    """The API view of a stored booking.

    Only a stored one: ``created_at`` comes from the database, and a booking
    without it has not been written yet.
    """
    if booking.created_at is None:
        raise ValueError("a booking is shown only once it is stored")
    return BookingResponse(
        id=booking.id,
        salon_id=booking.salon_id,
        master_id=booking.master_id,
        client_user_id=booking.client_user_id,
        service_id=booking.service.service_id,
        service_name=booking.service.name,
        price=booking.service.price,
        currency=booking.service.currency,
        duration_min=booking.service.duration_min,
        buffer_min=booking.buffer_min,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status.value,
        reminder_at=booking.reminder_at,
        cancelled_at=booking.cancelled_at,
        cancel_reason=booking.cancel_reason,
        created_by=booking.created_by,
        created_at=booking.created_at,
    )
