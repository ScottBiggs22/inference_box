# Phase 1 measurements — rented A10

**Status:** COMPLETE — measured 2026-09-10
**Box:** vast.ai instance `50504688`, machine `123534`, offer `47574078`, host `209278`, Illinois US
**Cost:** $0.2417/hr GPU + storage = $0.26/hr all-in. **Total spend $0.117** for the session.
**Operator:** scott.biggs@bkn301.com
**Procedure:** `docs/VAST_AI_RUNBOOK.md` rev 2
**Raw artefacts:** `results/phase1-vastai-a10/`

---

> ## Provenance warning — do not delete when quoting these numbers
>
> A vast.ai host has different CPU, PCIe topology and memory bandwidth from
> `VM.GPU.A10.1`. Every throughput figure below is a **provisional baseline to
> be re-confirmed on the OCI card** before anyone multiplies it (PRD §4.8).
> This host: Xeon Gold 6342, **PCIe gen4 ×16**, 251 GiB host RAM.
>
> DevOps sizes per-environment GPU counts off measured TPS, so a number from
> this page reaching a procurement spreadsheet unlabelled is the specific risk
> this banner exists to prevent. `loadgen.py` writes the same warning into its
> own output header so it survives a copy-paste.
>
> **The capacity figures are NOT provisional in the same way.** KV cache size is
> a property of the card, the model and vLLM's memory accounting, not of the
> host — so §3 transfers to any full 24 GiB A10.

---

## 1. Box verification — `scripts/verify_box.sh`

Run before any weights were pulled. Full output: `results/.../verify_box_output.txt`.

```
=== Rented box verification ===

GPUs visible: 1
  0, NVIDIA A10, 23028 MiB, 595.84

  PASS  exactly one GPU visible
  PASS  card is 'NVIDIA A10'
  PASS  VRAM 23028 MiB (full 24 GB card)
  PASS  card is idle (0 MiB used)
  driver: 595.84
  CUDA Version: 13.2
  PASS  disk free 59 GB

=== Data hygiene (PRD §4.8) ===
  WARN  host-injected credentials present (expected on vast.ai, instance-scoped):
        /root/.vast_api_key
  PASS  no real-data artefacts present
  PASS  no retrieval corpus present
  PASS  no credential-shaped material found

All hard requirements met.
```

| Field | Value | Notes |
|---|---|---|
| GPUs visible | 1 | |
| Card | **NVIDIA A10** | not A10G, not L4 |
| VRAM total | **23028 MiB** | full 24 GB card. Note: *addressable* is 22.06 GiB — see §3 |
| VRAM at idle | 0 MiB | nothing else on the card |
| Compute capability | **8.6** | SM86, confirms PRD §3.2 |
| Power limit | **150 W** | A10's full TDP — not capped below spec |
| PCIe | **gen4 ×16** | measured 19.1 GB/s by the listing |
| **Driver** | **595.84** | **DevOps Q17 minimum, verified** |
| **CUDA** | **13.2** | **DevOps Q17 minimum, verified** |
| Host CPU / RAM | Xeon Gold 6342, 96 threads, 251 GiB | host totals, not our slice |
| Disk free | 59 GB | of 60 requested |

**Verdict: full-card A10. PRD §3.3 applies.**

### A finding from the gate itself

vast.ai **injects `/root/.vast_api_key` into every instance.** Verified by hash
comparison that it is *not* the account key (65 bytes vs 64, different SHA-256)
— it is instance-scoped and dies with the contract. `verify_box.sh` now reports
it as a host-injected credential rather than failing on it, because failing
would make the gate un-passable on vast.ai.

---

## 2. Server configuration as actually run

| Item | Value |
|---|---|
| Image | `vllm/vllm-openai:v0.28.0` (CUDA 13.0 build) |
| vLLM | 0.28.0 |
| torch | 2.13.0+cu130 |
| Model | `Qwen/Qwen3-8B-AWQ` |
| `--revision` | `4da05a8edb55c6046cce958586c33b61da07bb79` |
| `--max-model-len` | 8192 |
| `--gpu-memory-utilization` | 0.90 |
| Quantisation as detected | `auto_awq` |
| dtype | `torch.float16` |

### Flag reality — three corrections

Evidence: `results/.../vllm_flag_reality.txt`.

| Flag | Documented in PRD/runbook | Reality in v0.28.0 |
|---|---|---|
| `--disable-log-requests` | prescribed in PRD §5.2, §5.4(2), §7 Phase 1 and the runbook | **DOES NOT EXIST.** `vllm serve` would have failed to start |
| — | — | Replaced by **`--enable-log-requests` / `--no-enable-log-requests`**, polarity **inverted**: request logging is now **off by default**, opt-in. Also `--enable-log-outputs` for completion text, likewise off |
| `--gpu-memory-utilization` | PRD §3.3 assumes 0.90 | **default is now 0.92.** We passed 0.90 explicitly, so §3.3's basis held |
| `--enable-prefix-caching` | PRD §3.6 relies on "on by default" | **CONFIRMED.** Engine reports `enable_prefix_caching=True` without the flag. Also `enable_chunked_prefill=True` |

**Net effect on PRD §5.4 item 2 (no-retention by default):** the requirement is
now satisfied *by default* rather than by a flag, which is a better posture —
but the flag the PRD names is a startup failure. The launch command must use
`--no-enable-log-requests --no-enable-log-outputs`, which states the intent
explicitly and survives a future default change.

---

## 3. KV cache — the headline measurement

> Every capacity claim in PRD §3.3 was calculated and never measured. This is
> the measurement.

### Result

| Quantity | PRD §3.3 (calculated) | **Measured (cold)** | **Measured (warm)** |
|---|---|---|---|
| Addressable VRAM | 24 GB assumed | **22.06 GiB** | 22.06 GiB |
| Usable at 0.90 util | 21.6 GiB | **19.85 GiB** | 19.85 GiB |
| Weights + non-torch | 5.6 GiB | 5.96 GiB | 5.96 GiB |
| Peak activation | 1.2 GiB *(incl. graphs)* | **1.18 GiB** | **0.18 GiB** |
| CUDA graphs | *not budgeted* | **0.62 GiB** | 0.62 GiB |
| **KV cache** | **14.8 GiB** | **12.71 GiB** | **13.71 GiB** |
| **Total KV tokens** | **~105,000** | **92,544** | **99,856** |
| Max concurrency @ 8192 | — | **11.30×** | 12.19× |
| `nvidia-smi` after load | — | 20541 MiB | 21621 MiB |

**PRD §3.3 was 12% optimistic** (92,544 vs ~105,000 on the conservative boot).

### Why, precisely

Two errors, and the larger one is arithmetic:

1. **`0.90 × 24 GB` is the wrong basis.** The card *addresses* 22.06 GiB, not
   24 — the rest is driver and ECC overhead. So 0.90 yields **19.85 GiB, not
   21.6 GiB**. That is 1.75 GiB of the 2.1 GiB gap.
2. **CUDA graphs were not budgeted separately.** §3.3 lumped "Activations /
   CUDA graphs = 1.2 GiB"; actual is 1.18 activation **plus** 0.62 graphs.

### The per-token arithmetic was exactly right

```
92,544 tokens × 147,456 B = 13.646 GB = 12.71 GiB   ← matches the reported figure
```

So PRD §3.1's geometry — 36 layers × 8 KV heads × 128 head_dim × 2 × 2 bytes =
**144 KiB/token** — is confirmed to the digit. Only the available-memory
assumption was wrong. That distinction matters: the model is sound, one input
was not.

### The conclusion §3.3 was drawn to support survives

| Question | PRD claim | Measured (conservative) |
|---|---|---|
| 5 × 8k = 40,960 tokens? | ✓ 2.6× headroom | **✓ 2.26× headroom** |
| 5 × 4k = 20,480 tokens? | ✓ | **✓ 4.5× headroom** |
| Concurrent 8k sequences | — | **11.3** |

**R4 is comfortably met.** The claim is refuted; the decision it justified is not.

### KV cache size is not constant across boots — and it is not noise

Three boots of the identical command:

| Boot | `torch.compile` | Peak activation | KV cache | Tokens | Boot time |
|---|---|---|---|---|---|
| 1 (cold compile cache) | 38.38 s | **1.18 GiB** | 12.71 GiB | **92,544** | 153 s |
| 2 (warm) | 0.36 s | 0.18 GiB | 13.71 GiB | 99,856 | 45 s |
| 3 (warm) | 0.36 s | 0.18 GiB | 13.71 GiB | 99,856 | 45 s |

Compiling *during the memory-profiling pass* inflates measured peak activation
by 1.0 GiB, and vLLM permanently deducts that from the KV cache for the life of
the process.

**Consequence for the deployment contract:** baking
`~/.cache/vllm/torch_compile_cache` into the image buys **~7,300 tokens of KV
cache (+7.9%) and ~108 s of startup**. Without it, a fresh pod on a new node
gets the *smaller* cache. **Capacity planning must use 92,544**, unless the
compile cache is baked — in which case 99,856.

---

## 4. Cold start — sets the startup probe

| Scenario | Measured | Composition |
|---|---|---|
| **First boot** (no weight cache, no compile cache) | **153 s** | ~6 GB weight download + 38.38 s `torch.compile` + 52.93 s engine init |
| **Warm restart** (weights + compile cache present) | **45 s** | 0.36 s compile, 11.07 s engine init, 7 s graph capture |

PRD §4.5 item 6 estimated 30–60 s. That is right for the warm case and
**wrong by 2.5× for a genuinely cold pod.**

### The current placeholder is wrong in the dangerous direction

`deploy/k8s/probes.yaml` ships `periodSeconds: 5, failureThreshold: 24` → a
**120 s** budget. A cold start took **153 s**. That is not a slow start, it is a
**boot loop**: Kubernetes kills the pod at 120 s, and the replacement starts
cold too.

| Setting | Placeholder | **Measured recommendation** |
|---|---|---|
| `startupProbe.periodSeconds` | 5 | 5 |
| `startupProbe.failureThreshold` | 24 | **60** |
| Effective budget | 120 s | **300 s** |

300 s is ~2× the measured cold start. Generous on purpose: the cost of a loose
startup probe is a slower failure verdict, and the cost of a tight one is a
restart loop that never converges. If the image bakes both weights and the
compile cache, 24 (120 s) is adequate — but the manifest should not assume it.

---

## 5. Throughput — `scripts/loadgen.py`, run **on the box**

Context shape **7000 prompt + 512 output** — the 8k shape R4 and the exit gate
are about. Run on the box, so **no tunnel is in these latencies**.

Definitions (the app harness's `decode_tok_s` is a different, non-comparable
quantity — it divides completion tokens by whole-request latency):

- **TTFT** — request sent → first content delta.
- **decode tok/s** — `(completion_tokens − 1) / (last delta − first delta)`.
- **per-user** — median of per-request decode rates. This is R1's subject.
- **aggregate** — total completion tokens / wall time.

### Through the gateway

| Concurrent | per-user tok/s (p50) | min | aggregate tok/s | TTFT p50 (ms) | TTFT p95 (ms) | failures |
|---|---|---|---|---|---|---|
| 1 | 71.69 | 71.48 | 69.72 | 191.5 | 204.8 | 0 |
| 3 | 60.85 | 60.56 | 171.7 | 255.3 | 451.7 | 0 |
| **5** | **50.89** | 50.46 | 242.1 | 225.3 | 561.6 | 0 |
| 8 | 48.11 | 47.57 | 360.5 | 253.2 | 870.7 | 0 |

### Direct to vLLM — for gateway overhead

| Concurrent | per-user tok/s (p50) | aggregate tok/s | TTFT p50 (ms) | TTFT p95 (ms) | failures |
|---|---|---|---|---|---|
| 1 | 71.45 | 70.65 | 92.1 | 106.8 | 0 |
| 3 | 60.66 | 176.7 | 124.6 | 177.7 | 0 |
| **5** | **50.87** | 248.3 | 131.6 | 230.8 | 0 |
| 8 | 48.29 | 371.5 | 167.0 | 307.3 | 0 |

### Gateway overhead, isolated

**Decode throughput: none.** Within ±0.4% at every level — the unbuffered SSE
passthrough design does what it claims.

**TTFT: +85 to +130 ms**, roughly doubling it at low concurrency. Isolated
directly:

| Endpoint | Auth | Mean latency |
|---|---|---|
| `/healthz` | none | **1.2 ms** |
| `/v1/models` | API key | **77.3 ms** |

So **argon2id verification costs ~76 ms per request** and accounts for
essentially all of it. `gateway/auth/keys.py` argues the cost is acceptable
because it "runs once per request in front of a multi-second GPU operation" —
confirmed, and now quantified: 0.8% of a 10 s generation, but ~2.5% of a 3 s
FAQ answer.

**Two consequences.** First, this is a measured argument for PRD §5.3's
preference for the **JWT path** service-to-service: RS256 verification is ~1–2
ms, roughly 40× cheaper. Second, a short-lived verified-key cache would remove
it for the API-key path — at the cost of delaying revocation by the cache TTL,
which is a trade to decide deliberately rather than by default.

### Comparison to the PRD's projections

| Claim | PRD | Measured | Verdict |
|---|---|---|---|
| tok/s/user @ 5 concurrent | **~48** | **50.89** | **CONFIRMED** (within 6%, on the better side) |
| R1 floor (≥20 tok/s/user) | met, ~2.4× headroom | **2.54× headroom** | **CONFIRMED** |
| Decode ceiling (bandwidth-bound) | ~107 tok/s theoretical | 71.7 tok/s @ batch 1 | 67% of theoretical — a normal achieved ratio |
| Aggregate scaling | — | 69.7 → 360.5 tok/s (1→8) | 5.2× for 8× concurrency |

**Exit gate (PRD §7 Phase 1): ≥20 tok/s/user at 5 concurrent with 8k context —
MET at 50.89 tok/s/user.** `loadgen.py` prints this verdict itself.

Notable: the throughput model was accurate while the memory model was not.

### Prefix caching — and the LMCache decision

| Metric | Value |
|---|---|
| `prefix_cache_hits_total` / `queries_total` | 730,928 / 742,322 = **98.5%** |
| `prompt_tokens_by_source{local_cache_hit}` | 730,928 |
| `prompt_tokens_by_source{local_compute}` | 11,394 (**1.5%**) |

The default loadgen shape models real traffic — a large byte-identical prefix
(system prompt + knowledge block) plus a short unique query. `--unique-prefix`
gives the floor:

| | warm prefix (98.5% hit) | cold prefix (**0%** hit) |
|---|---|---|
| tok/s/user @ 1 | 71.5 | 72.0 — *decode unaffected* |
| **tok/s/user @ 5** | **50.9** | **28.9** — 43% lower |
| TTFT p50 @ 1 | 92 ms | **1,405 ms** — 15× |
| TTFT p50 @ 5 | 132 ms | **3,788 ms** |
| TTFT p95 @ 5 | 231 ms | **6,450 ms** |

Prefilling 7,000 uncached tokens costs ~1.4 s, and at 5 concurrent that prefill
competes with decode for the GPU.

**R1's ≥20 tok/s/user floor is met in both cases** (50.9 and 28.9). That is the
robustness result: the target holds even with prefix caching contributing
nothing.

**LMCache decision (PRD §3.6):** stays deferred, now for a measured reason.
§3.6's condition was "adopt only if TTFT is demonstrably prefill-bound **and**
prefix caching is missing". TTFT *is* prefill-bound on a cache miss — but
prefix caching is emphatically not missing: it absorbs 98.5% of prompt tokens on
FirstData's traffic shape. LMCache would only help the residual 1.5%.

---

## 6. B3 on real Qwen3 — `scripts/b3_probe.py`

The open question from PRD §6.1 B3: **does a user typing `/think` override the
request-level `enable_thinking: false`?**

| Case | `enable_thinking` | `<think>` | unterminated | empty after strip | finish_reason |
|---|---|---|---|---|---|
| `baseline_no_flag` | *(absent)* | **yes** | no | no | stop |
| `flag_false` | `false` | no | no | no | stop |
| **`flag_false_user_types_think`** | `false` | **no** | no | no | stop |
| `flag_false_user_types_no_think` | `false` | no | no | no | stop |
| `flag_true_user_types_no_think` | `true` | **yes** | no | no | stop |
| `flag_absent_tight_budget` (160 tok) | *(absent)* | **yes** | **yes** | **YES** | **length** |
| `flag_false_tight_budget` (160 tok) | `false` | no | no | no | stop |

### Verdicts

| Question | Answer |
|---|---|
| Does Qwen3-8B-AWQ default to emitting `<think>`? | **Yes** — B3's premise confirmed |
| Does `enable_thinking: false` suppress it? | **Yes** — B3's fix works |
| **Does `/think` override the flag?** | **NO. The request flag wins.** |
| Does `/no_think` override `enable_thinking: true`? | **No** — the flag wins in *both* directions |
| Does B3's stated failure reproduce at `max_tokens: 160`? | **Yes, exactly** — unterminated block, `finish_reason: length`, empty after stripping |

`chat_template_kwargs` is authoritative and the Qwen3 soft switch is
subordinate to it in both directions. **So the risk B3 flagged does not
exist:** a user typing `/think` cannot re-enable reasoning against our
configuration, and no input sanitisation is needed at the app boundary.
`strip_think` is a belt rather than the primary defence — which is what PRD
§6.1 B3 hoped but could not confirm without a GPU.

The 160-token case is worth dwelling on because it reproduces the *whole*
failure chain the PRD predicted: thinking on + a tight allowance → block never
closes → `finish_reason: length` → nothing survives stripping → the app's
facade maps empty to `("", True)` → a silently degraded RAG-only answer.

**Untested cell, stated for honesty:** flag *absent* plus `/no_think` was not
run, so whether the soft switch works when no flag is sent is unknown. It does
not matter operationally — the app always sends the flag — but the matrix is not
exhaustive.

### Gateway vs direct — passthrough integrity

All seven cases **identical** between the gateway and direct-to-vLLM runs.
**The gateway's SSE/JSON passthrough preserves `chat_template_kwargs`.** This
was the failure mode worth checking: a gateway that forwarded only known OpenAI
fields would have silently dropped the flag and broken B3's fix in production
while every local test against the stub stayed green.

---

## 7. Audit log — metadata-only guarantee, verified against real traffic

78 real requests through the gateway to a real model. The audit log's complete
key set:

```
auth_method, completion_tokens, event, keyid, latency_ms, model,
prompt_tokens, reason, status, streamed, subject, timestamp, upstream
```

Searched for every distinctive word in the prompts and completions
(`Summarise`, `treasury`, `position`, `portfolio`, `asset`, `investor`,
`think`): **zero occurrences.** Sample record:

```json
{"auth_method":"apikey","completion_tokens":32,"event":"chat.completions",
 "keyid":"223811148d7a","latency_ms":419.57,"model":"Qwen/Qwen3-8B-AWQ",
 "prompt_tokens":22,"reason":null,"status":200,"streamed":false,
 "subject":"key:223811148d7a","timestamp":"2026-09-10T16:21:58Z",
 "upstream":"http://127.0.0.1:8000/v1"}
```

A much stronger validation than `tests/test_audit_no_prompt_text.py`, which
asserts the same property against a stub.

---

## 8. Corrections this session produced

| # | Claim | Where | What was measured |
|---|---|---|---|
| M1 | KV budget 14.8 GiB → **~105,000 tokens** | PRD §3.3 | **12.71 GiB → 92,544 tokens.** 12% optimistic. Cause: `0.90 × 24 GB` should be `0.90 × 22.06 GiB`, plus CUDA graphs unbudgeted. R4 still met at 2.26× headroom |
| M2 | Usable memory at 0.90 util = 21.6 GiB | PRD §3.3 | **19.85 GiB** — the card addresses 22.06 GiB, not 24 |
| M3 | `--disable-log-requests` | PRD §5.2, §5.4(2), §7; runbook §4 | **Flag does not exist in vLLM 0.28.0.** Replaced by `--no-enable-log-requests`, polarity inverted; logging now off by default |
| M4 | Cold start 30–60 s | PRD §4.5(6) | **45 s warm, 153 s cold.** The 120 s `startupProbe` budget would boot-loop a cold pod → `failureThreshold: 60` |
| M5 | `--gpu-memory-utilization` implied default | PRD §3.3 | Default is now **0.92**, not 0.90 |
| M6 | tok/s/user @ 5 concurrent ≈ 48 | PRD §3.3 | **50.89 — CONFIRMED**, within 6% |
| M7 | 144 KiB KV per token | PRD §3.1 | **CONFIRMED exactly** (92,544 × 144 KiB = 12.71 GiB) |
| M8 | Prefix caching on by default | PRD §3.6 | **CONFIRMED** (`enable_prefix_caching=True`), and 98.5% hit rate on realistic traffic |
| M9 | `/think` may override `enable_thinking: false` | PRD §6.1 B3 | **It does not.** The request flag wins in both directions |
| M10 | KV cache size is a constant | implied throughout | **It is not.** A cold `torch.compile` inflates profiled peak activation by 1.0 GiB and costs 7,312 tokens of KV permanently |

---

## 9. Handed to DevOps

- [x] **Driver + CUDA minimum (Q17): driver ≥ 595.84, CUDA 13.2 verified.** Note
      the coupling: `vllm/vllm-openai:v0.28.0` is a **CUDA 13.0** build, so a
      CUDA 12.x-only driver cannot run it. The other candidate box on this
      provider (driver 570.144, `cuda_max_good` 12.8) would have failed at boot.
- [x] **`startupProbe.failureThreshold: 60`** (300 s at `periodSeconds: 5`),
      measured. Updates `deploy/k8s/probes.yaml`.
- [x] **Bake `~/.cache/vllm/torch_compile_cache` into the image.** Worth ~7,300
      tokens of KV cache and ~108 s of startup. This is a real capacity gain, not
      just a speed-up.
- [x] **Launch flags corrected:** `--no-enable-log-requests
      --no-enable-log-outputs`. The previously specified
      `--disable-log-requests` is a startup failure on 0.28.0.
- [x] **TPS-per-card, with the §4.8 caveat attached:** ~51 tok/s/user at 5
      concurrent / ~242 tok/s aggregate at 8k context with a warm prefix cache;
      ~29 tok/s/user with a cold one. Both clear R1's 20 tok/s floor.
- [ ] **Wedge-detection window** for the sidecar backstop
      (`PROBE_CONTRACT.md` §4) — **not measured.** Requires inducing a wedge,
      which did not happen naturally. Still an estimate; carry to Phase 2 with
      the gateway breaker.
- [ ] Restate that the OCI card is the performance reference and Azure staging
      is application-integration only (PRD §8 Q3).
- [ ] **Still outstanding and still blocking: PRD §8 Q1 — is Azure staging a
      full-card A10?** `NVadsA10_v5` starts at one-sixth of a card with a 4 GiB
      frame buffer. Note this measurement makes the question sharper: the real
      KV budget on a *full* card is 12.71 GiB, so a half card would have ~4 GiB
      and roughly **28,000 tokens** — 5 × 8k (40,960) becomes **unreachable**,
      exactly as §4.4(a) warned, but now with a measured figure behind it.

---

## 10. What was not done, and why

- **Eval-set quality vs BF16.** Requires the app, which requires the real
  knowledge corpus, which must not reach a rented host (§0 of the runbook).
  Deferred to the OCI card as planned.
- **Wedge-detection window.** See §9.
- **Phase 2 security review.** Runs against the real deployment path (PRD §4.8).
- **BF16 (config A) comparison.** Not needed: AWQ cleared the exit gate at
  2.5× the R1 floor, so the fallback was never triggered.
