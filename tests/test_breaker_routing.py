"""The breaker as the router actually sees it, over a real socket.

The single most important assertion here is that "every replica is open" fails
FAST. With one replica that is the common case, not a corner one, and converting
a 120-second hang into an immediate 503 is the entire value the breaker delivers
at N=1 -- so a test that only checks the status code does not test the design.
"""
from __future__ import annotations

import time

import httpx
import pytest

from gateway.upstream.breaker import BreakerState, FailureKind
from gateway.upstream.client import AllReplicasOpen, UpstreamPool, classify


def _body(**over):
    b = {"model": "Qwen/Qwen3-8B-AWQ",
         "messages": [{"role": "user", "content": "hello"}]}
    b.update(over)
    return b


class TestClassify:
    @pytest.mark.parametrize("status,expected", [
        (200, None),
        (400, FailureKind.CLIENT_ERROR),
        (429, FailureKind.THROTTLED),
        (500, FailureKind.SERVER_ERROR),
        (503, FailureKind.SERVER_ERROR),
    ])
    def test_status_codes(self, status, expected):
        assert classify(None, status) is expected

    def test_a_read_timeout_is_not_a_connect_failure(self):
        """They are weighted alike but must stay distinguishable.

        A read timeout means the request may still be RUNNING on the GPU, which
        is why the retry policy treats them completely differently.
        """
        assert classify(httpx.ReadTimeout("x"), None) is FailureKind.READ_TIMEOUT
        assert classify(httpx.ConnectError("x"), None) is FailureKind.CONNECT


class TestPickSkipsOpenReplicas:
    def test_an_open_replica_is_not_returned(self):
        pool = UpstreamPool(["http://a/v1", "http://b/v1"])
        b = pool.breakers.get("http://a/v1")
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        assert b.state is BreakerState.OPEN
        assert {pool.pick() for _ in range(10)} == {"http://b/v1"}

    def test_all_open_raises_rather_than_routing_anyway(self):
        pool = UpstreamPool(["http://a/v1"])
        b = pool.breakers.get("http://a/v1")
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        with pytest.raises(AllReplicasOpen):
            pool.pick()

    def test_the_kill_switch_disables_all_of_it(self, monkeypatch):
        """A breaker with no off switch is a new way to have an outage."""
        from gateway.config import settings
        pool = UpstreamPool(["http://a/v1"])
        b = pool.breakers.get("http://a/v1")
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        monkeypatch.setattr(settings, "BREAKER_ENABLED", False, raising=False)
        assert pool.pick() == "http://a/v1"


class TestAllOpenOverARealSocket:
    def _trip(self, base: str, key: str, n: int = 8) -> None:
        for _ in range(n):
            try:
                httpx.post(f"{base}/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json=_body(), timeout=10.0)
            except httpx.HTTPError:
                pass

    def test_returns_503_with_retry_after_once_open(self, live_gateway):
        base, key = live_gateway("--stub-fail-after", "0")
        self._trip(base, key)
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_body(), timeout=10.0)
        assert r.status_code == 503
        assert r.json()["detail"] == "all_replicas_open"
        assert "Retry-After" in r.headers

    def test_it_fails_FAST(self, live_gateway):
        """The whole justification for raising instead of routing anyway.

        A caller that blocks for the 5s connect budget -- let alone the 120s read
        budget -- during an outage exhausts its own pools and propagates the
        outage upward. PRD §6.3 C3 wants the app to degrade to RAG-only rather
        than hang, and it can only do that if this answers quickly.
        """
        base, key = live_gateway("--stub-fail-after", "0")
        self._trip(base, key)
        t0 = time.perf_counter()
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_body(), timeout=10.0)
        elapsed = time.perf_counter() - t0
        assert r.status_code == 503
        assert elapsed < 1.0, (
            f"took {elapsed:.2f}s; an open breaker must not spend the connect "
            "budget, or it is delivering none of its value"
        )

    def test_readyz_stays_green_with_a_breaker_open(self, live_gateway):
        """The gateway is not the broken component.

        Pulling its pod out of the endpoint list would fix nothing and would
        remove the thing correctly reporting the failure.
        """
        base, key = live_gateway("--stub-fail-after", "0")
        self._trip(base, key)
        r = httpx.get(f"{base}/readyz", timeout=5.0)
        assert r.status_code == 200
        assert "open" in r.json()["breakers"].values()

    def test_the_breaker_state_is_exported(self, live_gateway):
        base, key = live_gateway("--stub-fail-after", "0")
        self._trip(base, key)
        body = httpx.get(f"{base}/metrics", timeout=5.0).text
        assert 'bkn301_gateway_rejections_total{reason="all_replicas_open"}' in body


class TestRecovery:
    def test_half_open_closes_once_the_replica_heals(self, live_gateway):
        """The transition a request-counted stub could never test.

        Once the breaker opens the gateway stops sending chat requests, so a heal
        driven by request count never advances. /control is out-of-band for
        exactly this reason.
        """
        base, key = live_gateway("--stub-control", "--stub-fail-after", "0",
                                 "--stub-fail-mode", "all")
        for _ in range(8):
            try:
                httpx.post(f"{base}/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json=_body(), timeout=10.0)
            except httpx.HTTPError:
                pass
        assert httpx.post(f"{base}/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json=_body(), timeout=10.0).status_code == 503

        # Heal the upstream out of band, then wait only on the prober.
        upstream = httpx.get(f"{base}/readyz", timeout=5.0).json()["upstreams"]
        stub_base = next(iter(upstream)).removesuffix("/v1")
        assert httpx.post(f"{stub_base}/control", json={"fail": False},
                          timeout=5.0).status_code == 200

        deadline = time.time() + 60
        while time.time() < deadline:
            r = httpx.post(f"{base}/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json=_body(), timeout=10.0)
            if r.status_code == 200:
                break
            time.sleep(1.0)
        else:
            pytest.fail("breaker never reclosed after the upstream recovered")

        assert httpx.get(f"{base}/readyz", timeout=5.0).json()["breakers"] == {
            next(iter(upstream)): "closed"
        }
