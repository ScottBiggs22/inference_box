"""/metrics — Prometheus exposition.

UNAUTHENTICATED, ON THE MAIN PORT. That is the deliberate choice, and the reason
gateway/metrics.py folds every label to a bounded vocabulary and carries no
`keyid`: what is exposed here is exposed to anything that can reach the port, so
the protection has to be in what is collected, not in who may read it. Restrict
reachability with a network policy, the way a Prometheus target normally is.
"""
from __future__ import annotations

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.config import settings

router = APIRouter(tags=["ops"])


# Deliberately `def`, not `async def`. generate_latest() walks and formats the
# whole registry, and FastAPI runs a sync route in its threadpool -- so a scrape
# never competes with the SSE relays this process exists to keep moving.
@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus text exposition, or 404 when collection is switched off.

    404 rather than 403 or 503, and the distinction matters on an endpoint with
    no authentication:

      * 403 would confirm to a stranger that a metrics surface exists here and is
        merely turned off, which is information they did not have.
      * 503 means "retry" -- it makes a deliberate configuration choice look like
        an incident, and leaves a scraper flapping against it forever.

    404 is indistinguishable from a route that was never registered, which is
    exactly the impression a disabled endpoint should give.

    The flag is checked HERE rather than by registering the router conditionally.
    Conditional registration would bind the decision to whichever settings object
    existed at create_app() time -- and the test suite builds an app per test --
    as well as changing the OpenAPI schema between environments.
    """
    if not settings.METRICS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    # A bare `str` return would be JSON-encoded by FastAPI: quoted, with escaped
    # newlines, under application/json. That is not parseable as Prometheus text,
    # and is the exact defect the stub's own /metrics shipped with.
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
