"""The argon2id verify must run OFF the event loop, and must be bounded.

Both properties are asserted structurally rather than by timing, so these do not
flake on a loaded CI box. The measurements that motivated them are recorded in
the note at the top of gateway/auth/__init__.py.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from gateway.auth import Principal, require_principal, reset_auth_singletons
from gateway.auth.keys import ApiKeyRecord, mint_key
from gateway.config import settings


class _RecordingStore:
    """A KeyStore stand-in that reports which thread its verify ran on.

    Deliberately not a real KeyStore: what is under test is where the call
    happens and how many run at once, not whether argon2 is correct.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.threads: list[int] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def verify(self, presented: str) -> ApiKeyRecord:
        with self._lock:
            self.threads.append(threading.get_ident())
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.delay:
                # Real blocking, not asyncio.sleep -- a sleep would yield and
                # the test would pass even if the call were left on the loop.
                threading.Event().wait(self.delay)
        finally:
            with self._lock:
                self.concurrent -= 1
        return ApiKeyRecord(keyid="deadbeef", hash="x", scopes=["chat"])


class _NoJwt:
    enabled = False

    def verify(self, token: str) -> dict:  # pragma: no cover - never reached
        raise AssertionError("the API-key path must not consult the JWT verifier")


@pytest.fixture(autouse=True)
def _clean_limiter():
    reset_auth_singletons()
    yield
    reset_auth_singletons()


class TestVerifyRunsOffTheEventLoop:
    async def test_verify_does_not_run_on_the_loop_thread(self):
        """The regression this file exists for.

        Inline, a ~77ms hash froze every in-flight SSE relay. If someone later
        'simplifies' the to_thread call away, this fails.
        """
        store = _RecordingStore()
        key, _ = mint_key()
        loop_thread = threading.get_ident()

        principal = await require_principal(
            authorization=f"Bearer {key}", store=store, verifier=_NoJwt()
        )

        assert isinstance(principal, Principal)
        assert principal.method == "apikey"
        assert store.threads, "verify was never called"
        assert loop_thread not in store.threads, (
            "argon2 ran on the event loop thread; every concurrent stream stalls "
            "for the duration of the hash"
        )

    async def test_the_loop_keeps_running_during_a_verify(self):
        """A blocking verify must not stop other tasks from being scheduled."""
        store = _RecordingStore(delay=0.25)
        key, _ = mint_key()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        await require_principal(
            authorization=f"Bearer {key}", store=store, verifier=_NoJwt()
        )
        t.cancel()
        # A blocked loop would allow roughly zero. Deliberately a weak bound: the
        # point is "the loop ran at all", not a latency SLO.
        assert ticks >= 5, f"loop was starved during the verify (only {ticks} ticks)"


class TestVerifyIsBounded:
    async def test_concurrency_is_capped(self, monkeypatch):
        """anyio's default threadpool limit is 40.

        At 40 x 64 MiB of argon2 memory cost this is a memory-exhaustion lever
        reachable by unauthenticated traffic, and it measured slower than running
        inline. The cap is the fix; this asserts it is actually applied.
        """
        monkeypatch.setattr(settings, "AUTH_VERIFY_CONCURRENCY", 3, raising=False)
        reset_auth_singletons()

        store = _RecordingStore(delay=0.05)
        key, _ = mint_key()

        await asyncio.gather(*(
            require_principal(
                authorization=f"Bearer {key}", store=store, verifier=_NoJwt()
            )
            for _ in range(20)
        ))

        assert store.max_concurrent <= 3, (
            f"{store.max_concurrent} concurrent hashes ran against a cap of 3"
        )
        assert store.max_concurrent > 1, "nothing ran in parallel; is the offload wired up?"
