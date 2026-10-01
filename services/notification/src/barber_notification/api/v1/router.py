"""The versioned API of the notification service."""

from __future__ import annotations

from fastapi import APIRouter

from barber_notification.api.v1 import link_telegram, preferences

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(link_telegram.router)
router.include_router(preferences.router)
