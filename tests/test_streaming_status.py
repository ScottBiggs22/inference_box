"""The streaming path must report the upstream's status, not always 200.

Before this, `_stream` built a StreamingResponse with the default status and read
the upstream's only inside the generator -- which runs after the headers are
already on the wire. An upstream 503 reached the caller as HTTP 200 with an error
body dressed up as a stream, and the real status reached only the audit log.
"""
from __future__ import annotations

import json
import time

import httpx

from gateway.auth import get_key_store
from gateway.main import create_app


def _client(key_store, stub_base):
    from fastapi.testclient import TestClient
    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: key_store
    return TestClient(app)


def _body(**over):
    b = {"model": "Qwen/Qwen3-8B-AWQ",
         "messages": [{"role": "user", "content": "hello there"}]}
    b.update(over)
    return b


class TestUpstreamErrorOnAStreamedRequest:
    def test_upstream_503_is_not_reported_as_200(
        self, key_store, live_key, audit_file, stub_upstream
    ):
        """The regression. --stub-fail-after 0 fails every chat request."""
        base = stub_upstream("--stub-fail-after", "0")
        with _client(key_store, base) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {live_key}"},
                       json=_body(stream=True))
        assert r.status_code == 503, (
            f"upstream 503 surfaced to the caller as {r.status_code}"
        )

    def test_no_sse_bytes_are_emitted_on_an_upstream_error(
        self, key_store, live_key, audit_file, stub_upstream
    ):
        """A caller must not receive an error body framed as a stream."""
        base = stub_upstream("--stub-fail-after", "0")
        with _client(key_store, base) as c:
            r = c.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {live_key}"},
                       json=_body(stream=True))
        assert b"data: " not in r.content

    def test_the_failure_is_audited(
        self, key_store, live_key, audit_file, stub_upstream
    ):
        base = stub_upstream("--stub-fail-after", "0")
        with _client(key_store, base) as c:
            c.post("/v1/chat/completions",
                   headers={"Authorization": f"Bearer {live_key}"},
                   json=_body(stream=True))
        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert records[-1]["status"] == 503
        assert records[-1]["reason"] == "upstream_error"


class TestSuccessStillStreamsUnbuffered:
    """Runs over a REAL SOCKET, deliberately.

    TestClient's ASGI transport collects a streaming response before returning
    it, so `iter_raw()` there always yields one chunk with zero spread -- these
    assertions would pass against an implementation that buffered everything.
    """

    def test_frames_arrive_incrementally(self, live_gateway):
        """The property the whole passthrough design exists to protect.

        Until now NO test streamed a real body through the gateway, because
        `client` always pulls in `dead_upstream`, so a change that assembled the
        response could have landed with the suite green. PRD §6.3 C2.

        Scope, honestly: this catches ACCUMULATION -- verified by making the relay
        collect chunks first, which fails here with "43 chunks arrived within
        0.000s". It does NOT catch BaseHTTPMiddleware, which was measured to pass
        this test: its memory object stream has zero capacity, so frames still
        hand over one at a time. The objection to that middleware is a task-group
        hop per frame and altered disconnect semantics, and this test is not
        evidence either way about it.
        """
        base, key = live_gateway("--stub-latency", "2.0")
        stamps = []
        with httpx.stream(
            "POST", f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=_body(stream=True, max_tokens=40), timeout=30.0,
        ) as r:
            assert r.status_code == 200
            t0 = time.perf_counter()
            for _chunk in r.iter_raw():
                stamps.append(time.perf_counter() - t0)

        assert len(stamps) > 5, f"only {len(stamps)} chunks; the body was assembled"
        assert stamps[-1] - stamps[0] > 0.5, (
            f"all {len(stamps)} chunks arrived within {stamps[-1] - stamps[0]:.3f}s: "
            "the response is being buffered, which defeats the streaming design"
        )

    def test_a_normal_stream_still_terminates_with_done(self, live_gateway):
        base, key = live_gateway()
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_body(stream=True, max_tokens=20), timeout=30.0)
        assert r.status_code == 200
        assert r.text.rstrip().endswith("data: [DONE]")

    def test_upstream_503_over_a_real_socket_is_503(self, live_gateway):
        """Belt and braces: the same assertion, without the in-process transport."""
        base, key = live_gateway("--stub-fail-after", "0")
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_body(stream=True), timeout=30.0)
        assert r.status_code == 503
        assert b"data: " not in r.content
