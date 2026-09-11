"""Authentication dependency shared by every protected route.

Two credential types, tried in this order (PRD §5.3):

  1. a JWT, if one validates -- the preferred service-to-service path;
  2. an API key, for non-interactive jobs and break-glass.

Both arrive as `Authorization: Bearer <credential>`, because that is what the
OpenAI client libraries send and the whole point of this surface is that an
existing OpenAI client can be pointed at it unchanged.

Telling them apart is done by SHAPE, not by trying each in turn: a JWT has three
dot-separated segments, an API key starts with `bkn_`. Attempting JWT validation
on an API key would produce a misleading error and burn CPU on a signature check
that cannot succeed.
"""
from __future__ import annotations

from dataclasses import dataclass

import anyio
import anyio.to_thread
from fastapi import Depends, Header, HTTPException, Request, status

from gateway.auth.jwt import JwtVerifier, scopes_of
from gateway.auth.keys import AuthError, KeyStore
from gateway.config import settings

# WHY THE ARGON2 VERIFY IS OFFLOADED, AND WHY IT IS ALSO BOUNDED
#
# `require_principal` is a coroutine, so everything it calls inline runs ON the
# event loop. `KeyStore.verify` is a synchronous argon2id hash -- measured at
# ~77ms on the Phase 1 Xeon box, ~22ms on an M-series laptop -- and nothing in
# it yields. So every API-key arrival froze the loop for that long, and with it
# every in-flight SSE relay, /healthz, and any background task. The container
# runs `--workers 1` on the stated grounds that "the gateway is IO-bound, so
# concurrency comes from async, not processes" (deploy/Dockerfile.gateway), and
# a blocking hash is precisely the thing that makes that untrue.
#
# Measured on this repo, loop lag (how late a 10ms sleep wakes) while N
# authentications run concurrently:
#
#     N   inline    to_thread(unbounded)   to_thread(limiter=6)
#     1    33.8ms         12.4ms                   --
#     5   106.8ms         12.2ms                  6.8ms
#     8   191.4ms         32.6ms                 16.0ms
#    40       --         503.9ms                 19.7ms
#
# Inline lag scales linearly with concurrency: N verifications serialise.
#
# THE LIMITER IS NOT OPTIONAL, and the N=40 row is why. anyio's default thread
# limiter is 40, and argon2-cffi's defaults are memory_cost=64 MiB with
# parallelism=4. Offloading without a bound therefore permits ~2.5 GiB of
# transient allocation and 160 threads of CPU demand -- which oversubscribes the
# machine so badly that loop lag becomes WORSE than doing nothing at all
# (503.9ms). Bounding costs no throughput: wall time at N=40 was 605ms bounded
# vs 617ms unbounded.
#
# It is reachable before authentication succeeds. `KeyStore.verify` runs
# `_dummy_verify()` on an unknown keyid deliberately, so that response latency
# cannot be used to enumerate valid key ids -- which means a stranger sending
# well-formed-but-wrong keys pays the full hash. Unbounded, that is a cheap
# memory-exhaustion lever; the coroutine version was accidentally serialising it
# away. Six is chosen against a GPU with ~5 decode slots: more concurrent hashes
# than the card can have users buys nothing. Overflow queues in the limiter,
# which is correct back-pressure at no CPU cost.
#
# The JWT path is deliberately NOT offloaded: RS256 is ~1-2ms, and a thread hop
# would cost more than it saves. That asymmetry is also the measured argument
# for PRD §5.3's preference for the JWT path.
_verify_limiter: anyio.CapacityLimiter | None = None


def _argon2_limiter() -> anyio.CapacityLimiter:
    """Built on first use, not at import, so it binds to the running loop."""
    global _verify_limiter
    if _verify_limiter is None:
        _verify_limiter = anyio.CapacityLimiter(settings.AUTH_VERIFY_CONCURRENCY)
    return _verify_limiter


@dataclass
class Principal:
    """Who is making this request, however they authenticated."""

    subject: str
    scopes: list[str]
    method: str          # "jwt" | "apikey"
    keyid: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


_key_store: KeyStore | None = None
_jwt_verifier: JwtVerifier | None = None


def get_key_store() -> KeyStore:
    global _key_store
    if _key_store is None:
        _key_store = KeyStore()
    return _key_store


def get_jwt_verifier() -> JwtVerifier:
    global _jwt_verifier
    if _jwt_verifier is None:
        _jwt_verifier = JwtVerifier()
    return _jwt_verifier


def reset_auth_singletons() -> None:
    """Test seam. Lets a test point the store at a temp file without monkeypatching.

    Also drops the argon2 limiter: it is bound to whichever event loop first used
    it, and the suite builds a fresh loop per TestClient.
    """
    global _key_store, _jwt_verifier, _verify_limiter
    _key_store = None
    _jwt_verifier = None
    _verify_limiter = None


def _unauthorized() -> HTTPException:
    # WWW-Authenticate is what makes this a well-behaved Bearer endpoint, and the
    # detail is deliberately generic -- see keys.AuthError on why the reason for
    # a rejection never reaches the caller.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _mark(request: Request | None, method: str) -> None:
    """Publish the credential type for the metrics middleware.

    It runs outside the dependency system and so cannot see the Principal, and
    an unauthenticated request never produces one at all -- the middleware reads
    "none" in that case, which is the right answer. Recorded as a label because
    PRD §5.3 calls the JWT path "preferred" and nothing currently reports whether
    any caller actually uses it.
    """
    if request is not None:
        request.state.auth_method = method


async def require_principal(
    request: Request = None,  # noqa: RUF013 - injected by type; default keeps it callable in tests
    authorization: str | None = Header(default=None),
    store: KeyStore = Depends(get_key_store),
    verifier: JwtVerifier = Depends(get_jwt_verifier),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()

    credential = authorization[len("bearer "):].strip()
    if not credential:
        raise _unauthorized()

    # Shape-based dispatch. A JWT is header.payload.signature.
    if credential.count(".") == 2 and not credential.startswith("bkn_"):
        try:
            claims = verifier.verify(credential)
        except AuthError as e:
            raise _unauthorized() from e
        _mark(request, "jwt")
        return Principal(
            subject=str(claims.get("sub")),
            scopes=scopes_of(claims),
            method="jwt",
        )

    try:
        # Off the event loop and bounded -- see the note at the top of this file.
        record = await anyio.to_thread.run_sync(
            store.verify, credential, limiter=_argon2_limiter()
        )
    except AuthError as e:
        raise _unauthorized() from e
    _mark(request, "apikey")
    return Principal(
        subject=f"key:{record.keyid}",
        scopes=record.scopes,
        method="apikey",
        keyid=record.keyid,
    )
