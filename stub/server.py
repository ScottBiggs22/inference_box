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
    /metrics                    (the four vLLM gauges the probe design reads)

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
from fastapi.responses import StreamingResponse

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
        # Mirrors vLLM's /metrics gauges. Static unless a request is in flight;
        # enough for the gateway's scrape path to be exercised end to end.
        self.running = 0
        self.waiting = 0
        self.generated_tokens = 0


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


@app.get("/metrics")
def metrics():
    """The four gauges the wedge-detection design reads (PRD §4.6).

    `vllm:generation_tokens_total` staying flat while `num_requests_waiting` is
    above zero is the wedge signature -- it is a relationship between two series
    over time, which is why it cannot be an HTTP probe and needs a scraper.
    """
    return "\n".join(
        [
            "# TYPE vllm:num_requests_running gauge",
            f"vllm:num_requests_running {state.running}",
            "# TYPE vllm:num_requests_waiting gauge",
            f"vllm:num_requests_waiting {state.waiting}",
            "# TYPE vllm:generation_tokens_total counter",
            f"vllm:generation_tokens_total {state.generated_tokens}",
            "# TYPE vllm:time_to_first_token_seconds histogram",
            "vllm:time_to_first_token_seconds_sum 0.0",
            "vllm:time_to_first_token_seconds_count 0",
        ]
    )


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

    max_tokens = int(body.get("max_tokens") or 64)
    text = _completion_text(body, max_tokens)

    state.running += 1
    try:
        if state.latency:
            await asyncio.sleep(state.latency)
    finally:
        state.running -= 1

    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in body["messages"])
    completion_tokens = len(text.split())
    state.generated_tokens += completion_tokens

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

    yield frame({"role": "assistant", "content": ""})
    for word in text.split():
        if state.latency:
            await asyncio.sleep(min(state.latency / 50.0, 0.05))
        yield frame({"content": word + " "})
    yield frame({}, finish="stop")
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
    args = ap.parse_args()

    state.think = args.stub_think
    state.latency = args.stub_latency
    state.fail_after = args.stub_fail_after

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
