"""Probe endpoints. The design and its reasoning are in docs/PROBE_CONTRACT.md.

The short version, because getting this wrong is expensive:

  /healthz  — liveness. Is this process's event loop alive? Nothing more.
              NEVER a test generation: a restart costs 30-60s of model reload,
              so liveness is the last resort, not a quality check.

  /readyz   — readiness. Is this instance BROKEN? Explicitly not "is it busy".
              Queue depth is deliberately absent: if readiness went red when an
              instance were merely loaded, Kubernetes would pull every replica
              out of the endpoint list during a traffic spike and the service
              would collapse at exactly the moment it was needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Response, status

from gateway.upstream.client import pool

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness. 200 whenever this process can serve a request at all."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    """Readiness. Red only when no upstream replica is reachable.

    Note the aggregation: ANY reachable replica means ready. The gateway can
    still serve if one of several vLLM instances is down -- that is what having
    replicas is for -- so a per-replica failure belongs in the router's health
    tracking and in an alert, not in this endpoint's verdict.
    """
    try:
        replicas = await pool.health()
    except Exception as e:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "reason": f"{type(e).__name__}", "upstreams": {}}

    if not any(replicas.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "upstreams": replicas}

    return {"status": "ok", "upstreams": replicas}
