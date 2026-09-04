"""The versioned API of the api-gateway service.

Empty for now: the endpoints are added by the tasks of the stage that owns
them. The prefix lives here so every one of them inherits the same path
(docs/CODING_STANDARDS.md section 9).
"""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/api/v1")
