"""Bounded retries with jitter on 429/502/503/504.

PRD §5.3 asks for "bounded retries with jitter on 429/503" and specifies no
bound. Chosen here: ONE retry, TWO attempts total -- a retried request that
already reached the GPU burned one of ~5 decode slots, so more than this turns a
503 storm into a self-inflicted one (see gateway/upstream/client.py's module
docstring for the full reasoning).

Two taxonomies are deliberately different and this file pins the difference:
FailureKind/WEIGHTS (breaker.py) decide how suspicious an outcome is; the
retryable-status set here decides whether sending it again is SAFE. 500 and 503
are the same FailureKind (SERVER_ERROR) but only one of them is retried.
"""
from __future__ import annotations

import httpx
import pytest

from gateway.upstream.client import (
    MAX_ATTEMPTS,
    UpstreamPool,
    UpstreamUnreachable,
    _retry_delay,
    _should_retry,
)


def _body(**over):
    b = {"model": "Qwen/Qwen3-8B-AWQ",
         "messages": [{"role": "user", "content": "hello"}]}
    b.update(over)
    return b


class TestShouldRetryStatusCodes:
    @pytest.mark.parametrize("status,expected", [
        (429, True),
        (502, True),
        (503, True),
        (504, True),
        (500, False),   # deterministic-error assumption; retrying doubles it
        (400, False),
        (404, False),
    ])
    def test_status(self, status, expected):
        resp = httpx.Response(status, request=httpx.Request("POST", "http://x"))
        assert _should_retry(resp) is expected

    def test_500_and_503_are_the_same_breaker_failurekind_but_differ_here(self):
        """The distinction this whole module exists to draw.

        classify() in this same file folds 500 and 503 into ONE FailureKind
        (SERVER_ERROR) for the breaker's purposes -- both indicate a replica
        worth the same suspicion. Retrying them is not equally safe.
        """
        from gateway.upstream.client import classify
        r500 = httpx.Response(500, request=httpx.Request("POST", "http://x"))
        r503 = httpx.Response(503, request=httpx.Request("POST", "http://x"))
        assert classify(None, 500) is classify(None, 503)
        assert _should_retry(r500) is False
        assert _should_retry(r503) is True


class TestShouldRetryExceptions:
    def test_connect_error_is_retried(self):
        """Nothing reached the GPU, so nothing is lost by trying again."""
        assert _should_retry(httpx.ConnectError("refused")) is True

    def test_read_timeout_is_not_retried(self):
        """The single most important rule in this file.

        A read timeout means the request may still be RUNNING on the GPU.
        Retrying it doubles decode load on an engine that is already not
        answering -- precisely the wrong thing to do to a wedged one.
        """
        assert _should_retry(httpx.ReadTimeout("timed out")) is False


class TestRetryDelay:
    def test_jitter_is_bounded_and_nonnegative(self):
        for attempt in range(4):
            d = _retry_delay(attempt, None)
            assert 0.0 <= d <= 2.0

    def test_retry_after_header_is_honoured(self):
        resp = httpx.Response(503, headers={"Retry-After": "1"},
                              request=httpx.Request("POST", "http://x"))
        assert _retry_delay(0, resp) == 1.0

    def test_retry_after_is_clamped_to_the_cap(self):
        """An upstream asking for 30s should produce a 503, not a 30s hang."""
        resp = httpx.Response(503, headers={"Retry-After": "30"},
                              request=httpx.Request("POST", "http://x"))
        assert _retry_delay(0, resp) == 2.0

    def test_a_malformed_retry_after_falls_back_to_jitter(self):
        resp = httpx.Response(503, headers={"Retry-After": "not-a-number"},
                              request=httpx.Request("POST", "http://x"))
        assert 0.0 <= _retry_delay(0, resp) <= 2.0


class TestAttemptBound:
    def test_max_attempts_is_two(self):
        """One retry. Pinned as a constant, not just a behaviour, so a change to
        it is a deliberate edit rather than an accident."""
        assert MAX_ATTEMPTS == 2


class TestRetryOverARealStub:
    """Exercises pool.request()/open_stream() end to end against a live stub.

    Not TestClient: the pool itself makes real httpx calls with real timeouts,
    and the read-timeout/no-retry test specifically needs a real slow response.
    """

    def _pool_for(self, base: str) -> UpstreamPool:
        return UpstreamPool([f"{base}/v1"])

    async def test_429_is_retried_and_the_retry_succeeds(self, live_stub):
        base = live_stub("--stub-control", "--stub-fail-first-n", "1",
                         "--stub-fail-status", "429")
        pool = self._pool_for(base)
        await pool.start()
        try:
            upstream, resp = await pool.request("/chat/completions", json=_body())
            assert resp.status_code == 200
        finally:
            await pool.stop()
        seen = httpx.post(f"{base}/control", json={}, timeout=5.0).json()["requests"]
        assert seen == 2, "expected exactly one retry (2 attempts), saw a different count"

    async def test_503_is_retried_and_the_retry_succeeds(self, live_stub):
        base = live_stub("--stub-control", "--stub-fail-first-n", "1",
                         "--stub-fail-status", "503")
        pool = self._pool_for(base)
        await pool.start()
        try:
            upstream, resp = await pool.request("/chat/completions", json=_body())
            assert resp.status_code == 200
        finally:
            await pool.stop()

    async def test_500_is_not_retried(self, live_stub):
        """The deterministic-error assumption, proven end to end.

        If this retried, the stub would see 2 requests and (with
        fail_first_n=1) the SECOND would succeed, masking the bug. Asserting the
        request count is what makes this test able to fail.
        """
        base = live_stub("--stub-control", "--stub-fail-first-n", "1",
                         "--stub-fail-status", "500")
        pool = self._pool_for(base)
        await pool.start()
        try:
            upstream, resp = await pool.request("/chat/completions", json=_body())
            assert resp.status_code == 500
        finally:
            await pool.stop()
        seen = httpx.post(f"{base}/control", json={}, timeout=5.0).json()["requests"]
        assert seen == 1, "a non-retryable status was retried anyway"

    async def test_sustained_failure_stops_at_the_bound(self, live_stub):
        """A 503 storm gets exactly 2 attempts, never more.

        A 503 that runs out of retries is a normal RETURN, not a raised
        exception -- pool.request() only raises when every attempt failed
        before any response arrived at all.
        """
        base = live_stub("--stub-fail-after", "0", "--stub-control")
        pool = self._pool_for(base)
        await pool.start()
        try:
            upstream, resp = await pool.request("/chat/completions", json=_body())
            assert resp.status_code == 503
        finally:
            await pool.stop()
        seen = httpx.post(f"{base}/control", json={}, timeout=5.0).json()["requests"]
        assert seen == MAX_ATTEMPTS

    async def test_streaming_503_is_retried_transparently(self, live_stub):
        """The caller sees ONE clean 200 stream; the failed attempt is invisible.

        Retrying is possible here at all only because pool.open_stream() reads
        status and headers before any body byte -- a 503 is retryable exactly up
        to that point and not one byte later.
        """
        base = live_stub("--stub-control", "--stub-fail-first-n", "1",
                         "--stub-fail-status", "503")
        pool = self._pool_for(base)
        await pool.start()
        try:
            upstream, resp = await pool.open_stream(
                "/chat/completions", json=_body(stream=True, max_tokens=20))
            assert resp.status_code == 200
            chunks = [c async for c in resp.aiter_raw()]
            await resp.aclose()
            assert b"".join(chunks).rstrip().endswith(b"data: [DONE]")
        finally:
            await pool.stop()

    async def test_a_slow_upstream_read_timeout_is_not_retried(self, live_stub):
        """The read-timeout rule, proven against a genuinely slow response.

        --stub-latency holds the response well past a deliberately short read
        timeout, so this triggers a REAL httpx.ReadTimeout rather than a
        synthetic one -- and confirms it costs exactly one attempt.
        """
        base = live_stub("--stub-control", "--stub-latency", "1.0")
        pool = UpstreamPool([f"{base}/v1"])
        pool._client = httpx.AsyncClient(  # noqa: SLF001 - test needs a short read timeout
            timeout=httpx.Timeout(connect=2.0, read=0.2, write=2.0, pool=2.0)
        )
        try:
            with pytest.raises(UpstreamUnreachable):
                await pool.request("/chat/completions", json=_body(max_tokens=5))
        finally:
            await pool.stop()
        seen = httpx.post(f"{base}/control", json={}, timeout=5.0).json()["requests"]
        assert seen == 1, "a read timeout was retried against an engine that may still be running"
