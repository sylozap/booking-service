"""The versioned API of the catalog service.

The prefix lives here so every endpoint inherits the same path
(docs/CODING_STANDARDS.md section 9).
"""

from __future__ import annotations

from fastapi import APIRouter

from barber_catalog.api.v1 import salons

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(salons.router)
