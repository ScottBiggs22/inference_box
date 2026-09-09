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

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.config import settings
from gateway.routes import chat, health, models
from gateway.upstream.client import pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.start()
    logger.info(
        "gateway up: upstreams=%s allowed_models=%s",
        pool.urls, settings.ALLOWED_MODELS,
    )
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
    try:
        yield
    finally:
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
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
