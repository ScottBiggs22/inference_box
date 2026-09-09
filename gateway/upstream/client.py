"""HTTP client for the upstream vLLM replicas.

STREAMING IS PASSED THROUGH, NOT BUFFERED
=========================================
At 20 tok/s a 200-token answer takes ten seconds. Buffering it to inspect the
body would mean ten seconds of blank screen for the user, which is the entire
reason streaming is required (PRD §6.3 C2). So SSE frames are relayed as they
arrive and the gateway never assembles the completion.

The consequence is worth stating plainly, because it is a real trade: the
gateway cannot inspect or post-process a streamed answer. Anything that needs
the complete text -- the AI service's `_cap_answer`, `numeric_guard` and
`response_validator` -- has to live at the consuming end, which is where PRD
§6.3 C2 already places that decision.

ROUTING
=======
Phase 1 runs one replica, but the router interface lands now rather than later,
because retro-fitting a replica abstraction into a request path is far more
invasive than starting with one. `pick()` is round-robin today; health-aware
selection and the circuit breaker (PRD §5.3) plug in here without touching the
routes.
"""
from __future__ import annotations

import itertools
import logging

import httpx

from gateway.config import settings

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """The upstream could not serve the request."""


class UpstreamPool:
    def __init__(self, urls: list[str] | None = None) -> None:
        self.urls = list(urls or settings.UPSTREAM_URLS)
        if not self.urls:
            raise ValueError("UPSTREAM_URLS is empty; the gateway has nothing to route to")
        self._rr = itertools.cycle(range(len(self.urls)))
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.UPSTREAM_API_KEY:
            headers["Authorization"] = f"Bearer {settings.UPSTREAM_API_KEY}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=settings.UPSTREAM_CONNECT_TIMEOUT_SEC,
                read=settings.UPSTREAM_READ_TIMEOUT_SEC,
                write=settings.UPSTREAM_CONNECT_TIMEOUT_SEC,
                pool=settings.UPSTREAM_CONNECT_TIMEOUT_SEC,
            ),
        )

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise UpstreamError("upstream pool not started")
        return self._client

    def pick(self) -> str:
        """Choose a replica.

        Round-robin. NOT load-aware yet -- and note PRD §4.6 is explicit that
        load-aware routing belongs here rather than in a readiness probe, because
        a probe that goes red when an instance is BUSY pulls every replica out of
        service at peak traffic.
        """
        return self.urls[next(self._rr)]

    async def health(self) -> dict[str, bool]:
        """Per-replica reachability, for /readyz."""
        out: dict[str, bool] = {}
        for url in self.urls:
            try:
                resp = await self.client.get(f"{url.rstrip('/')}/models")
                out[url] = resp.status_code < 400
            except Exception:
                out[url] = False
        return out


pool = UpstreamPool()
