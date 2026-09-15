"""The versioned API of the booking service."""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
