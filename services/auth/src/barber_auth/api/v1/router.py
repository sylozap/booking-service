"""The versioned API of the auth service.

The prefix lives here so every endpoint inherits the same path
(docs/CODING_STANDARDS.md section 9).
"""

from __future__ import annotations

from fastapi import APIRouter

from barber_auth.api.v1 import auth

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
