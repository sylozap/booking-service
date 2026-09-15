"""The versioned API of the auth service."""

from __future__ import annotations

from fastapi import APIRouter

from barber_auth.api.v1 import auth, users

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(users.router)
