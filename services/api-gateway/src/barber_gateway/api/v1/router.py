"""The versioned API of the api-gateway service."""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
