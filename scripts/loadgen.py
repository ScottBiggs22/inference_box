#!/usr/bin/env python3
"""Concurrency and throughput load generator for the Phase 1 measurements.

WHY THIS EXISTS RATHER THAN REUSING THE APP'S BENCH HARNESS
===========================================================
`baas-poc-templates/.../scripts/bench_llm_backends.py` cannot produce any of the
three headline Phase 1 numbers, which the audit on 2026-09-09 established:

  * It has NO concurrency support at all -- no flag, no threads, no asyncio, no
    multiprocessing; every loop is strictly sequential. The runbook nonetheless
    instructed "run 1/3/5/8 in parallel and report tok/s per user at each
    level". It cannot.
  * It never measures TTFT, and it cannot: the vllm backend hardcodes
    `"stream": False`, so there is no first-token timestamp to take.
  * Its `decode_tok_s` is `completion_tokens / whole_request_latency`, which
    conflates prefill with decode. PRD §3.3's ~48 tok/s/user is a DECODE
    figure. The two numbers are not comparable, and reporting one as the other
    would have understated the card.

This script is also deliberately in the gateway repo rather than the app repo,
because it must run on a rented box and the app repo carries real data: its
eval suite retrieves from the real knowledge base and injects the chunks into
the prompt, so real content would travel in the request body regardless of how
the code got there (PRD §4.8).

WHAT IT MEASURES, AND THE DEFINITIONS IT USES
=============================================
  TTFT            request sent -> first content delta. Prefill plus queueing.
  decode tok/s    (completion_tokens - 1) / (last delta - first delta).
                  The -1 is not fussiness: the first token is produced BY
                  prefill, so counting it as decode inflates the rate, and at
                  the short completion lengths a benchmark tends to use, that
                  inflation is several percent.
  per-user        the MEDIAN of per-request decode rates at a level. This is
                  what R1 (">=20 output tok/s per user") is about, and what
                  DevOps multiplies. Reported alongside, never replaced by, the
                  aggregate.
  aggregate       total completion tokens / wall time of the level. Rises with
                  concurrency as continuous batching does its job; per-user
                  falls. Both matter and they are different questions.

PREFIX CACHING IS MODELLED, NOT ACCIDENTALLY DEFEATED OR EXPLOITED
==================================================================
vLLM v1 has automatic prefix caching on by default (PRD §3.6). Sending N
identical prompts would measure the cache, not the model; sending N wholly
unrelated ones would measure a cache-miss path production never sees. Real
traffic is a large byte-identical system-and-knowledge block plus a short
unique query, so that is the default shape here: a shared padded prefix and a
per-request nonce. `--unique-prefix` forces total misses for a worst-case
prefill number.

SYNTHETIC BY CONSTRUCTION
=========================
Prompts are filler assembled from the domain-flavoured vocabulary in
`stub/server.py`, which exists precisely so a human reading output can tell at
a glance it is not real. Nothing here reads a corpus, a fixture, or a log.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

# Reused rather than redefined: this is the stub's filler vocabulary, and having
# one synthetic vocabulary across the stub and the load generator means output
# from either is recognisable as non-real by the same tell.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stub.server import _WORDS  # noqa: E402


def pct(values: list[float], q: float) -> float | None:
    """Interpolating percentile, matching the app harness's `pct` so numbers
    from the two tools are directly comparable rather than nearly so."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    frac = pos - lo
    return round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 2)


@dataclass
class RequestResult:
    level: int
    worker: int
    ok: bool
    status: int | None = None
    error: str | None = None
    ttft_ms: float | None = None
    decode_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    decode_tok_s: float | None = None
    usage_reported: bool = False


@dataclass
class LevelSummary:
    kind: str = "level"
    concurrency: int = 0
    requests: int = 0
    failures: int = 0
    wall_s: float = 0.0
    # The R1 number. Median of per-request decode rates.
    per_user_decode_tok_s_p50: float | None = None
    per_user_decode_tok_s_min: float | None = None
    aggregate_decode_tok_s: float | None = None
    ttft_ms_p50: float | None = None
    ttft_ms_p95: float | None = None
    completion_tokens_total: int = 0
    prompt_tokens_total: int = 0
    usage_reported_by_server: bool = False
    errors: dict[str, int] = field(default_factory=dict)


def build_prompt(shared_prefix: str, ctx_tokens: int, unique_prefix: bool) -> list[dict]:
    """A shared system block plus a unique user turn.

    Word count, not token count, is what is controlled here. English runs about
    0.75 words per token, so the filler is sized accordingly and the true token
    count is confirmed from the server's own `usage.prompt_tokens` rather than
    guessed -- which is also why `--ctx-tokens` is documented as approximate.
    """
    nonce = uuid.uuid4().hex
    prefix = _filler(int(ctx_tokens * 0.75), seed=nonce if unique_prefix else None) \
        if unique_prefix else shared_prefix
    return [
        {"role": "system", "content": prefix},
        {"role": "user", "content": f"Summarise position {nonce} in one paragraph."},
    ]


def _filler(n_words: int, seed: str | None = None) -> str:
    """Deterministic unless a seed is given. No corpus, no fixture, no log."""
    import hashlib

    material = (seed or "shared-prefix").encode()
    digest = hashlib.sha256(material).digest()
    out = [_WORDS[digest[i % len(digest)] % len(_WORDS)] for i in range(max(1, n_words))]
    return " ".join(out)


async def one_request(client: httpx.AsyncClient, url: str, body: dict,
                      level: int, worker: int) -> RequestResult:
    """One streamed completion, timed at the frame level.

    Timing is taken on the first frame that carries actual CONTENT, not the
    first frame of any kind: vLLM (like the stub) sends an opening frame with
    an empty-string delta to establish the role, and treating that as the first
    token would report a TTFT of nearly zero.
    """
    res = RequestResult(level=level, worker=worker, ok=False)
    t0 = time.perf_counter()
    t_first: float | None = None
    t_last: float | None = None
    delta_count = 0
    usage: dict | None = None

    try:
        async with client.stream("POST", url, json=body) as resp:
            res.status = resp.status_code
            if resp.status_code >= 400:
                await resp.aread()
                res.error = f"http_{resp.status_code}"
                return res
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):].strip()
                if payload == "[DONE]":
                    break
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # The usage frame arrives last and has an empty choices list.
                if frame.get("usage"):
                    usage = frame["usage"]
                choices = frame.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content") or ""
                if content:
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    t_last = now
                    delta_count += 1
    except Exception as e:                                   # noqa: BLE001
        res.error = f"{type(e).__name__}"
        return res

    t_end = time.perf_counter()
    res.total_ms = (t_end - t0) * 1000.0

    if t_first is None:
        # A completed stream that produced no content is the B3 failure shape
        # (an unterminated <think> block consuming the whole allowance), and it
        # must not be averaged away as a fast request.
        res.error = "empty_completion"
        return res

    res.ok = True
    res.ttft_ms = (t_first - t0) * 1000.0
    res.decode_ms = (t_last - t_first) * 1000.0

    if usage:
        res.usage_reported = True
        res.prompt_tokens = usage.get("prompt_tokens")
        res.completion_tokens = usage.get("completion_tokens")
    if res.completion_tokens is None:
        # Fallback: count content frames. Approximate -- a frame is not
        # guaranteed to be exactly one token -- so it is flagged as unreported
        # rather than silently presented as the server's own count.
        res.completion_tokens = delta_count

    n = res.completion_tokens or 0
    if n > 1 and res.decode_ms and res.decode_ms > 0:
        res.decode_tok_s = round((n - 1) / (res.decode_ms / 1000.0), 2)
    return res


async def run_level(base_url: str, headers: dict, model: str, level: int,
                    requests_per_worker: int, ctx_tokens: int, max_tokens: int,
                    unique_prefix: bool, timeout: float,
                    shared_prefix: str) -> tuple[LevelSummary, list[RequestResult]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    results: list[RequestResult] = []

    async def worker(idx: int, client: httpx.AsyncClient) -> None:
        for _ in range(requests_per_worker):
            body = {
                "model": model,
                "messages": build_prompt(shared_prefix, ctx_tokens, unique_prefix),
                "max_tokens": max_tokens,
                "stream": True,
                # Real token counts from the server. Also the exact plumbing the
                # Phase 2 per-key token budget needs, so this pre-validates it.
                "stream_options": {"include_usage": True},
                "temperature": 0.7, "top_p": 0.8,
            }
            results.append(await one_request(client, url, body, level, idx))

    limits = httpx.Limits(max_connections=level + 4, max_keepalive_connections=level + 4)
    async with httpx.AsyncClient(
        headers=headers, limits=limits,
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=30.0),
    ) as client:
        t0 = time.perf_counter()
        await asyncio.gather(*(worker(i, client) for i in range(level)))
        wall = time.perf_counter() - t0

    ok = [r for r in results if r.ok]
    rates = [r.decode_tok_s for r in ok if r.decode_tok_s is not None]
    ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
    comp = sum(r.completion_tokens or 0 for r in ok)
    errors: dict[str, int] = {}
    for r in results:
        if not r.ok:
            errors[r.error or "unknown"] = errors.get(r.error or "unknown", 0) + 1

    summary = LevelSummary(
        concurrency=level,
        requests=len(results),
        failures=len(results) - len(ok),
        wall_s=round(wall, 3),
        per_user_decode_tok_s_p50=pct(rates, 0.50),
        per_user_decode_tok_s_min=round(min(rates), 2) if rates else None,
        aggregate_decode_tok_s=round(comp / wall, 2) if wall > 0 else None,
        ttft_ms_p50=pct(ttfts, 0.50),
        ttft_ms_p95=pct(ttfts, 0.95),
        completion_tokens_total=comp,
        prompt_tokens_total=sum(r.prompt_tokens or 0 for r in ok),
        usage_reported_by_server=any(r.usage_reported for r in ok),
        errors=errors,
    )
    return summary, results


async def amain(args: argparse.Namespace) -> int:
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    # Sized once so every request at every level shares byte-identical prefix
    # bytes -- which is what makes the prefix-cache behaviour the realistic one.
    shared_prefix = _filler(int(args.ctx_tokens * 0.75))

    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    header = {
        "kind": "header",
        "tool": "loadgen.py",
        "target": args.url,
        "direct": args.direct,
        "model": args.model,
        "levels": levels,
        "requests_per_worker": args.requests,
        "ctx_tokens_requested": args.ctx_tokens,
        "max_tokens": args.max_tokens,
        "unique_prefix": args.unique_prefix,
        "warmup": args.warmup,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # PRD §4.8: this must accompany the numbers wherever they go.
        "provenance_warning": (
            "Rented-box figures are a PROVISIONAL baseline. A vast.ai host has "
            "different CPU, PCIe topology and memory bandwidth from "
            "VM.GPU.A10.1. Re-confirm on the OCI card before anyone multiplies "
            "these for procurement."
        ),
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")

        if args.warmup:
            print(f"warmup: {args.warmup} request(s), excluded from all statistics")
            await run_level(args.url, headers, args.model, 1, args.warmup,
                            args.ctx_tokens, args.max_tokens, args.unique_prefix,
                            args.timeout, shared_prefix)

        print()
        print(f"{'conc':>5}  {'per-user tok/s':>15}  {'min':>7}  {'aggregate':>10}  "
              f"{'TTFT p50':>9}  {'TTFT p95':>9}  {'fail':>5}")
        print("-" * 76)

        summaries = []
        for level in levels:
            summary, results = await run_level(
                args.url, headers, args.model, level, args.requests,
                args.ctx_tokens, args.max_tokens, args.unique_prefix,
                args.timeout, shared_prefix,
            )
            summaries.append(summary)
            for r in results:
                fh.write(json.dumps({"kind": "request", **asdict(r)}) + "\n")
            fh.write(json.dumps(asdict(summary)) + "\n")
            fh.flush()

            def _f(v, unit=""):
                return f"{v}{unit}" if v is not None else "-"

            print(f"{level:>5}  {_f(summary.per_user_decode_tok_s_p50):>15}  "
                  f"{_f(summary.per_user_decode_tok_s_min):>7}  "
                  f"{_f(summary.aggregate_decode_tok_s):>10}  "
                  f"{_f(summary.ttft_ms_p50):>9}  {_f(summary.ttft_ms_p95):>9}  "
                  f"{summary.failures:>5}")
            if summary.errors:
                print(f"         errors: {summary.errors}")
            if level != levels[-1]:
                await asyncio.sleep(args.settle)

    print()
    print(f"wrote {out}")

    # ── The gate, stated rather than left to the reader ──────────────────────
    # PRD §7 Phase 1 exit gate: >=20 tok/s/user at 5 concurrent with 8k context.
    gate = next((s for s in summaries if s.concurrency == 5), None)
    if gate and gate.per_user_decode_tok_s_p50 is not None:
        verdict = "MET" if gate.per_user_decode_tok_s_p50 >= 20 else "NOT MET"
        print(f"R1 gate (>=20 tok/s/user at 5 concurrent): {verdict} "
              f"({gate.per_user_decode_tok_s_p50} tok/s/user, "
              f"ctx~{args.ctx_tokens} requested)")
    if not any(s.usage_reported_by_server for s in summaries):
        print("NOTE: the server never returned a usage frame; token counts are "
              "delta-frame estimates, not server counts. Flag this when reporting.")

    total_failures = sum(s.failures for s in summaries)
    if total_failures:
        print(f"WARNING: {total_failures} request(s) failed. Numbers above cover "
              f"successes only -- do not report them without the failure count.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure per-user decode tok/s and TTFT at 1/3/5/8 concurrent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run this ON THE BOX, against the gateway on loopback. Running it\n"
            "from the laptop over the SSH tunnel inflates TTFT by the round\n"
            "trip, which is the caveat the old runbook had to attach to every\n"
            "latency figure it produced.\n"
        ),
    )
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1",
                    help="Gateway /v1 base. Use the vLLM /v1 with --direct.")
    ap.add_argument("--direct", action="store_true",
                    help="Recorded in the header: this run bypassed the gateway. "
                         "Pair with a gateway run to get the gateway's overhead.")
    ap.add_argument("--api-key", default="",
                    help="Throwaway gateway key, or vLLM's static --api-key.")
    ap.add_argument("--model", default="Qwen/Qwen3-8B-AWQ")
    ap.add_argument("--levels", default="1,3,5,8",
                    help="Concurrency levels, ascending. Default 1,3,5,8 (PRD §7).")
    ap.add_argument("--requests", type=int, default=4,
                    help="Requests per worker per level.")
    ap.add_argument("--ctx-tokens", type=int, default=7000,
                    help="Approximate prompt size. 7000 + 512 output exercises the "
                         "8k context the R4 goal and the exit gate are about; five "
                         "short prompts do not test it at all.")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--model-len", type=int, default=8192,
                    help="Server's --max-model-len, for the pre-flight check below.")
    ap.add_argument("--unique-prefix", action="store_true",
                    help="Defeat prefix caching for a worst-case prefill figure.")
    ap.add_argument("--warmup", type=int, default=2,
                    help="Unrecorded requests first. The first request pays CUDA "
                         "graph capture and lazy init, which would otherwise land "
                         "entirely on the level-1 numbers.")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="Seconds between levels, so one level's tail does not "
                         "batch with the next level's head.")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("-o", "--output", default="logs/loadgen.jsonl")
    args = ap.parse_args()

    # Pre-flight. Getting this wrong wastes paid box time on a run that fails
    # every request with a 400 nobody reads until the results are empty.
    if args.ctx_tokens + args.max_tokens > args.model_len:
        print(f"ERROR: ctx-tokens ({args.ctx_tokens}) + max-tokens "
              f"({args.max_tokens}) = {args.ctx_tokens + args.max_tokens} exceeds "
              f"--model-len {args.model_len}. vLLM will reject every request.",
              file=sys.stderr)
        return 2

    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
