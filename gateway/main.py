"""FastAPI application for the FirstData inference gateway.

ONE ENTRY POINT. This is worth stating explicitly because the consuming repo has
two -- `main.py` and `app/main.py` -- both live, both carrying routers, both
carrying comments admitting they are maintained separately and must be kept in
sync by hand. That is a standing source of drift (a startup hook fixed in one and
not the other is invisible until it matters). There is exactly one app here, and
it is built by a factory so tests can construct an isolated instance.

Uses `lifespan` rather than the deprecated `@app.on_event`, for the same reason:
startup and shutdown are one object, so a resource opened at boot and a resource
closed at exit cannot drift apart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway import metrics as gw_metrics
from gateway.config import settings
from gateway.metrics import MetricsMiddleware
from gateway.routes import chat, health, metrics, models
from gateway.upstream.client import pool
from gateway.upstream.metrics_scraper import upstream_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _scrape_loop() -> None:
    """Poll every replica's own /metrics so the wedge signature can be derived."""
    while True:
        try:
            await upstream_metrics.poll_once(pool.client, pool.urls)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("upstream metrics poll failed: %s: %s", type(e).__name__, e)
        await asyncio.sleep(settings.UPSTREAM_METRICS_POLL_SEC)


async def _breaker_prober() -> None:
    """Reclose probes for open replicas, and the wedge signal for closed ones.

    Deliberately lazy about the probing half: while everything is closed this
    loop does nothing but read local state, so a healthy gateway generates no
    background traffic at all and the unit suite does not acquire a task hammering
    a closed port.
    """
    while True:
        try:
            now = time.monotonic()
            for breaker in pool.breakers:
                # A closed replica is still watched for the wedge signature: a
                # wedged engine answers /health and serves no tokens, so nothing
                # else in the system would notice.
                if breaker.state.name == "CLOSED":
                    breaker.record_wedge(
                        upstream_metrics.wedge_suspected(breaker.url), now)
                    continue
                if breaker.due_for_probe(now):
                    ok = await pool.probe(breaker.url)
                    breaker.record_probe(
                        ok, time.monotonic(),
                        wedge_suspected=upstream_metrics.wedge_suspected(breaker.url),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("breaker prober failed: %s: %s", type(e).__name__, e)
        await asyncio.sleep(settings.BREAKER_PROBE_TICK_SEC)


async def _loop_lag_monitor(interval: float = 0.25) -> None:
    """Publish how late a short sleep wakes up.

    This process relays SSE from its event loop with a single worker, so
    synchronous work on that loop stalls every concurrent stream at once -- an
    inline argon2id hash was doing exactly that (see gateway/auth/__init__.py).
    This is the metric that makes that class of bug visible in production rather
    than only under a benchmark, and the breaker's timings assume the loop is
    responsive.
    """
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        gw_metrics.event_loop_lag_seconds.set(
            max(0.0, time.perf_counter() - t0 - interval)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.start()
    logger.info(
        "gateway up: upstreams=%s allowed_models=%s",
        pool.urls, settings.ALLOWED_MODELS,
    )
    # Replicas are labelled by index in /metrics, never by URL -- that endpoint is
    # unauthenticated and the URLs are private-subnet addresses. This is the one
    # place the mapping is written down.
    for i, url in enumerate(pool.urls):
        logger.info("upstream %d = %s", i, url)
    if settings.AUDIT_INCLUDE_PROMPT_TEXT:
        logger.error(
            "AUDIT_INCLUDE_PROMPT_TEXT is on. Prompt text will be written to the "
            "audit log, which makes it a PII store. This must not be the case in "
            "any deployed environment."
        )
    if not settings.API_KEY_PEPPER:
        # Not fatal: a fresh dev box has no pepper and should still start. But it
        # weakens the key store against offline attack, so it must never be
        # silent.
        logger.warning(
            "API_KEY_PEPPER is unset. Key hashes are salted but not peppered, so "
            "a stolen key store is sufficient for an offline attack."
        )
    tasks = [
        asyncio.create_task(_scrape_loop(), name="upstream-metrics"),
        asyncio.create_task(_loop_lag_monitor(), name="loop-lag"),
        asyncio.create_task(_breaker_prober(), name="breaker-prober"),
    ]
    try:
        yield
    finally:
        # Cancel and await BEFORE closing the client: a poll in flight against a
        # closed httpx client raises into the shutdown path for no reason.
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await pool.stop()
        logger.info("gateway down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="BKN301 Inference Gateway",
        description=(
            "Authenticating OpenAI-compatible gateway in front of self-hosted vLLM."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    # No CORS middleware. This gateway is called server-to-server by the C# API,
    # never from a browser. Adding permissive CORS "just in case" is how an
    # internal service acquires a browser-reachable attack surface.
    #
    # Instrumentation is pure ASGI rather than BaseHTTPMiddleware -- see the
    # class docstring. It is added unconditionally: METRICS_ENABLED gates
    # EXPOSURE, not collection, so that flipping it cannot change which code
    # paths run.
    app.add_middleware(MetricsMiddleware)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
