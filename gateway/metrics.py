"""Prometheus instrumentation for the gateway.

WHY EVERY METRIC IS A MODULE-LEVEL CONSTANT
===========================================
`prometheus_client`'s default REGISTRY is a process global, and registering the
same metric name into it twice raises `Duplicated timeseries`. `create_app()` is
called once per test by `tests/conftest.py::client`, so a metric built inside the
factory blows up on the second test. Module import is idempotent via
`sys.modules`; app construction is not. Nothing here may move into a function.

CARDINALITY IS A SECURITY PROPERTY HERE, NOT A TIDINESS ONE
===========================================================
`/metrics` is unauthenticated (see gateway/routes/metrics.py), so every label
value is published to whoever can reach the port. Two consequences, both
enforced in code below rather than by convention:

  * `keyid` appears nowhere. It is unbounded -- one series per tenant, forever --
    and it puts tenant identity on a surface with no access control, which the
    audit log deliberately does have.
  * Any label sourced from a request body is folded to a fixed vocabulary. A
    caller can put anything in `model`, and `chat.py` records the caller's string
    when rejecting an unknown one. Unbounded there is a memory leak an
    authenticated tenant can drive at will.

The same reasoning removes the upstream URL: replicas are labelled by index, so
internal vLLM addresses are not published to an anonymous scraper.

BUCKETS
=======
vLLM's own `http_request_duration_seconds` is the cautionary tale: its buckets
stop at 1.0s and, in the Phase 1 capture, 168 of 168 chat requests landed in
`+Inf`. It measures nothing. Ours are sized from
`results/phase1-vastai-a10/audit.jsonl` (n=78): min 85ms, p50 10.15s, p95 11.1s.
"""
from __future__ import annotations

import logging
import time

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from gateway.config import settings

logger = logging.getLogger(__name__)

NAMESPACE = "bkn301_gateway"

# Low end covers the rejection paths and /v1/models, where a regression in the
# validation layer would show. 5-30 straddles the measured 10.15s p50. The top
# finite bucket is UPSTREAM_READ_TIMEOUT_SEC's default, so `+Inf - le=120` reads
# as "beyond anything that should ever happen" -- deliberately a literal and not
# read from settings, because buckets that vary per environment cannot be
# aggregated across environments.
DURATION_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 120.0, float("inf"),
)

# Two clusters, because Phase 1 measured two regimes: a prefix-cache hit gives
# TTFT p50 92-132ms, a miss gives p50 1.4-3.8s and p95 6.45s. Losing the prefix
# cache costs 43% of throughput, and with these buckets it shows as one
# unmistakable step of mass from the first cluster into the second.
TTFT_BUCKETS = (
    0.05, 0.1, 0.15, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 4.0, 6.0, 10.0, 20.0,
    float("inf"),
)

# The closed vocabulary for the `reason` label. Kept here rather than in chat.py
# so that adding a rejection path there without thinking about cardinality folds
# to "other" and logs, instead of silently minting series.
REJECT_REASONS = frozenset({
    "body_too_large", "invalid_json", "invalid_body", "scope_denied",
    "model_not_allowed", "messages_required", "prompt_too_long",
    "max_tokens_invalid", "upstream_unreachable", "upstream_error",
    "upstream_bad_response", "upstream_stream_broken", "all_replicas_open",
})

requests_total = Counter(
    f"{NAMESPACE}_requests_total",
    "Requests handled, by route template, method, status and auth method.",
    ["route", "method", "status", "auth_method"],
)

request_duration_seconds = Histogram(
    f"{NAMESPACE}_request_duration_seconds",
    "Wall time from request received to response fully sent.",
    # `outcome` is a three-way fold of the status, not the status itself: a
    # histogram multiplied by every distinct status code costs 18 buckets each
    # and answers no question that `requests_total` does not already answer.
    ["route", "outcome"],
    buckets=DURATION_BUCKETS,
)

time_to_first_token_seconds = Histogram(
    f"{NAMESPACE}_time_to_first_token_seconds",
    "Streaming only: time from request received to the first upstream byte.",
    ["model"],
    buckets=TTFT_BUCKETS,
)

# Unlabelled on purpose. The route is not known until Starlette has matched it,
# which is after this gauge must be incremented; labelling it with the raw path
# would hand an anonymous scraper one series per URL a caller invents.
inflight_requests = Gauge(
    f"{NAMESPACE}_inflight_requests",
    "Requests currently being handled. High and flat with a flat "
    f"{NAMESPACE}_requests_total is what a wedge looks like from this side.",
)

rejections_total = Counter(
    f"{NAMESPACE}_rejections_total",
    "Requests refused before reaching the GPU, by reason.",
    ["reason"],
)

tokens_total = Counter(
    f"{NAMESPACE}_tokens_total",
    "Tokens reported by the upstream.",
    ["model", "kind"],
)

# Streamed requests carry no usage unless stream_options.include_usage is both
# requested and parsed out of the SSE frames, which the gateway does not do yet.
# In Phase 1, 70 of 78 requests were streamed, so tokens_total currently sees
# roughly a tenth of reality. This counter makes that gap visible instead of
# letting a cost dashboard quietly read a tenth of the truth.
requests_missing_usage_total = Counter(
    f"{NAMESPACE}_requests_missing_usage_total",
    "Requests whose token usage the gateway could not observe.",
    ["model"],
)

upstream_requests_total = Counter(
    f"{NAMESPACE}_upstream_requests_total",
    "Attempts against an upstream replica, by index and outcome.",
    ["upstream", "outcome"],
)

# Written by gateway/upstream/metrics_scraper.py.
upstream_up = Gauge(
    f"{NAMESPACE}_upstream_up",
    "1 if the replica's own /metrics was scraped successfully most recently.",
    ["upstream"],
)

upstream_scrape_age_seconds = Gauge(
    f"{NAMESPACE}_upstream_scrape_age_seconds",
    "Seconds since the last successful scrape of the replica's /metrics.",
    ["upstream"],
)

upstream_scrape_failures_total = Counter(
    f"{NAMESPACE}_upstream_scrape_failures_total",
    "Failed scrapes of a replica's /metrics, by failure kind.",
    ["upstream", "kind"],
)

upstream_wedge_suspected = Gauge(
    f"{NAMESPACE}_upstream_wedge_suspected",
    "1 while the wedge signature has held for longer than the configured window.",
    ["upstream"],
)

# The most valuable series in this module. PROBE_CONTRACT.md §4's 120s window is
# an ESTIMATE -- Phase 1 never wedged an engine, so nobody has measured how long
# a real stall runs before it is truly unrecoverable. This is exported
# continuously, not only when it crosses the window, precisely so that the first
# real incident replaces that estimate with a number.
upstream_wedge_stall_seconds = Gauge(
    f"{NAMESPACE}_upstream_wedge_stall_seconds",
    "Seconds the wedge signature has currently held for. 0 when not stalled.",
    ["upstream"],
)

# The gateway is single-worker and relays SSE from its event loop, so anything
# that blocks that loop stalls every concurrent stream at once. This is the one
# metric that makes that whole bug class visible in production; it is here
# because an inline argon2id hash had been doing exactly that. See
# gateway/auth/__init__.py.
event_loop_lag_seconds = Gauge(
    f"{NAMESPACE}_event_loop_lag_seconds",
    "How late a short scheduled sleep woke up. Sustained non-zero means the "
    "event loop is being blocked by synchronous work.",
)


def safe_model(model: str | None) -> str:
    """Fold a caller-supplied model name to the allowlist, or to `other`.

    `chat.py` rejects unknown models but records the string the caller sent, and
    that string is arbitrary. Anything not on the allowlist becomes one series.
    """
    if model and model in settings.ALLOWED_MODELS:
        return model
    return "other"


def record_rejection(reason: str) -> None:
    """Count a refusal, folding an unknown reason rather than minting a series."""
    if reason not in REJECT_REASONS:
        logger.warning(
            "rejection reason %r is not in metrics.REJECT_REASONS; counting it as "
            "'other'. Add it there so it is visible in /metrics.", reason,
        )
        reason = "other"
    rejections_total.labels(reason=reason).inc()


def outcome_of(status: int) -> str:
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "success"


class MetricsMiddleware:
    """Pure-ASGI request instrumentation.

    WHY NOT BaseHTTPMiddleware / @app.middleware("http")
    ----------------------------------------------------
    That implementation pipes the response through
    `anyio.create_memory_object_stream()` and re-wraps it in a private
    `_StreamingResponse`. At its default zero capacity it does not accumulate the
    whole body, so the objection is not that it buffers -- it is an extra
    task-group hop per SSE frame and different backpressure and disconnect
    semantics, applied to the one property this codebase says must not be
    touched (unbuffered passthrough, PRD §6.3 C2).

    Pure ASGI also gets streaming durations right for free: `await self.app(...)`
    returns only once the response generator is exhausted, so the `finally` below
    times a 10-second stream as 10 seconds rather than as the 5ms the handler
    took to return a StreamingResponse.

    WHY MIDDLEWARE AT ALL, GIVEN chat.py COULD COUNT ITS OWN REQUESTS
    ----------------------------------------------------------------
    Because a 401 never reaches a handler body. `require_principal` raises from
    the dependency, which is why the audit log has no record of a failed
    authentication despite auth/keys.py claiming otherwise. "How many callers are
    being turned away, and did that change" is the most operationally useful
    number an authenticating gateway has, and today it cannot be obtained at all.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Share one dict with Request.state so `auth_method`, set by the auth
        # dependency, is readable here after the handler has run.
        scope.setdefault("state", {})

        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        started = time.perf_counter()
        inflight_requests.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_holder["status"] = 500
            raise
        finally:
            # Decrement here rather than on the final body message: a client that
            # disconnects mid-stream never sends one, and the gauge would climb
            # forever.
            inflight_requests.dec()
            try:
                self._record(scope, status_holder["status"], started)
            except Exception as e:  # noqa: BLE001
                # An instrumentation bug must never turn a served request into a
                # 500. Same posture as AuditLog.write: never fail the request,
                # never be silent about it.
                logger.warning("metrics recording failed: %s: %s", type(e).__name__, e)

    @staticmethod
    def _record(scope, status: int, started: float) -> None:
        route = getattr(scope.get("route"), "path", None) or "__unmatched__"
        if route == "/metrics":
            # Self-counting a scrape every 15s is pure noise, and it makes every
            # other series look busier than the service actually is.
            return
        elapsed = time.perf_counter() - started
        auth_method = scope.get("state", {}).get("auth_method", "none")
        requests_total.labels(
            route=route,
            method=scope.get("method", "GET"),
            status=str(status),
            auth_method=auth_method,
        ).inc()
        request_duration_seconds.labels(
            route=route, outcome=outcome_of(status)
        ).observe(elapsed)


def sample(name: str, **labels) -> float | None:
    """Read one sample value. Test helper.

    Note the trap it documents: `get_sample_value` wants the SAMPLE name, so a
    counter is `..._total` here even though its family name is not.
    """
    return REGISTRY.get_sample_value(name, labels or None)
