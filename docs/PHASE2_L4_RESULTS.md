# Phase 2 GPU session — argon2-under-load and the wedge window, measured on a rented L4

**Status:** COMPLETE — measured 2026-09-14/15
**Box:** vast.ai instance `51076943`, machine `109523`, offer `49883396`, Utah US
**Card:** NVIDIA L4, 23034 MiB, driver 595.84, CUDA 13.2, 72 W, compute capability
8.9 (Ada) — **not an A10.** `scripts/verify_box.sh` correctly failed on the card
check; that failure was expected and accepted in advance (see below), not
overridden blindly.
**Cost:** $0.3232–0.3589/hr all-in. **Total spend $0.148** for the session
(balance $4.880 → $4.732). Instance destroyed, SSH key removed from the
account and locally, volumes confirmed empty.
**Operator:** scott.biggs@bkn301.com
**Raw artefacts:** `results/phase2-l4-wedge-argon2/`

---

## 0. Why an L4, and what that does and does not invalidate

`docs/HANDOFF.md` §5c left two measurements open, both blocked on GPU
availability: the wedge-detection window (still a 120s estimate) and the
argon2-threadpool fix (Phase 2, §1) unconfirmed under real concurrent decode
load. Re-checking vast.ai on 2026-09-14:

- The two live A10 offers (Netherlands, $0.24/hr) report `cuda_max_good: 12.8`
  — below the CUDA 13.0 the pinned `vllm/vllm-openai:v0.28.0` image needs.
  Booking either would reproduce the exact documented trap in
  `docs/VAST_AI_OPERATIONS.md` §3: boots, pulls ~16 GB, fails.
- The known-good Illinois machine from Phase 1 (`machine_id 123534`, CUDA
  13.2) is still listed but showed `rentable: false` across five repeated
  checks over 15 seconds — not a CUDA problem, just not bookable right now.
- L4 offers were available, real GPUs, CUDA 13.2+, ~23 GB VRAM (same ballpark
  as the A10), $0.32–0.35/hr.

**Decision (confirmed with the operator before booking):** use the L4 for the
two open questions only. Neither depends on A10-specific throughput —
they are about *mechanism and timing*, not tok/s. No capacity or throughput
number from this session is comparable to Phase 1's A10 baseline, and none is
claimed to be. `verify_box.sh`'s `FAIL card is 'NVIDIA L4' (L4, not A10)` is
therefore accurate and was consciously proceeded past, not a bug in the gate.

One incidental, non-authoritative data point: this L4's measured KV cache
(92,448 tokens, 12.7 GiB) came out **almost identical** to the A10's Phase 1
figure (92,544 tokens, 12.71 GiB) — expected, since KV budget is a function of
usable VRAM and model geometry, not compute architecture. Interesting, not
reused for any capacity claim.

---

## 1. Argon2-under-real-decode-load — CONFIRMED HOLDING

**Question:** the Phase 2 local measurement (`gateway/auth/__init__.py`) bounded
argon2 verification to `AUTH_VERIFY_CONCURRENCY=6` and showed event-loop lag
drop from 191ms to 19.7ms at N=40 concurrent verifies — but with **nothing
competing for CPU**. Phase 1's gateway never authenticated concurrently with
in-flight decoding either. Does the fix still hold when the CPU is also
servicing a real GPU-bound workload?

**Method:** 5 concurrent sustained streamed chat completions (400 max_tokens
each, real decode against the real engine) for 25s, with three bursts of 40
concurrent authenticated `GET /v1/models` calls fired mid-stream (pure argon2
load, no GPU cost). `bkn301_gateway_event_loop_lag_seconds` sampled every
200ms throughout.

**Result:**

| Metric | Value |
|---|---|
| Event loop lag, max | **2.42 ms** |
| Event loop lag, p50 | **0.224 ms** |
| Auth burst (n=40) latency, p50 | 750–780 ms |
| Auth burst (n=40) latency, max | 1,307–1,361 ms |
| Decode stream completions during the window | 15, **0 errors, 0 bad status** |

**The fix holds, and holds better than the no-GPU-competition baseline.** Max
loop lag (2.42ms) is well under the local measurement's already-bounded 19.7ms
figure — this Xeon-class box has enough CPU headroom that 5 GPU-bound decode
streams plus a 40-wide argon2 burst never stalled the event loop.

**The individual auth-request latency (750ms+ p50 under a 40-wide burst) is
not a defect — it is the bound working as designed.** `AUTH_VERIFY_CONCURRENCY
=6` means 40 concurrent verifies queue into ~7 sequential rounds; the
`p50 ≈ 780ms` is consistent with ~7 × ~110ms per hash round on this CPU. The
whole point of bounding the threadpool is that a burst backs up in the queue
(cheap, correct back-pressure) rather than ever touching the event loop — and
the zero decode errors during three such bursts confirm that trade held under
real GPU-bound traffic, not just synthetically.

---

## 2. Wedge-detection window — measured, and a real gap found

Two sub-experiments, one against **legitimate heavy load** (the
false-positive question) and one against a **real, faithful freeze** of the
engine process (the true-positive question). The second overturned an
assumption.

### 2a. Does legitimate heavy overload ever produce the wedge signature?

**Method:** 30 concurrent requests, each with a **unique** ~5–6k-token prompt
(a worker-id nonce placed at the very front, specifically to defeat prefix
caching — see the methodology note below) and `max_tokens=400`, sustained for
40s against the real engine. `vllm:num_requests_waiting`,
`vllm:num_requests_running`, `vllm:generation_tokens_total`,
`vllm:kv_cache_usage_perc` polled every 0.5s directly from vLLM's own
`/metrics`.

**A first attempt at this failed silently and is worth recording.** The first
run used an *identical* prompt across all 30 workers. `kv_cache_usage_perc`
never exceeded 7.6% and `num_requests_waiting` never left 0 — the run
accidentally re-demonstrated PRD §2.1 P1 (prefix caching collapsing
byte-identical prompts to nearly one worker's worth of real KV usage) instead
of stressing anything. Fixed by putting a per-worker nonce at the *front* of
the prompt (P1's own warning: "variable content early costs the whole
prefix").

**With genuinely unique prompts, the overload was real:**

| Metric | Value |
|---|---|
| `kv_cache_usage_perc`, max | **99.9%** |
| `num_requests_waiting`, max | **27** |
| `num_requests_running`, max | 26 |
| Samples with `waiting > 0` | 76 / 78 (essentially the whole window) |
| Requests completed | 30, **0 errors, 0 5xx** |
| Preemptions | 0 |
| **Longest continuous (`waiting>0` AND `generation_tokens_total` flat) streak** | **< 0.5s** (sub-poll-interval; no real plateau) |

**Finding:** even with the KV cache pinned at ~100% and the queue 20+ deep for
nearly the entire 40s window, the token counter never went flat for anything
resembling a multi-second stretch. This is real evidence — not conclusive for
every failure shape, but a genuine data point — that the **false-positive
risk of a much shorter window is low**, at least for admission-limited
queuing under legitimate heavy load. Zero preemptions is itself notable:
`kv_cache_usage_perc` at 99.9% did not trigger recompute in this run; the
scheduler held new admissions back at capacity instead.

### 2b. Does a real, faithful engine freeze produce the documented signature? — NO, and this is the significant finding

**Method:** `kill -STOP` on the vLLM **`EngineCore`** process only (pid 1147),
leaving the API server process (pid 537) alive — the most realistic
reproduction available of "the process is alive and the socket answers, but
the engine is not" (`PROBE_CONTRACT.md` §4's own framing). Confirmed via
`ps -p 1147 -o stat` reading `T` (stopped) throughout, and via
`vllm:generation_tokens_total` staying **exactly flat** for the whole freeze —
so the freeze genuinely halted generation, not merely appeared to. `/health`
and `/metrics` on the API server kept answering 200 the entire time, exactly
as the design doc predicts a wedge would look from outside.

A **new** chat completion was sent *after* the freeze was confirmed in effect,
specifically so there would be a genuinely pending, queued request during the
frozen window (the three requests in flight *before* the freeze finished
naturally before the signal landed — a tool-latency artifact of the
interactive session, not a vLLM behaviour, and not the basis for any finding
here).

**Result, sampled repeatedly over a ~201-second freeze:**

```
vllm:num_requests_running  = 0.0   (unchanged throughout)
vllm:num_requests_waiting  = 0.0   (unchanged throughout — including with a
                                     request genuinely pending against the
                                     frozen engine)
vllm:generation_tokens_total = flat (unchanged throughout)
bkn301_gateway_upstream_wedge_stall_seconds = 0.0 (never moved)
bkn301_gateway_upstream_wedge_suspected     = 0.0 (never moved)
bkn301_gateway_inflight_requests            = 2.0 (correctly showed the hang)
```

**`num_requests_waiting` never left zero, even with a request genuinely stuck
against a frozen engine for over three minutes.** The documented signature —
`waiting > 0 AND generation_tokens_total flat` — requires a precondition that
this freeze shape never satisfies. The new request appears to block *before*
it reaches whatever internal structure vLLM increments into `waiting`
(plausibly: admission itself requires a round-trip to the same EngineCore
process that is frozen, so the request never gets far enough to be counted at
all). Practically: **the dedicated metrics-based wedge detector
(`gateway/upstream/metrics_scraper.py`) would not detect this failure mode,
at any window length**, because its trigger condition never becomes true —
120s vs. 20s is moot if the count never leaves zero.

**This does not mean the gateway has no backstop for this failure mode — it
means the backstop is a different, slower one than the design intended.**
`gateway/upstream/breaker.py` weights `FailureKind.READ_TIMEOUT` at 3, tripping
at 2 (`WEIGHTS`, `breaker.py:74-76`), so a request that eventually times out
against `UPSTREAM_READ_TIMEOUT_SEC` (120s) *does* count against the breaker.
But that path needs **two** such timeouts to trip — ~240s+ with jitter, not
the ~120s the wedge-detector window was meant to provide as a **faster**,
complementary signal (`PROBE_CONTRACT.md` §4 explicitly frames the breaker as
"sees the stall first"). For this failure shape, that ordering is inverted:
the metrics-based detector never fires, and the breaker only closes the loop
much later, via the read-timeout path it was supposed to be faster than.

**Recovery was clean.** `kill -CONT` after ~201s: the process returned to
normal (`SLl`) state within 3 seconds, the pending request completed
correctly with real generated content, and `generation_tokens_total` advanced
by exactly the requested 50 tokens (48701 → 48751). No corruption, no
partial/garbled output, from a genuine 3+-minute freeze — well past the 120s
window this system was designed around.

### What this changes

- **The 120s window question is now answered for the "legitimate heavy load"
  side**: real overload does not produce a multi-second flat-counter plateau
  in this test, so a shorter window is not obviously unsafe on that axis.
- **The "does the detector actually fire on a real wedge" question has a
  different answer than assumed**: for the specific, realistic failure shape
  tested here (engine process frozen, API server alive), it does not fire at
  all, at any window. The estimate in `PROBE_CONTRACT.md` §4 was about
  *timing* a signal that turns out not to arrive for this shape.
- **Recommended follow-up, not built this session:** a complementary signal
  that does not depend on the frozen engine's own counters — e.g. alerting on
  `bkn301_gateway_inflight_requests` staying elevated with no matching
  completions for a shorter window, or a synthetic periodic health-check
  request (not `/health` or `/models`, which this freeze shape leaves
  answering) sent *through* the same code path a real chat request takes.
  This is a design decision, not a one-line fix, and is left for the next
  session rather than rushed in under a live rental.

---

## 3. Housekeeping

- `scripts/pack_for_box.sh` correctly refused to pack on its first invocation
  — its `.jsonl$` deny-list rule caught `results/phase1-vastai-a10/*.jsonl`,
  our own already-safe Phase 1 outputs, a genuine false positive against a
  rule designed for real app telemetry. Resolved by moving `results/` out of
  the working tree for the pack (git-tracked, so trivially restorable) rather
  than weakening the deny-list — the new box did not need old results anyway.
  Worth a follow-up: either scope the `.jsonl$` rule more precisely, or give
  the packer an explicit include/exclude list, so this stops requiring a
  manual workaround every session.
- `scripts/verify_box.sh --scan-only` caught our own throwaway API key
  embedded in the two experiment scripts' JSON output (via an argparse
  `--key` echoed into the saved config) before it reached this repo. Redacted
  before commit; the scan did exactly its job.
- Host-injected `/root/.vast_api_key` verified instance-scoped by hash
  (differed from the account key), consistent with every prior session.
