"""The versioned API of the catalog service.

The prefix lives here so every endpoint inherits the same path
(docs/CODING_STANDARDS.md section 9).
"""

from __future__ import annotations

from fastapi import APIRouter

from barber_catalog.api.v1 import masters, salons, services

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
router.include_router(salons.router)
router.include_router(masters.router)
router.include_router(masters.salon_masters_router)
router.include_router(services.router)
router.include_router(services.salon_services_router)
