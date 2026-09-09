"""/v1/models — the allowlist, not a passthrough of what vLLM happens to serve.

Reporting the upstream's own list would advertise models a caller is not
permitted to use, and an OpenAI client that picks the first model it is offered
would then send a request the gateway rejects. The allowlist is the contract.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from gateway.auth import Principal, require_principal
from gateway.config import settings

router = APIRouter(prefix="/v1", tags=["inference"])


@router.get("/models")
async def list_models(principal: Principal = Depends(require_principal)) -> dict:
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": created, "owned_by": "bkn301"}
            for m in settings.ALLOWED_MODELS
        ],
    }
