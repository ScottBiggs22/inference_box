# Phase 1 measurements — rented A10

**Status:** TEMPLATE — no measurements taken yet
**Box:** _(vast.ai instance id, host, hourly rate)_
**Date:** _(session date)_
**Operator:** scott.biggs@bkn301.com
**Procedure:** `docs/VAST_AI_RUNBOOK.md` rev 2

---

> ## Provenance warning — do not delete when quoting these numbers
>
> A vast.ai host has different CPU, PCIe topology and memory bandwidth from
> `VM.GPU.A10.1`. Every throughput figure below is a **provisional baseline to
> be re-confirmed on the OCI card** before anyone multiplies it (PRD §4.8).
>
> DevOps sizes per-environment GPU counts off measured TPS, so a number from
> this page reaching a procurement spreadsheet unlabelled is the specific risk
> this banner exists to prevent. `loadgen.py` writes the same warning into its
> own output header so it survives a copy-paste.

---

## 1. Box verification — `scripts/verify_box.sh`

Run **before** pulling weights. If this failed, the box was destroyed and
nothing below was measured.

```
_(paste the complete verify_box.sh output here, verbatim)_
```

| Field | Value | Notes |
|---|---|---|
| GPUs visible | | must be exactly 1 |
| Card | | A10, not A10G (different bandwidth/clocks) and not L4 |
| VRAM total | | ~23028 MiB = full 24 GB card |
| VRAM used at idle | | vLLM pre-allocates; another tenant fails it at boot |
| **Driver version** | | **hand to DevOps — Q17 minimum** |
| **CUDA version** | | **hand to DevOps — Q17 minimum** |
| Disk free | | ≥ 30 GB |
| Hygiene scan | | expect clean |

**Verdict:** _(A10 full card / A10G / partitioned — and whether §3.3 applies)_

---

## 2. Server configuration as actually run

| Item | Value |
|---|---|
| Image tag | _(pinned — never `latest`)_ |
| Model | `Qwen/Qwen3-8B-AWQ` |
| `--revision` | `4da05a8edb55c6046cce958586c33b61da07bb79` |
| `--max-model-len` | 8192 |
| `--gpu-memory-utilization` | 0.90 |
| vLLM version reported at boot | |

Flag reality for this image tag — recorded rather than assumed, because the
spelling and defaults have moved across releases:

```
_(paste: vllm serve --help 2>&1 | grep -iE 'prefix-caching|log-requests|max-model-len')_
```

| Flag | Exists? | Default | Notes |
|---|---|---|---|
| prefix caching | | | V1 has it on by default; PRD §3.6 relies on that |
| `--disable-log-requests` | | | required by PRD §5.4 item 2 — no prompt retention |

---

## 3. KV cache — the headline measurement

> **This is the single highest-value number the box exists for.** Every
> capacity claim in PRD §3.3 is calculated, not measured, and this line either
> confirms or refutes the ~105,000-token AWQ budget the whole plan rests on.

```
_(paste: grep -iE 'kv cache|gpu blocks|graph captur|memory profil' vllm-boot.log)_
```

| Quantity | Calculated (PRD §3.3) | **Measured** | Delta |
|---|---|---|---|
| KV budget | 14.8 GiB | | |
| KV per token | 144 KiB | — | re-derived below |
| **Total KV tokens** | **~105,000** | | |
| GPU blocks reported | — | | × block size (16) = tokens |
| Weights resident | 5.6 GiB | | from `nvidia-smi` after load |
| Total GPU memory used | ~21.6 GiB @ 0.90 util | | |

### The arithmetic being checked

Re-derived from the real `config.json` of `Qwen/Qwen3-8B-AWQ` on 2026-09-09,
and it confirms PRD §3.1 exactly:

```
36 layers × 8 KV heads × 128 head_dim × 2 (K,V) × 2 bytes = 147,456 B
                                                          = 144 KiB / token
one 8k sequence  = 1.125 GiB
14.8 GiB budget  = 107,770 tokens   (PRD says "~105,000" — consistent)
```

So the *per-token* cost is not in doubt. What is unknown, and what the measured
line settles, is **how much of the 21.6 GiB vLLM actually leaves for KV** once
weights, activations and CUDA graphs are resident.

**Derived capacity:**

| Question | Answer |
|---|---|
| 5 × 8k = 40k tokens? | _(PRD claims ✓ with 2.6× headroom)_ |
| 5 × 4k = 20k tokens? | _(PRD claims ✓)_ |
| Concurrent 8k sequences at measured capacity | |

**Verdict on §3.3:** _(confirmed / refuted / confirmed with a different margin)_

---

## 4. Cold start — sets the startup probe

| Measurement | Value |
|---|---|
| Process start → `/health` 200 | _(seconds)_ |
| Of which: weight download | _(exclude from the probe budget — a warm HF cache or a baked image removes it)_ |
| Of which: load + CUDA graph capture | _(this is what the probe must tolerate)_ |

PRD §4.5 item 6 estimates 30–60s. `deploy/k8s/probes.yaml` currently ships
`periodSeconds: 5, failureThreshold: 24` → a 120s budget, **marked as a
placeholder**.

| Setting | Placeholder | **Measured recommendation** |
|---|---|---|
| `startupProbe.periodSeconds` | 5 | |
| `startupProbe.failureThreshold` | 24 | |
| Effective budget | 120s | |

Pick the threshold with real headroom over the measured time, not a tight fit:
a cold pull on a cold node is the slow case, and the cost of being generous is
only a slower failure verdict, while the cost of being tight is a boot loop.

---

## 5. Throughput — `scripts/loadgen.py`, run on the box

Context shape: **7000 prompt + 512 output**, i.e. the 8k shape the R4 goal and
the Phase 1 exit gate are actually about. Five short prompts would test a card
with an almost-empty KV cache and would not be the same measurement.

Definitions, because the app repo's harness used a different and
non-comparable one (it divided completion tokens by whole-request latency,
conflating prefill with decode):

- **TTFT** — request sent → first content delta. Prefill plus queueing.
- **decode tok/s** — `(completion_tokens − 1) / (last delta − first delta)`.
  The −1 excludes the token produced by prefill.
- **per-user** — the median of per-request decode rates at that level. This is
  what R1 is about and what DevOps multiplies.
- **aggregate** — total completion tokens / wall time. Rises with concurrency
  as continuous batching works; per-user falls. Different questions.

### Through the gateway

| Concurrent | per-user tok/s (p50) | per-user min | aggregate tok/s | TTFT p50 (ms) | TTFT p95 (ms) | failures |
|---|---|---|---|---|---|---|
| 1 | | | | | | |
| 3 | | | | | | |
| 5 | | | | | | |
| 8 | | | | | | |

### Direct to vLLM (`--direct`) — for gateway overhead

| Concurrent | per-user tok/s (p50) | aggregate tok/s | TTFT p50 (ms) | failures |
|---|---|---|---|---|
| 1 | | | | |
| 3 | | | | |
| 5 | | | | |
| 8 | | | | |

**Gateway overhead:** _(TTFT delta and tok/s delta; the gateway is IO-bound and
passes SSE through unbuffered, so this should be small — if it is not, that is a
finding)_

### Comparison to the PRD's projections

| Claim | PRD §3.3 / §2 | Measured | Verdict |
|---|---|---|---|
| tok/s per user @ 5 concurrent | ~48 | | |
| R1 floor (≥20 tok/s/user) | met with ~2.4× headroom | | |
| Decode ceiling (bandwidth-bound) | ~107 tok/s theoretical | | |

**Exit gate (PRD §7 Phase 1): ≥20 tok/s/user at 5 concurrent with 8k context.**
_(MET / NOT MET — `loadgen.py` prints this verdict itself)_

If NOT MET, the PRD's stated fallback is config A (BF16) at 4×8k or 5×6k, which
still meets R1 at ~25 tok/s/user.

### Prefix caching

Default run models real traffic: a byte-identical large prefix plus a short
unique query. `--unique-prefix` defeats the cache for a worst case.

| Run | TTFT p50 | Cache hit rate (vLLM `/metrics`) |
|---|---|---|
| Default (shared prefix) | | |
| `--unique-prefix` | | |

This is the PRD §7 Phase 2 / §3.6 input on whether LMCache is worth revisiting.

---

## 6. B3 on real Qwen3 — `scripts/b3_probe.py`

The open question from PRD §6.1 B3: **does a user typing `/think` override the
request-level `enable_thinking: false`?**

| Case | `enable_thinking` | `<think>` present | unterminated | empty after strip | finish_reason |
|---|---|---|---|---|---|
| `baseline_no_flag` | *(absent)* | | | | |
| `flag_false` | `false` | | | | |
| **`flag_false_user_types_think`** | `false` | | | | |
| `flag_false_user_types_no_think` | `false` | | | | |
| `flag_true_user_types_no_think` | `true` | | | | |
| `flag_absent_tight_budget` (160 tok) | *(absent)* | | | | |
| `flag_false_tight_budget` (160 tok) | `false` | | | | |

**Verdicts:**

| Question | Answer |
|---|---|
| Does Qwen3-8B-AWQ default to emitting `<think>`? | _(B3's premise)_ |
| Does `enable_thinking: false` suppress it? | _(B3's fix)_ |
| **Does `/think` override the flag?** | |
| Does `/no_think` suppress it when the flag is `true`? | |
| Does B3's stated failure reproduce at `max_tokens: 160`? | |

**Gateway vs direct:** _(identical / differ)_ — if they differ, the gateway is
dropping `chat_template_kwargs` in passthrough, and B3's fix is broken in
production while every local test stays green.

**If `/think` wins:** the `strip_think` stripper is load-bearing rather than a
belt, and the app should strip the soft switch from user input at the boundary
— otherwise any user with the `/think` habit silently degrades their own
answers to RAG-only via the empty-completion path.

---

## 7. Corrections this session produced

_(Record anything measured here that contradicts the PRD, in the same style as
PRD §6.2's in-place corrections. Building on a refuted claim produces no-op work
and a false "done", which is why this section is part of the template rather
than an afterthought.)_

| # | Claim | Where | What was measured |
|---|---|---|---|
| | | | |

---

## 8. Handed to DevOps

- [ ] Driver + CUDA minimum (Q17)
- [ ] `startupProbe.failureThreshold`, measured — updates `deploy/k8s/probes.yaml`
- [ ] Wedge-detection window for the sidecar backstop (`PROBE_CONTRACT.md` §4)
- [ ] TPS-per-card figure, **with the §4.8 provisional-baseline caveat attached**
- [ ] Restated: the OCI card is the performance reference, Azure staging is
      application-integration only (PRD §8 Q3)
- [ ] Still outstanding and still blocking: **PRD §8 Q1 — is Azure staging a
      full-card A10?** `NVadsA10_v5` starts at one-sixth of a card with a 4 GiB
      frame buffer, and a fractional SKU boots, serves, and silently breaks the
      8k context requirement.
