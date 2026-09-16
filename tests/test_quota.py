"""Per-key token budgets and concurrency caps (PRD §5.3).

Three layers, tested separately because each can hide a bug the others cannot
catch:

  * `QuotaTracker` in isolation -- the accounting rules, with no HTTP involved.
  * the route, sequentially -- that chat.py actually calls the tracker and
    turns a raised QuotaExceeded into the right 429.
  * the route, CONCURRENTLY, over a real socket -- that a concurrency slot is
    genuinely held for the life of a streamed response and not just until the
    headers are sent. TestClient cannot prove this: its ASGI transport
    collects a streaming response before returning it (see
    test_streaming_status.py), so a bug that releases the slot as soon as the
    StreamingResponse object is constructed would pass there and fail here.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from gateway.auth import get_key_store
from gateway.auth.keys import mint_key
from gateway.main import create_app
from gateway.quota import QuotaExceeded, QuotaTracker, quota


def _body(**over):
    b = {"model": "Qwen/Qwen3-8B-AWQ", "messages": [{"role": "user", "content": "hi"}]}
    b.update(over)
    return b


def _client(key_store, stub_base):
    """A TestClient wired to a real stub, not `dead_upstream`.

    The `client` fixture always points at a dead upstream (see conftest.py),
    which is correct for the request-validation suite -- every case there is a
    rejection before forwarding -- but wrong here: a budget test needs a
    request to actually succeed once, so a later one has something to be
    rejected against.
    """
    from fastapi.testclient import TestClient

    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: key_store
    return TestClient(app)


def _metric(text: str, name: str, **labels) -> float | None:
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return sample.value
    return None


@pytest.fixture(autouse=True)
def _clean_quota():
    """The module-level tracker is a process global, same reasoning as
    gateway/auth's `_verify_limiter` -- tests must not leak state into each
    other via it.
    """
    quota.reset()
    yield
    quota.reset()


class TestQuotaTrackerUnit:
    def test_budget_is_not_exceeded_until_usage_reaches_it(self):
        t = QuotaTracker()
        t.check_budget("k1", 100)  # 0 consumed so far -- must not raise
        t.record_usage("k1", 99)
        t.check_budget("k1", 100)  # still under -- must not raise

    def test_a_request_that_lands_exactly_on_budget_still_completed(self):
        """The budget prices what already happened, not what is about to.

        record_usage is what a COMPLETED request reports; check_budget is what
        the NEXT request is judged against. A key sitting exactly at budget
        must not retroactively fail the request that put it there.
        """
        t = QuotaTracker()
        t.check_budget("k1", 10)  # allowed while under budget
        t.record_usage("k1", 15)  # this request finishes over budget
        with pytest.raises(QuotaExceeded) as exc:
            t.check_budget("k1", 10)  # the NEXT one is rejected
        assert exc.value.reason == "token_budget_exceeded"

    def test_concurrency_cap_rejects_the_slot_past_the_limit(self):
        t = QuotaTracker()
        t.acquire("k1", 2)
        t.acquire("k1", 2)
        with pytest.raises(QuotaExceeded) as exc:
            t.acquire("k1", 2)
        assert exc.value.reason == "concurrency_cap_exceeded"

    def test_release_frees_a_slot_for_a_later_acquire(self):
        t = QuotaTracker()
        t.acquire("k1", 1)
        with pytest.raises(QuotaExceeded):
            t.acquire("k1", 1)
        t.release("k1")
        t.acquire("k1", 1)  # must not raise now

    def test_release_floors_at_zero(self):
        """A stray extra release must not credit a key with negative usage."""
        t = QuotaTracker()
        t.release("k1")
        t.release("k1")
        t.acquire("k1", 1)  # still exactly one slot, not -2 headroom

    def test_keys_do_not_share_state(self):
        t = QuotaTracker()
        t.acquire("k1", 1)
        t.acquire("k2", 1)  # a different keyid's slot is untouched
        with pytest.raises(QuotaExceeded):
            t.acquire("k1", 1)


class TestTokenBudgetAtTheRoute:
    def test_a_key_with_no_budget_is_unaffected(self, key_store, stub_upstream):
        """The overwhelming majority of keys: no budget field, no new behaviour."""
        plaintext, record = mint_key(scopes=["chat"])
        key_store.add(record)
        base = stub_upstream()

        with _client(key_store, base) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {plaintext}"},
                       json=_body(max_tokens=5))
        assert r.status_code == 200

    def test_exceeding_the_budget_is_a_429_before_the_next_request(
        self, key_store, audit_file, stub_upstream
    ):
        plaintext, record = mint_key(scopes=["chat"], token_budget=5)
        key_store.add(record)
        base = stub_upstream()

        with _client(key_store, base) as c:
            r1 = c.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {plaintext}"},
                        json=_body(max_tokens=20))
            assert r1.status_code == 200, r1.text

            r2 = c.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {plaintext}"},
                        json=_body(max_tokens=20))
            assert r2.status_code == 429
            assert r2.json()["detail"] == "token_budget_exceeded"

        records = [json.loads(x) for x in audit_file.read_text().splitlines()]
        assert records[-1]["reason"] == "token_budget_exceeded"
        assert records[-1]["status"] == 429
        # Rejected before the GPU: a third call to the same store never ran,
        # which the stub cannot prove directly, but the rejection is unaudited
        # with any upstream field -- see _reject, which never sets `upstream`.
        assert records[-1]["upstream"] is None


class TestConcurrencyCapAtTheRoute:
    async def test_the_second_concurrent_request_is_rejected(self, live_gateway):
        base, key = live_gateway("--stub-latency", "1.5",
                                 key_kwargs={"max_concurrency": 1})

        async with httpx.AsyncClient(timeout=10.0) as ac:
            results = await asyncio.gather(*(
                ac.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_body(max_tokens=10))
                for _ in range(2)
            ))

        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 429], (
            f"expected one request to get the only slot and one to be turned "
            f"away, got {statuses}"
        )
        rejected = next(r for r in results if r.status_code == 429)
        assert rejected.json()["detail"] == "concurrency_cap_exceeded"

    async def test_the_slot_is_released_so_a_later_request_succeeds(self, live_gateway):
        base, key = live_gateway("--stub-latency", "0.1",
                                 key_kwargs={"max_concurrency": 1})
        async with httpx.AsyncClient(timeout=10.0) as ac:
            r1 = await ac.post(f"{base}/v1/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=_body(max_tokens=5))
            assert r1.status_code == 200
            r2 = await ac.post(f"{base}/v1/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=_body(max_tokens=5))
            assert r2.status_code == 200, (
                "the slot from r1 was never released, or was released too late"
            )

    async def test_a_streamed_requests_slot_is_held_until_the_stream_ends(
        self, live_gateway
    ):
        """The regression this suite exists to catch.

        chat_completions returns a StreamingResponse well before the stream is
        actually done. If the concurrency slot were released when that object
        is constructed -- rather than in relay()'s finally, once the body is
        fully sent -- a second request would wrongly be admitted while the
        first is still mid-stream.

        Timing note: the stub's `--stub-latency` sleeps for the FULL delay
        BEFORE it sends any response headers at all (see stub/server.py's
        `/v1/chat/completions` -- the semaphore/latency wait happens before
        the StreamingResponse is even constructed), and only then paces
        per-word delivery, capped at 50ms/word regardless of the latency
        value. So a large `max_tokens` -- not a large `--stub-latency` -- is
        what makes the POST-header window long enough to probe reliably; a
        short latency keeps the pre-header wait, which this test is not
        trying to measure, small.
        """
        base, key = live_gateway("--stub-latency", "0.3",
                                 key_kwargs={"max_concurrency": 1})
        async with httpx.AsyncClient(timeout=20.0) as ac:
            async def _consume_stream():
                async with ac.stream(
                    "POST", f"{base}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=_body(stream=True, max_tokens=200),
                ) as r:
                    assert r.status_code == 200
                    async for _chunk in r.aiter_raw():
                        pass

            task = asyncio.create_task(_consume_stream())
            # 0.3s pre-header wait, then ~200 words * up to 50ms = up to 10s of
            # per-word pacing. 1.0s lands safely after headers and well before
            # the stream ends.
            await asyncio.sleep(1.0)

            blocked = await ac.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=_body(max_tokens=5),
            )
            assert blocked.status_code == 429, (
                "a second request was admitted while the first stream was "
                "still in flight -- the concurrency slot was released too early"
            )
            assert blocked.json()["detail"] == "concurrency_cap_exceeded"

            await task  # the stream finishes and releases its slot

            freed = await ac.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=_body(max_tokens=5),
            )
            assert freed.status_code == 200, "the slot was never released"


class TestStreamedUsageFeedsTheBudget:
    """Proves the SSE usage-frame parsing in chat.py's _stream actually runs.

    Without it, a streamed request's tokens never reach QuotaTracker at all,
    and this test's second call would wrongly succeed forever regardless of
    `token_budget`.
    """

    async def test_a_streamed_requests_tokens_count_against_the_budget(
        self, live_gateway
    ):
        base, key = live_gateway(key_kwargs={"token_budget": 5})
        async with httpx.AsyncClient(timeout=10.0) as ac:
            r1 = await ac.post(f"{base}/v1/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=_body(stream=True, max_tokens=20))
            assert r1.status_code == 200

            r2 = await ac.post(f"{base}/v1/chat/completions",
                              headers={"Authorization": f"Bearer {key}"},
                              json=_body(max_tokens=5))
            assert r2.status_code == 429
            assert r2.json()["detail"] == "token_budget_exceeded"

    async def test_an_explicit_include_usage_false_is_respected(self, live_gateway):
        """The gateway defaults usage on but must not override a caller's `False`.

        Without an observed usage frame, requests_missing_usage_total is the
        counter that goes up instead of tokens_total -- checked directly
        against the live process's own /metrics, not the in-process REGISTRY,
        since this gateway is a separate subprocess.
        """
        base, key = live_gateway()
        async with httpx.AsyncClient(timeout=10.0) as ac:
            before_text = (await ac.get(f"{base}/metrics")).text
            before = _metric(before_text, "bkn301_gateway_requests_missing_usage_total",
                             model="Qwen/Qwen3-8B-AWQ") or 0.0

            r = await ac.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=_body(stream=True, max_tokens=10,
                          stream_options={"include_usage": False}),
            )
            assert r.status_code == 200

            after_text = (await ac.get(f"{base}/metrics")).text
            after = _metric(after_text, "bkn301_gateway_requests_missing_usage_total",
                            model="Qwen/Qwen3-8B-AWQ") or 0.0

        assert after == before + 1, (
            "an explicit include_usage=False was overridden by the gateway's "
            "default, or the missing-usage gap stopped being counted"
        )
