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

import logging
import time

import httpx

from gateway.config import settings
from gateway.upstream.breaker import BreakerRegistry, FailureKind

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """The upstream could not serve the request."""


class AllReplicasOpen(UpstreamError):
    """Every replica's breaker is open, so there is nowhere to route.

    With one replica in production this is the COMMON case rather than a corner
    one, and the route turns it into a fast 503 rather than a 120s hang. See
    UpstreamPool.pick.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__("all replicas open")
        self.retry_after = retry_after


def classify(exc: Exception | None, status_code: int | None) -> FailureKind | None:
    """Map an outcome onto the breaker's failure vocabulary.

    Returns None for a success. The distinctions are not cosmetic -- see WEIGHTS
    in breaker.py for what each one is worth and why.
    """
    if exc is not None:
        if isinstance(exc, httpx.ReadTimeout):
            return FailureKind.READ_TIMEOUT
        # Everything else reaching here failed before or during connect. Treated
        # alike because the consequence is the same: nothing reached the GPU.
        return FailureKind.CONNECT
    if status_code is None:
        return None
    if status_code == 429:
        return FailureKind.THROTTLED
    if status_code >= 500:
        return FailureKind.SERVER_ERROR
    if status_code >= 400:
        return FailureKind.CLIENT_ERROR
    return None


class UpstreamPool:
    def __init__(self, urls: list[str] | None = None) -> None:
        self.breakers = BreakerRegistry()
        self._next = 0
        self._client: httpx.AsyncClient | None = None
        self.urls: list[str] = []
        self.set_urls(list(urls or settings.UPSTREAM_URLS))

    def set_urls(self, urls: list[str]) -> None:
        """Replace the replica list, rebuilding everything keyed to it.

        This exists because assigning to `pool.urls` directly -- which the test
        suite did -- left the old round-robin cycle indexing a list of a
        different length, and would now also leave breaker state attached to
        replicas that no longer exist.
        """
        if not urls:
            raise ValueError("UPSTREAM_URLS is empty; the gateway has nothing to route to")
        self.urls = list(urls)
        self._next = 0
        self.breakers.reset(self.urls)

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

    def pick(self, *, exclude: str | None = None) -> str:
        """Choose a replica whose breaker is closed.

        Round-robin over the healthy ones. An integer counter rather than
        itertools.cycle, because a cycle cannot express "skip the open ones" and
        breaks outright when the replica list changes length.

        WHEN EVERY REPLICA IS OPEN, THIS RAISES. IT DOES NOT ROUTE ANYWAY.
        ----------------------------------------------------------------
        That is the load-bearing decision in the whole breaker, and with a single
        replica "all open" is the common case rather than a corner one -- so it
        deserves its reasoning written down.

        With one replica there is no elsewhere to route to, so the breaker's
        entire value reduces to exactly one thing: converting a 120-second hang
        into an immediate 503. That is not a small thing. The caller is the C#
        API, and a client that blocks for two minutes per request during an
        outage exhausts its own connection and thread pools and propagates the
        outage upward. PRD §6.3 C3 wants the consuming app to degrade to RAG-only
        rather than hang -- and it can only enter that mode if this answers fast.
        Routing anyway would deliver none of the breaker's value and all of its
        complexity.

        The honest cost: a false trip is now a total outage rather than a
        degradation. That is why 429 carries no weight, why one success resets the
        score completely, why reclose is automatic and unattended, and why
        BREAKER_ENABLED exists.

        What deliberately is NOT here is a last-ditch trial request when
        everything is open. It would hand the 120s hang back to one unlucky
        caller -- the exact cost this is removing -- and the prober already
        answers "is it back yet" more cheaply and with no victim.
        """
        if not settings.BREAKER_ENABLED:
            self._next = (self._next + 1) % len(self.urls)
            return self.urls[self._next]

        now = time.monotonic()
        candidates = [
            u for u in self.urls
            if self.breakers.get(u).allows() and u != exclude
        ]
        if not candidates:
            # Fall back to any closed replica before giving up, so `exclude`
            # cannot manufacture an outage when a retry has nowhere else to go.
            candidates = [u for u in self.urls if self.breakers.get(u).allows()]
        if not candidates:
            wait = min(
                (self.breakers.get(u).seconds_until_probe(now) for u in self.urls),
                default=settings.BREAKER_OPEN_SEC,
            )
            raise AllReplicasOpen(retry_after=wait)

        self._next = (self._next + 1) % len(candidates)
        return candidates[self._next]

    def record(self, url: str, *, exc: Exception | None = None,
               status_code: int | None = None) -> None:
        """Fold one attempt's outcome into that replica's breaker."""
        kind = classify(exc, status_code)
        breaker = self.breakers.get(url)
        now = time.monotonic()
        if kind is None:
            breaker.record_success(now)
        else:
            breaker.record_failure(kind, now)

    async def health(self) -> dict[str, bool]:
        """Per-replica reachability, for /readyz.

        The explicit timeout is not decoration. Without it this inherits the
        pool's 120s READ timeout, so a replica that accepts a connection and then
        never answers would hang /readyz for two minutes -- on the endpoint whose
        entire job is to answer "is this instance broken" promptly.
        """
        out: dict[str, bool] = {}
        for url in self.urls:
            try:
                resp = await self.client.get(
                    f"{url.rstrip('/')}/models",
                    timeout=settings.UPSTREAM_PROBE_TIMEOUT_SEC,
                )
                out[url] = resp.status_code < 400
            except Exception:
                out[url] = False
        return out

    async def probe(self, url: str) -> bool:
        """One reclose probe against a single replica."""
        try:
            resp = await self.client.get(
                f"{url.rstrip('/')}/models",
                timeout=settings.UPSTREAM_PROBE_TIMEOUT_SEC,
            )
            return resp.status_code < 400
        except Exception:
            return False


pool = UpstreamPool()
