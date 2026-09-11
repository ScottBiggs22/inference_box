"""A fake OpenAI-compatible server, for developing without a GPU.

WHY THIS EXISTS
===============
An M-series MacBook has no CUDA, and Docker Desktop on macOS has no GPU
passthrough, so vLLM cannot run locally at all -- not slowly, not at reduced
precision, not at all (PRD §4.7). Without a stand-in, every line of gateway code
would be blocked on a rented or shared GPU box being up.

So this serves the parts of the OpenAI surface the gateway and the AI service
actually touch, on the laptop, in-process, deterministically:

    /v1/models
    /v1/chat/completions        (streaming and non-streaming)
    /health                     (mimics vLLM's own readiness semantics)
    /metrics                    (vLLM's real series, with its real label set)

DETERMINISTIC BY DESIGN
=======================
Responses are generated from a hash of the request, not sampled. That makes the
stub usable as a test fixture: the same request always yields the same bytes, so
a gateway test can assert on a response body without pinning a model. It is NOT
trying to be a language model -- it produces filler of a requested length. Any
test asserting on answer *quality* against this stub is testing nothing.

WHAT IT DELIBERATELY REPRODUCES
===============================
Three behaviours exist here only because the real thing has them and the gateway
must cope:

  * `--stub-think` emits a Qwen3 <think> block, so the B3 stripper can be tested
    against the failure it exists for.
  * `--stub-latency` adds a delay, so probe tuning and the gateway's separate
    connect/read budgets can be exercised. PRD §4.6 warns that a naively tuned
    liveness probe kills healthy pods precisely because a legitimate generation
    can take 10s+.
  * `--stub-fail-after N` starts returning 503 after N requests, for the circuit
    breaker.

Run:
    python -m stub.server --port 8001
    python -m stub.server --port 8001 --stub-think --stub-latency 2.0
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

STUB_MODEL = "Qwen/Qwen3-8B-AWQ"

# Filler vocabulary. Domain-flavoured so a human reading a test failure can tell
# at a glance that they are looking at stub output and not a real answer.
_WORDS = (
    "the", "asset", "portfolio", "position", "reported", "quarter", "value",
    "token", "supply", "investor", "compliance", "review", "figure", "record",
    "allocation", "yield", "treasury", "balance", "status", "summary",
)


class StubState:
    def __init__(self) -> None:
        self.think = False
        self.latency = 0.0
        self.fail_after: int | None = None
        self.requests = 0
        # Mirrors vLLM's /metrics. REAL counters now, not decoration: `waiting`
        # was previously initialised to 0 and written by nothing at all, which
        # made the wedge signature -- waiting > 0 while generated_tokens stays
        # flat -- literally unreachable through this server, even though the
        # /metrics docstring said that was what it existed for.
        self.running = 0
        self.waiting = 0
        self.generated_tokens = 0
        # When set, a chat request queues itself and then never completes: the
        # socket stays open and /health and /metrics keep answering 200. That is
        # what a wedged engine looks like, and why no HTTP probe can see one.
        self.wedge = False
        # Admission control, so queue depth is a consequence of concurrency
        # rather than a decoration. Built lazily: a semaphore binds to the loop
        # that first awaits it, and StubState is constructed at import.
        self.max_concurrent = 0
        self._sem: asyncio.Semaphore | None = None

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrent or 1024)
        return self._sem


state = StubState()
app = FastAPI(title="Stub OpenAI-compatible server", version="0.1.0")


def _deterministic_text(seed_material: str, n_tokens: int) -> str:
    """Filler of roughly n_tokens words, stable for identical input."""
    digest = hashlib.sha256(seed_material.encode("utf-8")).digest()
    out = []
    for i in range(max(1, n_tokens)):
        out.append(_WORDS[digest[i % len(digest)] % len(_WORDS)])
    text = " ".join(out)
    return text[0].upper() + text[1:] + "."


def _seed_of(body: dict) -> str:
    return json.dumps(body.get("messages", []), sort_keys=True, ensure_ascii=False)


def _completion_text(body: dict, n_tokens: int) -> str:
    text = _deterministic_text(_seed_of(body), n_tokens)
    if not state.think:
        return text
    # Reproduce the exact shape that empties an answer: a reasoning block the
    # caller must strip. See openai_http.strip_think.
    return f"<think>\n{_deterministic_text('think' + _seed_of(body), 40)}\n</think>\n\n{text}"


@app.get("/health")
def health():
    """vLLM returns 200 here only once the engine is up. Startup probe target."""
    return {"status": "ok"}


# The real vLLM 0.28.0 label set, captured from a live server in
# results/phase1-vastai-a10/vllm-metrics-final.txt. Emitting UNLABELLED series
# here -- as this endpoint used to -- is worse than emitting nothing: a scraper
# written against it parses cleanly, passes every local test, and then matches
# nothing whatsoever against a real server.
_LABELS = f'engine="0",model_name="{STUB_MODEL}"'


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """vLLM's exposition, in the shape a real one actually has.

    `response_class=PlainTextResponse` is load-bearing. Returning a bare `str`
    from a FastAPI route JSON-encodes it -- quoted, newlines escaped, served as
    application/json -- and no Prometheus parser will accept that. It was this
    endpoint's second defect.

    Three traps are reproduced ON PURPOSE, because a stub that emits only the
    happy shape is how a parser bug reaches production:

      * `_created` siblings. Every counter has one; it is a GAUGE whose value is
        a boot timestamp that never changes. A parser matching by prefix picks it
        up and then sees a permanently flat counter -- wedge-alerting forever.
      * `num_requests_waiting_by_reason`, which SUMS to `num_requests_waiting`
        and so double-counts anything totalling naively.
      * the `_total` suffix, which prometheus_client's parser strips from counter
        FAMILY names while keeping it on the sample.
    """
    created = 1.789056823729462e09
    return "\n".join([
        "# TYPE vllm:num_requests_running gauge",
        f"vllm:num_requests_running{{{_LABELS}}} {float(state.running)}",
        "# TYPE vllm:num_requests_waiting gauge",
        f"vllm:num_requests_waiting{{{_LABELS}}} {float(state.waiting)}",
        "# TYPE vllm:num_requests_waiting_by_reason gauge",
        f'vllm:num_requests_waiting_by_reason{{{_LABELS},reason="capacity"}} '
        f'{float(state.waiting)}',
        f'vllm:num_requests_waiting_by_reason{{{_LABELS},reason="deferred"}} 0.0',
        "# TYPE vllm:kv_cache_usage_perc gauge",
        f"vllm:kv_cache_usage_perc{{{_LABELS}}} 0.0",
        "# TYPE vllm:generation_tokens_total counter",
        f"vllm:generation_tokens_total{{{_LABELS}}} {float(state.generated_tokens)}",
        "# TYPE vllm:generation_tokens_created gauge",
        f"vllm:generation_tokens_created{{{_LABELS}}} {created}",
        "# TYPE vllm:prompt_tokens_total counter",
        f"vllm:prompt_tokens_total{{{_LABELS}}} 0.0",
        "# TYPE vllm:num_preemptions_total counter",
        f"vllm:num_preemptions_total{{{_LABELS}}} 0.0",
        "# TYPE vllm:prefix_cache_queries_total counter",
        f"vllm:prefix_cache_queries_total{{{_LABELS}}} 0.0",
        "# TYPE vllm:prefix_cache_hits_total counter",
        f"vllm:prefix_cache_hits_total{{{_LABELS}}} 0.0",
        "# TYPE vllm:time_to_first_token_seconds histogram",
        f'vllm:time_to_first_token_seconds_bucket{{{_LABELS},le="+Inf"}} 0.0',
        f"vllm:time_to_first_token_seconds_sum{{{_LABELS}}} 0.0",
        f"vllm:time_to_first_token_seconds_count{{{_LABELS}}} 0.0",
        "",
    ])


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": STUB_MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "stub",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    state.requests += 1

    if state.fail_after is not None and state.requests > state.fail_after:
        # 503 rather than 500: it is the retryable status the gateway's bounded
        # retry-with-jitter and its breaker both key on.
        raise HTTPException(status_code=503, detail="stub: simulated overload")

    if not body.get("messages"):
        raise HTTPException(status_code=400, detail="messages is required")

    if state.wedge:
        # Register as queued, then never finish. PROBE_CONTRACT.md §4's signature
        # needs exactly this: work waiting while the token counter does not move.
        state.waiting += 1
        await asyncio.Event().wait()  # never returns; the caller must time out

    max_tokens = int(body.get("max_tokens") or 64)
    text = _completion_text(body, max_tokens)
    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in body["messages"])
    completion_tokens = len(text.split())

    state.waiting += 1
    async with state.sem:
        state.waiting -= 1
        state.running += 1
        try:
            if state.latency:
                await asyncio.sleep(state.latency)
        finally:
            state.running -= 1

    created = int(time.time())
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    if body.get("stream"):
        # `stream_options.include_usage` is opt-in in the OpenAI protocol, and
        # the gateway's per-key token budget (PRD §5.3) depends on it: without
        # it a streamed request has no token counts and the audit record can
        # only log zeros. Reproduced here so BOTH paths -- server-reported
        # counts and the consumer's delta-counting fallback -- are testable
        # without a GPU.
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        return StreamingResponse(
            _stream(cid, created, body.get("model", STUB_MODEL), text,
                    usage if include_usage else None),
            media_type="text/event-stream",
        )

    # Non-streamed: the whole completion exists now, so count it now. The
    # streamed path counts per frame inside _stream instead.
    state.generated_tokens += completion_tokens
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": body.get("model", STUB_MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


async def _stream(cid: str, created: int, model: str, text: str,
                  usage: dict | None = None):
    """SSE in the OpenAI delta format, terminated by the literal [DONE].

    The gateway passes this through rather than buffering it, so the exact frame
    shape matters: a client that parses `data: ` lines and stops at [DONE] must
    work against the stub and against vLLM without changes.
    """
    def frame(delta: dict, finish=None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # `running` spans the WHOLE stream. It used to cover only the latency sleep,
    # so it dropped to 0 while tokens were still being emitted -- inverted
    # relative to a real engine, and exactly wrong for anything reading it.
    state.running += 1
    try:
        yield frame({"role": "assistant", "content": ""})
        for word in text.split():
            if state.latency:
                await asyncio.sleep(min(state.latency / 50.0, 0.05))
            # Per frame, not one jump before the first byte is sent. The old
            # accounting meant a scraper sampling mid-stream saw an
            # already-advanced counter -- the wedge condition, inverted.
            state.generated_tokens += 1
            yield frame({"content": word + " "})
        yield frame({}, finish="stop")
    finally:
        state.running -= 1
    if usage is not None:
        # The usage frame comes last and carries an EMPTY choices list. A
        # consumer that assumes every frame has a choices[0] crashes on it,
        # which is exactly the shape worth having a fixture for.
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--stub-think", action="store_true",
                    help="Emit a Qwen3 <think> block, to exercise the B3 stripper.")
    ap.add_argument("--stub-latency", type=float, default=0.0,
                    help="Seconds of simulated decode time per request.")
    ap.add_argument("--stub-fail-after", type=int, default=None,
                    help="Return 503 after this many requests, for the breaker.")
    ap.add_argument("--stub-wedge", action="store_true",
                    help="Accept chat requests, queue them, and never answer -- "
                         "the wedge signature PROBE_CONTRACT.md 4 describes.")
    ap.add_argument("--stub-max-concurrent", type=int, default=0,
                    help="Admission limit, so num_requests_waiting is real.")
    args = ap.parse_args()

    state.think = args.stub_think
    state.latency = args.stub_latency
    state.fail_after = args.stub_fail_after
    state.wedge = args.stub_wedge
    state.max_concurrent = args.stub_max_concurrent

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
