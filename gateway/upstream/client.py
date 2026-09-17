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

mTLS TO vLLM
============
One `httpx.AsyncClient` (`start()`, below) backs every outbound call this
process makes -- chat completions, health/reclose probes, and the /metrics
scraper in `metrics_scraper.py`, which is handed `pool.client` directly. So
wiring TLS in one place (PRD §5.4 item 1, `_upstream_ssl_context()`) covers all
three; there is no second HTTP client anywhere in this process that could
bypass it. See `gateway/config.py`'s `UPSTREAM_CLIENT_CERT`/
`UPSTREAM_CA_BUNDLE_PATH` for the env-var surface, `_upstream_ssl_context`'s own
docstring for why the TLS context is built by hand rather than through httpx's
`cert=`/`verify=<str>` params, and PRD §4.8 for why this is scoped to code +
config + a self-signed-cert test rather than something validated on a laptop:
the security review has to run against the real deployment path.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time

import httpx

from gateway import metrics as gw_metrics
from gateway.config import settings
from gateway.upstream.breaker import BreakerRegistry, FailureKind

logger = logging.getLogger(__name__)


def _upstream_ssl_context() -> ssl.SSLContext | bool:
    """The TLS config for the upstream connection: True (default trust store,
    no client cert) if mTLS is not configured, else a real ssl.SSLContext.

    NOT httpx's own `cert=`/`verify=<str>` convenience params, and this is not
    a style choice: `httpx._config.create_ssl_context` (0.28.1) returns for a
    string `verify` BEFORE it ever reaches the code that applies `cert=`, so
    passing both together -- which is exactly `UPSTREAM_CA_BUNDLE_PATH` plus
    `UPSTREAM_CLIENT_CERT` set, the realistic private-CA mTLS case -- silently
    sends no client certificate at all. No error, no warning about the
    cert being dropped; the handshake just completes without one. Caught by
    tests/test_mtls.py's positive-path test failing with
    `TLSV13_ALERT_CERTIFICATE_REQUIRED` against a server that demands one.
    Building the context by hand is what httpx's own deprecation message for
    `cert=` recommends.
    """
    ca_bundle = settings.UPSTREAM_CA_BUNDLE_PATH
    client_cert = settings.UPSTREAM_CLIENT_CERT
    if not ca_bundle and client_cert is None:
        return True
    ctx = ssl.create_default_context(cafile=ca_bundle or None)
    if client_cert is not None:
        ctx.load_cert_chain(*client_cert)
    return ctx

# BOUND: 1 RETRY, 2 ATTEMPTS TOTAL.
#
# A retried request that already reached the GPU has burned one of ~5 decode
# slots on the card. In a 503 storm, a policy that retries more than once turns
# 5 users into far more than 10 upstream requests, which is how a retry policy
# causes the outage it exists to paper over.
MAX_ATTEMPTS = 2

# Status codes safe to retry. DELIBERATELY NARROWER than "5xx", and a DIFFERENT
# taxonomy from FailureKind/WEIGHTS in breaker.py -- classify() below folds 500
# and 503 into the same SERVER_ERROR bucket for TRIP purposes, because both
# indicate a replica worth the same suspicion. Retrying them is not equally
# safe: 502/503/504 are what an overloaded, restarting, or wedged-and-timing-out
# proxy actually returns, and doubling a request against those is the retry's
# whole reason to exist. 500 more often reflects a genuine error the REQUEST
# itself triggered -- a shape vLLM rejected -- and retrying that only doubles a
# guaranteed failure. 429 is included: it is the designed path, since the
# breaker's WEIGHTS give it weight 0 specifically so a replica asking for less
# load is retried rather than treated as broken.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

# Full jitter, capped low: the backoff exists to avoid a correlated retry
# immediately after a shared 503, not to be a meaningful wait relative to a
# 10s+ generation. Retry-After is honoured up to the same cap -- an upstream
# asking for 30s should produce a 503 to the caller, not a 30s hang.
_MAX_RETRY_DELAY_SEC = 2.0
_JITTER_BASE_SEC = 0.25


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


class UpstreamUnreachable(UpstreamError):
    """Every attempt failed before any response arrived.

    Raised only after MAX_ATTEMPTS is exhausted with nothing but connect-level
    exceptions -- a read timeout is deliberately excluded from what gets
    retried (see _should_retry), so a single read timeout reaches here after
    exactly one attempt, which is correct: the request may still be running on
    the GPU, and retrying it would double decode load on a replica that is
    already not answering.
    """

    def __init__(self, last_exc: Exception, upstream: str) -> None:
        super().__init__(f"upstream unreachable: {last_exc}")
        self.last_exc = last_exc
        self.upstream = upstream


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


def _should_retry(outcome: httpx.Response | Exception) -> bool:
    """Is this outcome safe to retry? A DIFFERENT question from classify()'s.

    classify() answers "how much does this count against the breaker"; this
    answers "did anything reach the GPU, and would sending it again be safe".
    A read timeout fails both: something may already be running, so it is
    deliberately excluded even though CONNECT (a cousin failure that reached
    nothing) is retried.
    """
    if isinstance(outcome, Exception):
        return classify(outcome, None) is FailureKind.CONNECT
    return outcome.status_code in _RETRYABLE_STATUS


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Honour Retry-After if the upstream sent one; otherwise full jitter.

    Retry-After is clamped to the same cap as the jitter -- an upstream asking
    for 30s should produce a 503 to the caller, not a 30s hang.
    """
    if response is not None:
        raw = response.headers.get("retry-after")
        if raw is not None:
            try:
                return min(float(raw), _MAX_RETRY_DELAY_SEC)
            except ValueError:
                pass
    # Backoff jitter, not a security token -- S311 does not apply.
    return random.uniform(  # noqa: S311
        0, min(_JITTER_BASE_SEC * (2 ** attempt), _MAX_RETRY_DELAY_SEC)
    )


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
            # mTLS to vLLM (PRD §5.4 item 1). See _upstream_ssl_context's
            # docstring for why this is a hand-built ssl.SSLContext rather
            # than httpx's own cert=/verify=<str> params. Inert for a plain
            # http:// UPSTREAM_URLS entry -- Phase 1's stub is unaffected.
            verify=_upstream_ssl_context(),
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

    def _label(self, url: str) -> str:
        """Replicas are labelled by index, never by URL, in every metric.

        /metrics is unauthenticated, and these are private-subnet vLLM
        addresses -- the same class of leak that keeps `keyid` out of the
        request-path labels, topology instead of tenancy.
        """
        try:
            return str(self.urls.index(url))
        except ValueError:
            return "unknown"

    async def _send_and_record(
        self, upstream: str, url: str, body: dict, *, stream: bool,
    ) -> httpx.Response | Exception:
        """One attempt against one replica: send, then record into both the
        breaker and /metrics. Returns the response OR the exception -- never
        raises -- so the retry loop can inspect either uniformly.
        """
        try:
            if stream:
                request = self.client.build_request("POST", url, json=body)
                resp = await self.client.send(request, stream=True)
            else:
                resp = await self.client.post(url, json=body)
        except Exception as e:
            self.record(upstream, exc=e)
            gw_metrics.upstream_requests_total.labels(
                upstream=self._label(upstream), outcome="unreachable").inc()
            return e
        self.record(upstream, status_code=resp.status_code)
        gw_metrics.upstream_requests_total.labels(
            upstream=self._label(upstream),
            outcome="ok" if resp.status_code < 400 else "http_error",
        ).inc()
        return resp

    async def _with_retry(
        self, path: str, body: dict, *, stream: bool,
    ) -> tuple[str, httpx.Response]:
        """pick() + attempt + bounded retry, in one place.

        The point of owning this here rather than in the route is that a future
        call site cannot bypass the breaker or the retry bound by hand-rolling
        its own `pool.client.post(...)`.

        Retrying is safe here specifically because, for the streaming case,
        `stream=True` gives status and headers WITHOUT reading the body -- so a
        503 is visible and retryable before a single byte has reached the
        caller. Once this returns, retrying is no longer possible: the caller
        owns the response from here on, streamed or not.
        """
        exclude: str | None = None
        outcome: httpx.Response | Exception | None = None
        upstream = ""
        for attempt in range(MAX_ATTEMPTS):
            upstream = self.pick(exclude=exclude)  # AllReplicasOpen propagates
            url = f"{upstream.rstrip('/')}{path}"
            outcome = await self._send_and_record(upstream, url, body, stream=stream)

            if not isinstance(outcome, Exception) and outcome.status_code < 400:
                return upstream, outcome
            if attempt + 1 >= MAX_ATTEMPTS or not _should_retry(outcome):
                break

            if stream and not isinstance(outcome, Exception):
                # This attempt's response is being discarded in favour of a
                # retry. It was never read; close it explicitly or the
                # connection never returns to the pool.
                await outcome.aclose()
            await asyncio.sleep(
                _retry_delay(attempt, None if isinstance(outcome, Exception) else outcome)
            )
            exclude = upstream

        if isinstance(outcome, Exception):
            raise UpstreamUnreachable(outcome, upstream)
        return upstream, outcome

    async def request(self, path: str, *, json: dict) -> tuple[str, httpx.Response]:
        """POST with pick + bounded retry + breaker/metrics recording owned here.

        Returns (upstream_url, response) -- the caller decides what a >=400
        status means; only connect-level exhaustion raises (UpstreamUnreachable),
        and an all-open pool raises AllReplicasOpen from pick().
        """
        return await self._with_retry(path, json, stream=False)

    async def open_stream(self, path: str, *, json: dict) -> tuple[str, httpx.Response]:
        """Like request(), but returns an UNREAD streaming response.

        The caller owns `response.aclose()` from here on -- see the ownership
        note in routes/chat.py._stream.
        """
        return await self._with_retry(path, json, stream=True)

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
