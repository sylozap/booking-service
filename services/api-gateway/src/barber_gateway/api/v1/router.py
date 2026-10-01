"""The versioned API of the api-gateway service.

One endpoint of its own; everything else under ``/api`` is proxied.
"""

from __future__ import annotations

from fastapi import APIRouter

from barber_gateway.api.v1.master_card import router as master_card_router

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(master_card_router)
