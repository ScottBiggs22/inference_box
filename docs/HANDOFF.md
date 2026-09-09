# Handoff — state of play as of 2026-09-09

Written to carry context into a new session working inside this repo. The PRD
(`docs/INFERENCE_SERVICE_PRD.md`) remains the design source of truth; this
document is *state*, not design.

---

## 1. Where things live

Two repos, **siblings** — the inference repo is not inside the app repo:

```
Desktop/BKN Code 2/
├── baas-poc-templates/          the FirstData app. Consumes the gateway.
│   └── services/bkn301-ai-service/
└── bkn301-inference/            THIS REPO. Gateway + vLLM deployment.
```

`bkn301-inference` has **no remote**. Two local commits. To publish:

```bash
gh repo create bkn301-inference --private --source=. --push
```

In `baas-poc-templates`, the working remote is **`raghav`**
(`github.com/raghavsingh15/fd-llm-chatbot`), not `origin`. Its branch is
`cloud_infra_prep`, and **the Phase 0 changes there are uncommitted on purpose** —
`CLAUDE.md` now forbids push/merge without approval, and the commit was never
asked for.

---

## 2. Phase 0 — complete

| Item | State |
|---|---|
| **S1** Anthropic key | Closed. There was no key in `.env` to rotate — it is 2 bytes, and nothing read it. Rotated at the console independently. `.env.example` added; compose now reads `env_file`. |
| **S5** `.dockerignore` | Done, allowlist form. Build context **5.4 GB → 1.32 MB**. `.env`, `logs/`, `models/`, both venvs verified absent from the built image. |
| **S10** compose publish | Done — `127.0.0.1:8000:8000`. |
| Eval set | `chat_cases.json` **12 → 36 cases, exactly 25% Arabic**, calibrated over three real runs. |
| Synthetic variant | `scripts/pseudonymise.py` + 17 tests incl. a leak gate over the real 1188-record corpus. |
| Bench harness | `--backend vllm` works end to end against the stub. |
| **B3** thinking mode | Done, moved forward from Phase 3 — see §4. |
| **B4** tokenizer | Done, and the finding was not what the PRD expected — see §4. |
| Gateway first slice | This repo. 58 tests, ruff clean, verified live against the stub. |
| vast.ai runbook | `docs/VAST_AI_RUNBOOK.md` + `scripts/verify_box.sh`. |

**Test/build state:** 179 ai-service tests pass, 58 gateway tests pass, `ruff`
clean, `dotnet build` 0 errors, `npm run build` ok, `tsc --noEmit` ok.

---

## 3. Three PRD claims that were wrong, and now say so

Each was verified firsthand and the PRD is corrected in place. Flagged here
because a new session reading only the original text would rebuild on them.

**S10's diagnosis was inverted.** `Dockerfile:26` launches `main:app` as an ASGI
target with `--host 0.0.0.0` hardcoded, so `main.py`'s `__main__` block never
runs and `settings.HOST` is dead code in the container. Compose was never
"silently broken" — it was *exposing* an unauthenticated service on all host
interfaces. Narrowing the publish was the whole fix; `HOST: 0.0.0.0` in
`environment:` is a no-op kept as a belt.

**S1 had nothing in it.** No key in `.env`, no Anthropic SDK, no `load_dotenv`,
never committed. The `sk-ant-` hits in `ConfigController.cs` history are
`body.ApiKey.StartsWith("sk-ant-")`.

**B4 overstated its impact by a wide margin.** See §4.

---

## 4. B3 and B4, as actually resolved

**B3 moved from Phase 3 into Phase 0.** Phase 1 benchmarks Qwen3-8B-AWQ; without
`enable_thinking: false` every answer returns empty, `generate()` maps to
`("", True)`, and the entire benchmark reads as uniformly degraded with no error.
Implemented in `baas-poc-templates/.../llm_backends/openai_http.py`: the request
flag *plus* a `strip_think` stripper, because the flag is a hint the server may
ignore and a user typing `/think` is a Qwen3 soft switch. The stripper handles
the unterminated block that `max_tokens` truncation produces — that is the shape
that empties an answer.

**B4's premise was false; its fix still landed.** The PRD said Qwen3's vocab
"differs" such that "wrong counts silently mis-size every chunk". Measured:

- `vocab_size` is **151643 for both**.
- Token IDs were **byte-identical across all 349 distinct strings** in
  `chat_interactions.log` plus all five extraction fixtures. Zero count
  differences, on English, Arabic, hex-dense and money-dense text.
- Chunk budgets were therefore **never** mis-sized.

The genuine difference is four added control tokens:

| Token | Qwen2.5 | Qwen3 |
|---|---|---|
| `<think>` / `</think>` | 3 tokens | **1** |
| `<tool_response>` / `</tool_response>` | 4 tokens | **1** |

Narrow, but it lands exactly on the B3 surface, so anything budgeting or stopping
around a reasoning block needs the Qwen3 tokenizer. Both are now vendored and
**selected from `LLM_BACKEND`** — llamacpp/qvac → Qwen2.5, vllm → Qwen3 — because
the right tokenizer belongs to the model served, not to the service. Pinned by
`tests/test_tokenizer.py::TestQwen3Divergence` and `::TestBackendSelection`.

---

## 5. Four app defects the eval calibration exposed

None was on any list before. All are in `baas-poc-templates`, all laptop-side,
none needs a GPU. Recorded as PRD §7.1.

| # | Defect |
|---|---|
| **E1** | **The out-of-scope guard is English-only.** "the weather in Paris tomorrow" takes the `no_model` refusal path; *the same question in Arabic* reaches the model. On a bilingual platform, Arabic users walk through a door that is shut for English users, at a full inference each time. |
| **E2** | **The model fabricates investor contact details.** Asked for names and emails it has no access to, it invents a plausible roster with `@example.com` addresses rather than declining. Obviously fake on inspection — which is what makes it dangerous: a well-formatted answer carries no signal it was invented. Marked `known_failure`. |
| **E3** | **`live_ctx` omits more than §6.1 recorded.** Token *price* and *supply* are in the context payload but never rendered into the prompt. Same class as the known mint/burn/transfer and `draftAssetNames` gaps. Both EN and AR forms failed identically, ruling out a language cause. |
| **E4** | **The committed baselines are stale.** `scripts/bench_baselines/*.jsonl` were captured against the 2.0 GB **3B** model (`model_sig …/2104932768`); the default is now the 1.2 GB **1.5B** (`…/1285494304`), and the knowledge fingerprint changed too. Three *pre-existing* cases pass there and fail today. **Left failing deliberately** — they are a real quality signal from the model downgrade and relaxing them would erase it. |

**Also carry forward:** routing is far more phrasing-sensitive than expected.
"Give me the top 3 assets by AUM" → widget; "Which three assets have the highest
AUM?" → live context; "Summarise the KYC pipeline" → model, but "Summarise total
supply" → widget. **Any prompt change is a routing change.**

---

## 6. What is deliberately not built

Gateway, all PRD §7 Phase 2. Seams exist for each:

- per-key **token budgets** and **concurrency caps** — `ApiKeyRecord` already
  carries `token_budget` and `max_concurrency`; nothing enforces them
- **circuit breaker** and health-aware routing — `UpstreamPool.pick()` is
  round-robin; `health()` exists and is unused by the router
- **mTLS** gateway→vLLM
- **`/metrics`** — `prometheus-client` is a declared dependency, no endpoint yet
- **streaming token counts** — the audit record honestly logs zeros with
  `streamed=true`; real counts need `stream_options.include_usage` upstream

App side: **S3** (`_cache_key` missing `role`), **S6** (Redis unauthenticated and
published), **S7–S9**. Plus E1–E3 above.

---

## 7. Next actions, in order

1. **Book the vast.ai A10** and follow `docs/VAST_AI_RUNBOOK.md`. Run
   `scripts/verify_box.sh` first and record its output.
2. **The single highest-value measurement** is vLLM's reported KV cache blocks.
   Every capacity figure in PRD §3.3 is *calculated, not measured*, and that
   number confirms or refutes the ~105,000-token AWQ budget the whole plan rests
   on.
3. **Cold-start time** → sets `startupProbe.failureThreshold` in
   `deploy/k8s/probes.yaml`, currently a placeholder.
4. **tok/s per user at 1/3/5/8 concurrent** — report per level, not aggregate.
   This is a procurement input; DevOps sizes GPU counts off it. Flag it as a
   provisional baseline to be re-confirmed on the OCI card.
5. **Confirm B3 on real Qwen3**, including whether a user typing `/think`
   overrides `enable_thinking: false`.
6. Then Phase 2 gateway work from §6.

**Still blocking, and not ours:** PRD §8 Q1 — whether Azure staging is a
full-card A10. `NVadsA10_v5` starts at one-sixth of a card with a 4 GiB frame
buffer, and a fractional SKU boots, serves, and silently breaks the 8k context
requirement. Highest-priority question outstanding with DevOps.
