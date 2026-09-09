"""`scripts/loadgen.py` — the Phase 1 instrument.

WHY THESE TESTS ARE END-TO-END AGAINST A REAL SOCKET
====================================================
The app repo's bench harness looks like it measures throughput and does not:
it has no concurrency at all, never takes a first-token timestamp, and its
`decode_tok_s` divides completion tokens by whole-request latency, conflating
prefill with decode. Every one of those defects would pass a unit test of the
arithmetic. So the tests that matter here assert the properties the harness
lacked:

  * the workers genuinely overlap (proved by wall-clock, not by inspection);
  * TTFT and decode rate come out as two separate numbers;
  * server-reported token counts are used when offered and the delta-counting
    fallback is used when they are not, and the difference is visible in the
    output rather than silently smoothed over;
  * a failure is reported as a failure.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
LOADGEN = REPO / "scripts" / "loadgen.py"


def _loadgen_module():
    """Import loadgen.py by path.

    It is a script in `scripts/`, not a package module -- deliberately, since
    it has to be runnable on a bare rented box with nothing installed but this
    repo. Loading it by path keeps that true instead of adding a package just
    to make it importable from a test.
    """
    if "_loadgen" in sys.modules:
        return sys.modules["_loadgen"]
    spec = importlib.util.spec_from_file_location("_loadgen", LOADGEN)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves its own module's namespace
    # through `sys.modules[cls.__module__]`, so a module that is not yet
    # registered makes every dataclass in it fail to build.
    sys.modules["_loadgen"] = module
    spec.loader.exec_module(module)
    return module


def _run(out: Path, url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - interpreter plus literals
        [sys.executable, str(LOADGEN), "--url", f"{url}/v1",
         "--model", "Qwen/Qwen3-8B-AWQ", "-o", str(out), *args],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )


def _records(out: Path) -> tuple[dict, list[dict], list[dict]]:
    lines = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
    header = lines[0]
    return (header,
            [r for r in lines if r.get("kind") == "request"],
            [r for r in lines if r.get("kind") == "level"])


class TestMeasuresWhatItClaims:
    def test_ttft_and_decode_are_separate_numbers(self, live_stub, tmp_path):
        """The defect this tool exists to fix: one number for two questions."""
        url = live_stub("--stub-latency", "1.0")
        out = tmp_path / "lg.jsonl"
        result = _run(out, url, "--levels", "1", "--requests", "2",
                      "--ctx-tokens", "200", "--max-tokens", "32", "--warmup", "0")
        assert result.returncode == 0, result.stdout + result.stderr

        _, requests, levels = _records(out)
        assert requests, "no request records written"
        for r in requests:
            assert r["ok"], r
            assert r["ttft_ms"] is not None
            assert r["decode_ms"] is not None
            assert r["decode_tok_s"] is not None
            # Distinct measurements of distinct phases, not one divided twice.
            assert r["ttft_ms"] != r["decode_ms"]
            # total >= ttft + decode, since total also covers the closing frames.
            assert r["total_ms"] >= r["ttft_ms"] + r["decode_ms"] - 1.0
        assert levels[0]["ttft_ms_p50"] is not None
        assert levels[0]["per_user_decode_tok_s_p50"] is not None

    def test_decode_rate_excludes_the_prefill_token(self, live_stub, tmp_path):
        """(n-1)/decode_window, not n/decode_window.

        The first token is produced BY prefill, so counting it as decode
        inflates the rate -- materially so at the short completion lengths a
        benchmark uses.
        """
        url = live_stub()
        out = tmp_path / "lg.jsonl"
        assert _run(out, url, "--levels", "1", "--requests", "1",
                    "--ctx-tokens", "100", "--max-tokens", "40",
                    "--warmup", "0").returncode == 0
        _, requests, _ = _records(out)
        r = requests[0]
        expected = round((r["completion_tokens"] - 1) / (r["decode_ms"] / 1000.0), 2)
        assert r["decode_tok_s"] == pytest.approx(expected, rel=1e-6)


class TestConcurrencyIsReal:
    def test_workers_overlap(self, live_stub, tmp_path):
        """The property `bench_llm_backends.py` cannot have.

        The stub sleeps `--stub-latency` per request before streaming. Eight
        workers issuing one request each must therefore finish in roughly the
        time one takes, not eight times it. A sequential generator fails this
        by a wide margin, so the threshold does not need to be tight to be
        decisive.
        """
        url = live_stub("--stub-latency", "1.0")
        out = tmp_path / "lg.jsonl"
        assert _run(out, url, "--levels", "1,8", "--requests", "1",
                    "--ctx-tokens", "100", "--max-tokens", "16",
                    "--warmup", "0", "--settle", "0").returncode == 0

        _, _, levels = _records(out)
        one = next(x for x in levels if x["concurrency"] == 1)
        eight = next(x for x in levels if x["concurrency"] == 8)
        assert eight["requests"] == 8
        # Sequential would be ~8x. Allow generous slack for scheduling and CI.
        assert eight["wall_s"] < one["wall_s"] * 4, (
            f"8 concurrent took {eight['wall_s']}s vs {one['wall_s']}s for 1 -- "
            "the workers are not actually running in parallel"
        )

    def test_every_level_runs_every_request(self, live_stub, tmp_path):
        url = live_stub()
        out = tmp_path / "lg.jsonl"
        assert _run(out, url, "--levels", "1,3", "--requests", "2",
                    "--ctx-tokens", "100", "--max-tokens", "16",
                    "--warmup", "0", "--settle", "0").returncode == 0
        _, requests, levels = _records(out)
        assert {x["concurrency"]: x["requests"] for x in levels} == {1: 2, 3: 6}
        assert len(requests) == 8


class TestTokenCounts:
    def test_server_reported_usage_is_used_when_offered(self, live_stub, tmp_path):
        """`stream_options.include_usage` is what the Phase 2 budget needs."""
        url = live_stub()
        out = tmp_path / "lg.jsonl"
        assert _run(out, url, "--levels", "1", "--requests", "1",
                    "--ctx-tokens", "300", "--max-tokens", "24",
                    "--warmup", "0").returncode == 0
        _, requests, levels = _records(out)
        assert requests[0]["usage_reported"] is True
        assert levels[0]["usage_reported_by_server"] is True
        # The stub counts prompt tokens by words; a 300-token request is ~225
        # words, so a real count arrived rather than a zero.
        assert requests[0]["prompt_tokens"] > 100

    async def test_empty_completion_is_a_failure_not_a_fast_request(self):
        """The B3 shape: a stream that completes having produced no content.

        This is the exact failure PRD §6.1 B3 describes -- an unterminated
        `<think>` block consumes the whole `max_tokens` allowance and the answer
        arrives empty. It has to be a failure here, because a zero-token
        response is also the FASTEST possible response: averaged in, a
        uniformly degraded run reads as an excellent one, which is precisely how
        B3 was described as failing silently.
        """
        lg = _loadgen_module()

        def handler(request: httpx.Request) -> httpx.Response:
            # Role frame and finish frame, no content frame anywhere.
            body = (
                'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
                '"content":""},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{},'
                '"finish_reason":"length"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await lg.one_request(client, "http://x/v1/chat/completions",
                                       {}, level=1, worker=0)
        assert res.ok is False
        assert res.error == "empty_completion"
        assert res.decode_tok_s is None

    async def test_usage_absent_falls_back_to_counting_deltas(self):
        """Flagged as an estimate, not passed off as a server count."""
        lg = _loadgen_module()

        def handler(request: httpx.Request) -> httpx.Response:
            frames = [
                'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
                '"content":""},"finish_reason":null}]}',
            ]
            frames += [
                'data: {"choices":[{"index":0,"delta":{"content":"tok "},'
                '"finish_reason":null}]}'
            ] * 5
            frames.append('data: {"choices":[{"index":0,"delta":{},'
                          '"finish_reason":"stop"}]}')
            frames.append("data: [DONE]")
            return httpx.Response(200, text="\n\n".join(frames) + "\n\n",
                                  headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await lg.one_request(client, "http://x/v1/chat/completions",
                                       {}, level=1, worker=0)
        assert res.ok is True
        assert res.usage_reported is False, "no usage frame was sent"
        assert res.completion_tokens == 5, "should count content frames"

    async def test_usage_frame_with_empty_choices_does_not_crash(self):
        """vLLM's usage frame carries `choices: []`.

        A consumer indexing `choices[0]` unconditionally raises on it, and the
        request would be recorded as an exception rather than a success.
        """
        lg = _loadgen_module()

        def handler(request: httpx.Request) -> httpx.Response:
            body = (
                'data: {"choices":[{"index":0,"delta":{"content":"a "},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{"content":"b "},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":11,'
                '"completion_tokens":2,"total_tokens":13}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res = await lg.one_request(client, "http://x/v1/chat/completions",
                                       {}, level=1, worker=0)
        assert res.ok is True
        assert res.usage_reported is True
        assert res.prompt_tokens == 11
        assert res.completion_tokens == 2


class TestFailuresAreReportedAsFailures:
    def test_upstream_503_is_counted_and_exits_nonzero(self, live_stub, tmp_path):
        url = live_stub("--stub-fail-after", "1")
        out = tmp_path / "lg.jsonl"
        result = _run(out, url, "--levels", "3", "--requests", "2",
                      "--ctx-tokens", "100", "--max-tokens", "16", "--warmup", "0")
        assert result.returncode == 1, "a run with failures must not exit 0"
        assert "WARNING" in result.stdout and "failed" in result.stdout
        _, _, levels = _records(out)
        assert levels[0]["failures"] > 0
        assert any("503" in k for k in levels[0]["errors"])


class TestProvenance:
    def test_header_carries_the_rented_box_warning(self, live_stub, tmp_path):
        """PRD §4.8: these numbers must never travel without the caveat.

        DevOps sizes per-environment GPU counts off measured TPS, so a figure
        from a vast.ai host reaching a procurement spreadsheet unlabelled is a
        real risk. Putting it in the artefact means it survives being pasted.
        """
        url = live_stub()
        out = tmp_path / "lg.jsonl"
        assert _run(out, url, "--levels", "1", "--requests", "1",
                    "--ctx-tokens", "100", "--max-tokens", "16",
                    "--warmup", "0").returncode == 0
        header, _, _ = _records(out)
        assert header["kind"] == "header"
        assert "PROVISIONAL" in header["provenance_warning"]
        assert "VM.GPU.A10.1" in header["provenance_warning"]
        assert header["ctx_tokens_requested"] == 100


class TestPreFlight:
    def test_context_plus_output_over_model_len_is_refused(self, tmp_path):
        """Catches on the laptop what would otherwise 400 on every paid request."""
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(LOADGEN), "--ctx-tokens", "7000",
             "--max-tokens", "2048", "--model-len", "8192",
             "-o", str(tmp_path / "x.jsonl")],
            cwd=REPO, capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "exceeds" in result.stderr
        assert not (tmp_path / "x.jsonl").exists()
