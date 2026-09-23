"""A master's working time: the weekly template, its exceptions, the buffer.

Open to a `super_admin`, to a `salon_admin` of the master's salon and to the
master themselves. Times are the salon's local wall-clock time.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from barber_booking.api.v1.dependencies import (
    AddScheduleExceptionScenario,
    ListScheduleExceptionsScenario,
    ReadWeeklyScheduleScenario,
    RemoveScheduleExceptionScenario,
    ReplaceWeeklyScheduleScenario,
    UpdateMasterSettingsScenario,
)
from barber_booking.domain.identifiers import ExceptionId, MasterId
from barber_booking.schemas.schedule import (
    MasterSettingsResponse,
    MasterSettingsUpdateRequest,
    ScheduleExceptionList,
    ScheduleExceptionRequest,
    ScheduleExceptionResponse,
    WeeklyScheduleRequest,
    WeeklyScheduleResponse,
)
from barber_booking.services.authorization import SCHEDULE_MANAGERS
from barber_common.auth import Principal, require_roles

__all__ = ["router"]

router = APIRouter(prefix="/masters", tags=["schedule"])

ScheduleManager = Annotated[Principal, Depends(require_roles(*SCHEDULE_MANAGERS))]

_REFUSED: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {
        "description": "Not this master, nor an administrator of the salon"
    },
    status.HTTP_404_NOT_FOUND: {"description": "No such master"},
}


@router.put(
    "/{master_id}/schedule",
    summary="Replace the weekly schedule from a date on",
    responses={
        **_REFUSED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Intervals of one weekday overlap, or valid_from is in the past"
        },
    },
)
async def replace_weekly_schedule(
    master_id: UUID,
    body: WeeklyScheduleRequest,
    caller: ScheduleManager,
    scenario: ReplaceWeeklyScheduleScenario,
) -> WeeklyScheduleResponse:
    """Put a new weekly template in force from `valid_from` (today by default).

    The body is the whole week: what is absent is not worked. The version in
    force is closed the day before `valid_from`, versions planned after it are
    dropped, and exceptions of single dates are left as they are.

    **Existing bookings are not cancelled**, even when the new hours leave them
    outside working time. The administrator sees the conflict and settles it
    with the client.
    """
    return await scenario.execute(caller=caller, master_id=MasterId(master_id), body=body)


@router.get(
    "/{master_id}/schedule",
    summary="Read the weekly schedule",
    responses=_REFUSED,
)
async def read_weekly_schedule(
    master_id: UUID,
    caller: ScheduleManager,
    scenario: ReadWeeklyScheduleScenario,
) -> WeeklyScheduleResponse:
    """The template in force today, followed by every version planned after it."""
    return await scenario.execute(caller=caller, master_id=MasterId(master_id))


@router.post(
    "/{master_id}/schedule/exceptions",
    status_code=status.HTTP_201_CREATED,
    summary="Add a day off, other hours or a break on one date",
    responses={
        **_REFUSED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The date is in the past, or contradicts another exception of it"
        },
    },
)
async def add_schedule_exception(
    master_id: UUID,
    body: ScheduleExceptionRequest,
    caller: ScheduleManager,
    scenario: AddScheduleExceptionScenario,
) -> ScheduleExceptionResponse:
    """Change the schedule of one date.

    `day_off` stands alone on its date. `custom_hours` replace the template for
    the date and may be several; `break` is cut out of whatever hours remain.
    Exceptions of one kind may not overlap. **Bookings on the date are not
    cancelled.**
    """
    return await scenario.execute(caller=caller, master_id=MasterId(master_id), body=body)


@router.get(
    "/{master_id}/schedule/exceptions",
    summary="List the exceptions of a window of dates",
    responses=_REFUSED,
)
async def list_schedule_exceptions(
    master_id: UUID,
    caller: ScheduleManager,
    scenario: ListScheduleExceptionsScenario,
    date_from: Annotated[date | None, Query(description="Today in the salon if absent.")] = None,
    date_to: Annotated[
        date | None, Query(description="Inclusive. 92 days after date_from if absent.")
    ] = None,
) -> ScheduleExceptionList:
    """Exceptions ordered by date. The window is at most a year long."""
    return await scenario.execute(
        caller=caller, master_id=MasterId(master_id), date_from=date_from, date_to=date_to
    )


@router.delete(
    "/{master_id}/schedule/exceptions/{exception_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw an exception",
    responses={
        **_REFUSED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "The date is in the past"},
    },
)
async def remove_schedule_exception(
    master_id: UUID,
    exception_id: UUID,
    caller: ScheduleManager,
    scenario: RemoveScheduleExceptionScenario,
) -> None:
    """Withdraw an exception of today or a later date. The past stays as it was."""
    await scenario.execute(
        caller=caller, master_id=MasterId(master_id), exception_id=ExceptionId(exception_id)
    )


@router.patch(
    "/{master_id}/settings",
    summary="Change the buffer after each booking",
    responses=_REFUSED,
)
async def update_master_settings(
    master_id: UUID,
    body: MasterSettingsUpdateRequest,
    caller: ScheduleManager,
    scenario: UpdateMasterSettingsScenario,
) -> MasterSettingsResponse:
    """Set how long the master stays occupied after each booking.

    Applies to bookings made from now on; existing bookings keep the buffer
    they were made with.
    """
    return await scenario.execute(caller=caller, master_id=MasterId(master_id), body=body)
