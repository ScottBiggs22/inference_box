# Handoff — state of play as of 2026-09-10 (rev 3)

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
`cloud_infra_prep`.

~~The Phase 0 changes there are uncommitted on purpose.~~ **Corrected
2026-09-11: they were committed as `d12414b` ("Phase 0 of the inference stack:
security gates, eval set, vllm backend", 2026-09-10) and pushed to
`raghav/feat/inference-phase0`.** The only dirty files in that tree now are two
rebuilt FAISS index binaries (`knowledge/.faiss_index.{json,bin}`). This mattered
enough to correct because the stated reason for leaving them uncommitted was a
standing constraint, and a session reading the old sentence would have gone
looking for uncommitted work that is not there.

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

## 5a. Phase 1 toolkit — built, and six more documented claims that were wrong

Phase 1 is now blocked only on a booked GPU. Everything the box session needs
exists, was tested against the stub, and the runbook it follows has been
rewritten around what the code actually does.

**Test/build state:** **107 gateway tests** pass (up from 58), `ruff` clean,
both image targets build, and the compose stack was verified end to end — auth,
`/readyz`, a streamed completion carrying a usage frame, and a 400 on a
malformed request. `baas-poc-templates` was **not touched**; its Phase 0
changes are still uncommitted.

### Built

| Artefact | What it is for |
|---|---|
| `scripts/pack_for_box.sh` | The transfer. Temp-index build so uncommitted work ships, deny-list on top of gitignore, printed manifest, shape-aware credential scan. Refuses to run against the app repo. |
| `scripts/loadgen.py` | The concurrency instrument. Per-user decode tok/s and TTFT p50/p95 at 1/3/5/8, 8k-shaped context, `stream_options.include_usage`. Prints the R1 gate verdict itself. |
| `scripts/b3_probe.py` | Seven-case matrix answering whether `/think` overrides `enable_thinking: false`. Sends the app's exact wire payload without the app. |
| `scripts/dev_key.sh` | Mints the key store compose needs. |
| `scripts/verify_box.sh` | Hardened. Now catches `data/`, `knowledge/`, `*.gguf` and tokenizer dirs, fails on >1 GPU, and has `--self-test` and `--scan-only`. |
| `docs/PHASE1_RESULTS.md` | Pre-cut slots for every measurement, so the paid session is fill-in-the-blanks. |
| `docs/VAST_AI_RUNBOOK.md` rev 2 | Rewritten. See its "What changed in rev 2". |

### The findings, in order of what they would have cost

**F1 — the runbook shipped the wrong app tree.** `git archive
HEAD:services/bkn301-ai-service` predated every uncommitted Phase 0 change,
including untracked `openai_http.py`. A box built from it had no
`--backend vllm`, so the benchmark could not run and B3 could not be confirmed.

**F2 — "tracked files only" was not the safety guarantee it read as.** Of the
AI service's 2.4 MB tracked payload, **1.2 MB was real platform content**:
`data/pending_knowledge.json` (40 records of verbatim `extractedText`;
`data/` is not gitignored at all) and `knowledge/.faiss_index.{json,bin}` (182
verbatim chunks plus their embeddings). `verify_box.sh` checked for none of
them. **Tracked is not the same as safe.**

Deeper: the eval suite calls the real `chat_rag`, which retrieves from the real
knowledge base and injects the chunks into the prompt — so real content travels
in the *request body* regardless of how the code arrived. Resolution: **the app
repo does not go on a rented box at all.** Eval quality moves to the OCI card,
which PRD §4.8 already names as the only place numbers may come from.

**F3 — the harness could not produce any Phase 1 headline number.**
`bench_llm_backends.py` has no concurrency support whatsoever, so "run 1/3/5/8
in parallel" was uncarryable. It never measures TTFT and cannot
(`openai_http.py:177` hardcodes `"stream": False`), and its `decode_tok_s`
divides completion tokens by whole-request latency — prefill and decode
conflated, not comparable to §3.3's ~48 tok/s. Hence `loadgen.py`.

**F4 — `verify_box.sh` would have hard-failed on its first run.** Its
credential scan was `grep -rl 'sk-ant-'`, and `tests/test_auth.py` deliberately
contains `"sk-ant-something"` as a malformed-credential fixture. The moment
this repo reached the box, step one exited 1 saying an Anthropic key was
present, under a banner reading "do not benchmark this box". Now shape-aware
(20+ chars of key material after the prefix), with no filename exclusions to
maintain, and a `--self-test` that plants every decoy. Confirmed against the
real tree: 5 files matched the old pattern, 0 match the new one.

**F5 — the image build was broken, and CI has never run.**
`pyproject.toml` declared `stub` as a package while
`deploy/Dockerfile.gateway` copied only `gateway/` into the build stage, so
`docker build` failed with `package directory 'stub' does not exist`. The
`image` job in `.github/workflows/ci.yml` would have caught it, but this repo
**has no remote**, so that workflow has never executed. The README said the
compose path worked; it could not have. Fixed by dropping `stub` from the
distribution (it is a dev tool, and an unauthenticated server has no business
in the shipping image) and adding a `dev` target that carries it.

Worth recording how that fix failed first: making `dev` the last stage meant
`docker build` with no `--target` built *it*, and because `FROM` inherits
`CMD`, the gateway container came up running the unauthenticated stub while
reporting itself healthy. A trailing `FROM runtime AS default` alias makes the
safe image the default again. CI now builds both targets and asserts
`import stub` fails in the shipping one.

**F6 — two PRD §3.1 figures were wrong; the load-bearing ones were right.**
Verified against the real `Qwen/Qwen3-8B-AWQ` `config.json`. The geometry the
memory budget depends on is exact — 36 layers, 8 KV heads, head_dim 128 →
144 KiB/token, and a 14.8 GiB budget is ~107,770 tokens, consistent with
§3.3's "~105,000". But `vocab_size` is **151,936** (not 151,552) and
`max_position_embeddings` is **40,960** (not 32,768). Three vocabulary figures
were in circulation because they measure different things: 151,936 is the
padded embedding width, 151,643 is the tokenizer's token count (B4's
measurement), and 151,552 matches neither. Corrected in place; §7 Phase 5's
"YaRN above 32k" should read 40,960.

Also: the model revision is now pinned to a real hash
(`4da05a8edb55c6046cce958586c33b61da07bb79`) rather than `<COMMIT_HASH>`, and
the repo is safetensors-only with no pickle — PRD §5.4 item 5 satisfied at the
source.

**Smaller, fixed:** `chat.py`'s unguarded `int(max_tokens)` returned 500 from
the validation layer on `{"max_tokens": "abc"}` and let a negative value
through unclamped (now 400 `max_tokens_invalid`, with tests); compose mounted a
`local-keys.json` that did not exist. **Recorded, not fixed:** PRD Phase 3's
`vllm_remote.py` shipped as `openai_http.py` and the checkbox was never ticked;
the "vendored" Qwen3 tokenizer is gitignored, untracked and has no fetch script,
so B4's fix does not reproduce from a clean clone; and `.env.example:65`
hardcodes `TOKENIZER_DIR=./models/qwen2.5-tokenizer`, which overrides the
`LLM_BACKEND` selection and silently undoes B4.

---

## 5b. Phase 1 — DONE. Measured on a rented A10, 2026-09-10.

Full record: **`docs/PHASE1_RESULTS.md`**. Raw artefacts:
`results/phase1-vastai-a10/`. Session cost **$0.117**; instance destroyed, keys
discarded, nothing left on the vast.ai account.

**Box:** full-card NVIDIA A10, 23028 MiB, driver 595.84, CUDA 13.2, compute
capability 8.6, 150 W, PCIe gen4 ×16. `verify_box.sh` passed before any weights
were pulled.

### The three priority measurements

| # | Measurement | Result |
|---|---|---|
| 1 | **KV cache** | **92,544 tokens / 12.71 GiB** (cold) vs PRD §3.3's calculated ~105,000 / 14.8 GiB — **12% optimistic**. R4 still met at **2.26× headroom** for 5×8k |
| 2 | **Cold start** | **45 s warm, 153 s cold.** `startupProbe.failureThreshold` 24 → **60** |
| 3 | **tok/s/user** | 71.7 / 60.9 / **50.9** / 48.1 at 1/3/5/8 concurrent, 8k context. **R1 gate MET at 2.54×.** PRD projected ~48 @5 — confirmed within 6% |

**B3 (item 3) is fully resolved:** `/think` does **not** override
`enable_thinking: false`, and `/no_think` does not override `true` — the request
flag wins in both directions, so no input sanitisation is needed and
`strip_think` is a belt rather than the primary defence. B3's premise was
confirmed and its predicted failure reproduced exactly at `max_tokens: 160`.
The gateway's passthrough preserves `chat_template_kwargs` (checked
gateway-vs-direct, all seven cases identical) — that was the failure that would
have broken the fix in production while every stub test stayed green.

### Ten corrections, and the pattern in them

`PHASE1_RESULTS.md` §8 lists all ten. The pattern worth carrying: **the
throughput model was accurate, the memory model was not.** §3.3's ~48 tok/s/user
came in at 50.89, while its KV budget was out by 2.1 GiB — chiefly because
`0.90 × 24 GB` is the wrong basis (the card *addresses* 22.06 GiB, so 0.90 gives
19.85 GiB), plus CUDA graphs never being budgeted on top of peak activation.
§3.1's 144 KiB/token was confirmed **to the digit**, so the arithmetic was
sound and one input was not.

Three that change how things run:

- **`--disable-log-requests` does not exist in vLLM 0.28.0.** `vllm serve`
  fails to start with it — and the PRD named it in three places. Replaced by
  `--no-enable-log-requests` / `--no-enable-log-outputs`, polarity inverted, so
  PRD §5.4's no-retention requirement is now the *default*.
- **KV cache size is not a constant.** A cold `torch.compile` inflates profiled
  peak activation by 1.0 GiB and permanently costs **7,312 tokens** of KV
  (92,544 vs 99,856). Baking the compile cache into the image is worth that
  plus ~108 s of startup — a real capacity gain, now a DevOps recommendation.
- **Prefix caching absorbs 98.5% of prompt tokens** on FirstData's traffic
  shape, confirming PRD §3.6's assumption and keeping LMCache deferred for a
  measured reason. Forcing 0% hit shows TTFT *is* prefill-bound when it misses
  (92 ms → 1,405 ms at batch 1), but R1's floor holds even there (28.9 tok/s/user).

### Two findings about the gateway itself

- **Decode overhead is zero** (±0.4% at every concurrency level) — the
  unbuffered SSE passthrough does what it claims.
- **TTFT overhead is ~85–130 ms, and it is all argon2id.** Isolated directly:
  `/healthz` 1.2 ms vs `/v1/models` 77.3 ms. `keys.py` argued the cost was
  acceptable in front of a multi-second GPU op — confirmed, and now quantified
  at 0.8% of a 10 s generation but ~2.5% of a 3 s FAQ answer. This is a measured
  argument for PRD §5.3's preference for the JWT path (~1–2 ms, ~40× cheaper),
  and a verified-key cache is the alternative if the API-key path stays hot —
  at the cost of delaying revocation by its TTL.
- **The audit log's metadata-only guarantee was verified against 78 real
  requests** to a real model: 13 keys, all metadata, zero occurrences of any
  prompt or completion word. Stronger evidence than the stub-based unit test.

### Tooling defects the session exposed

Three in the tooling built the day before, all fixed and all now covered by
tests:

1. **`verify_box.sh`'s hygiene scan would have reported PASS having examined
   nothing.** Its default target was `/workspace`, which the
   `vllm/vllm-openai` image does not have — `find` on a missing path returns
   nothing. A missing target is now a hard failure.
2. **vast.ai injects `/root/.vast_api_key` into every instance.** Verified by
   hash that it is instance-scoped, not the account key. The scan now reports
   host-injected credentials separately rather than failing, which would have
   made the gate un-passable on this provider.
3. **`pkill -f "vllm serve"` kills its own SSH session**, because `pkill -f`
   matches full command lines and the SSH command line carries the pattern. Cost
   two aborted measurement runs. The launcher now lives in a file on the box so
   a kill pattern and the launch text never share a command line.

### Not done, and why

- **Eval-set quality vs BF16** — needs the app, which needs the real knowledge
  corpus, which must not reach a rented host. OCI card, as planned.
- **Wedge-detection window** for the sidecar backstop — never wedged naturally.
  Still an estimate; carry into Phase 2 alongside the gateway breaker.
- **BF16 (config A) fallback comparison** — not needed, AWQ cleared the gate at
  2.5× the R1 floor.

### The Azure question is now sharper

PRD §8 Q1 remains **the outstanding blocker**, and this measurement gives it
teeth: the real KV budget on a *full* card is 12.71 GiB, so a half-card
`NVadsA10_v5` would have ~4 GiB and roughly **28,000 tokens** — making 5 × 8k
(40,960) **unreachable**, exactly as §4.4(a) warned but now with a measured
figure rather than an estimate.

---

## 5c. Phase 2 session — `/metrics`, breaker, retries. Done, 2026-09-11.

Built on the laptop against the stub, no GPU needed for any of it. Branch
`phase2-metrics-breaker`, seven commits, **213 tests** (up from 108), `ruff`
clean, both image targets still build.

### What shipped, in dependency order

1. **argon2id moved off the event loop, and bounded.** `require_principal`
   was `async def` calling a synchronous ~22-77ms hash inline, which stalls
   every in-flight SSE relay for that long on every API-key arrival. Dropping
   `async` moves it to FastAPI's threadpool — but **unbounded, that measured
   WORSE than doing nothing** (503.9ms loop lag at 40 concurrent verifies vs
   191.4ms inline at 8), because anyio's default 40-thread limit times
   argon2's `parallelism=4` oversubscribes the CPU. Bounded to 6
   (`AUTH_VERIFY_CONCURRENCY`) it drops to 19.7ms with no throughput cost. The
   bound also closes a real lever: an unbounded threadpool is reachable by
   unauthenticated traffic, since `_dummy_verify()` runs a full hash on an
   unknown keyid on purpose (timing-oracle defence).
2. **`/metrics`, plus a scraper for vLLM's own `/metrics`.** Unauthenticated on
   the main port per plan; every label folded to a bounded vocabulary (`model`
   to the allowlist, `reason` to a frozenset, replicas by index not URL) —
   the same reasoning that already kept `keyid` out, generalised. Three real
   parser traps closed, each verified against
   `results/phase1-vastai-a10/vllm-metrics-final.txt`: `prometheus_client`
   strips `_total` from counter *family* names (the sample keeps it — a
   family-name lookup silently finds nothing); every counter has a `_created`
   gauge sibling holding a boot timestamp that never moves (a prefix match
   reads it as a permanently flat counter); `vllm:iteration_tokens_total` is a
   histogram despite the suffix. The stub's `/metrics` could not previously
   produce the wedge signature at all (`state.waiting` was written by
   nothing) and returned JSON, not Prometheus text — both fixed, plus a new
   `--stub-wedge` flag that reproduces the signature on demand.
3. **The streaming-status bug.** A streamed request whose upstream returned
   503 reached the caller as **HTTP 200** with an error body dressed as a
   stream — `StreamingResponse` was built before the upstream status was
   read. Fixed by taking `client.send(request, stream=True)` directly instead
   of the `client.stream()` context manager, so status and headers are known
   before any header is committed to the caller. This is also what makes a
   streamed request retryable at all (see below). Caught a real gap in test
   coverage doing this: `TestClient`'s ASGI transport **collects** a
   streaming response before returning it, so no existing test had ever
   proven a real body streams incrementally through the gateway. New
   `live_gateway` fixture runs the real process on a real socket.
4. **Circuit breaker + health-aware routing.** Per-replica state machine,
   weighted consecutive-failure score (not a rate — at 3-5 concurrent users a
   rate window has no denominator). 429 carries zero weight on purpose
   (PROBE_CONTRACT.md §3's mistake, one layer up). **The load-bearing
   decision:** when every replica is open, `pick()` raises rather than
   routing anyway — at N=1 that is the common case, and the entire value is
   converting a 120s hang into an immediate 503 (tested: wall clock under 1s).
   Reclose needs two good probes AND no live wedge signal, because `/models`
   alone is a weak probe and the stub already proves it.
5. **Bounded retries with jitter on 429/502/503/504.** One retry, two
   attempts — a retry that already reached the GPU burned a decode slot. A
   read timeout is deliberately **never** retried even though it is a cousin
   of the retried connect-error, because the request may still be running;
   this is proven against a genuinely slow stub response (a real
   `httpx.ReadTimeout`), not simulated. 500 is excluded from the retryable set
   that 502/503/504 belong to — a distinction the breaker's own `FailureKind`
   does not carry, since 500 and 503 are the same `SERVER_ERROR` weight for
   trip purposes but not equally safe to retry.

### What this found that Phase 1's own numbers had already shown, unread

Phase 1 reported the *median* TTFT gateway overhead (+85-130ms) and moved on.
Re-reading `loadgen_gateway.jsonl` at the **per-request** level: TTFT minimum
is flat (~175-190ms) across every concurrency level while the maximum scales
with N (207→899ms at 1→8 concurrent), and the direct-to-vLLM path shows no
such pattern. That is the signature of something serialising in the gateway,
not of the network. Correlational, not proven mid-refactor: the distribution
is bimodal through the gateway rather than an even staircase, so the argon2
fix's local measurement (§1 above) is the actual confirmation: it targets the
same mechanism and the effect size matches, but a real-load A/B on the
gateway itself (see below) is still the decisive test.

### Not done, and why — same two items as Phase 1 left, still true

- **The wedge-detection window (120s) is still an estimate.** The stub can
  now produce the *signature* — `waiting > 0` while `generation_tokens_total`
  stays flat, verified via `--stub-wedge` — which validates the detector and
  the labelled-series parsing end to end. It cannot tell us how long a real
  engine takes to become unambiguously wedged, or how much natural plateau
  a REAL vLLM shows under heavy load (the false-positive risk a shorter
  window would carry). That needs a real engine.
- **The argon2 fix is unconfirmed under real decode load.** The local
  measurement (§1) isolates the mechanism cleanly with no GPU competing for
  CPU; Phase 1's own gateway never authenticated *concurrently with*
  in-flight decoding (loadgen's workers arrive in a burst, then stream), so
  the interaction between real GPU-bound decode and this fix has never been
  measured together.

**Both of the above were explicitly attempted this session and explicitly
NOT closed: vast.ai had zero A10 offers on 2026-09-11** (checked twice; a
plain RTX_3090 query worked, ruling out a syntax issue), and zero for A10G,
L4, or L40 — the entire card class was gone from the marketplace. The one
same-VRAM substitute, RTX 4090, was quoted at $6.67-$40/hr against Phase 1's
$0.24/hr A10 rate, and is a different architecture regardless of price
(Ada Lovelace, no ECC, 450W). **Decision: skip the GPU session rather than
pay 4090 rates for cards that cannot answer these two questions the way an
A10 would matter for.** See `docs/VAST_AI_OPERATIONS.md` for the operational
playbook this used, and the memory file
`vastai-a10-unavailable-2026-09-11.md` for the finding. Re-check availability
before the next session assumes Phase 1's pricing still holds.

---

## 6. What is deliberately not built — and what Phase 1 changed about it

All PRD §7 Phase 2. **Agreed order is dependency order, not the order §7 lists
them:** `/metrics` → breaker → budgets → mTLS. Each seam already exists, and
several now have a measurement attached that did not exist before.

**Items 1 and 2 below are done — see §5c.** Kept here for the historical
record of what was true at the Phase 1 handoff; do not build against this
section, `/metrics`, the scraper, the breaker and retries already exist.

### 1. ~~`/metrics`~~ — DONE, §5c

`prometheus-client` is a declared dependency (`pyproject.toml:48`) with no
endpoint. `METRICS_ENABLED` already exists in `config.py`. Needs
`gateway/metrics.py` + `gateway/routes/metrics.py`.

**Keep `keyid` out of the labels** — unbounded cardinality, and it drags tenant
identity onto a scrape surface that is not access-controlled the way the audit
log is. Label on `status` / `model` / `auth_method`.

Also needs a scraper for vLLM's own `/metrics` to compute the wedge signature
from `PROBE_CONTRACT.md` §4. **The real series names are now known** — captured
in `results/phase1-vastai-a10/vllm-metrics-final.txt` from the live server, so
this can be written against reality rather than against the doc:
`vllm:num_requests_running`, `vllm:num_requests_waiting`,
`vllm:num_requests_waiting_by_reason{reason=capacity|deferred}`,
`vllm:prefix_cache_hits_total`, `vllm:prefix_cache_queries_total`,
`vllm:prompt_tokens_by_source_total{source=local_compute|local_cache_hit}`.

### 2. ~~Circuit breaker + health-aware routing~~ — DONE, §5c (retries included)

`UpstreamPool.pick()` (`upstream/client.py:73-81`) is round-robin;
`health()` (`:83-92`) exists and the router never calls it. `stub/server.py
--stub-fail-after N` exists for exactly this test.

**Still unmeasured: the wedge-detection window.** Phase 1 never wedged the
engine naturally, so `PROBE_CONTRACT.md` §4's "120s, not 20s" is still an
estimate. It belongs here, with the breaker, and inducing a wedge deliberately
is the way to get it. **Still true after §5c** — the stub can now produce the
signature, which is not the same as measuring the window on a real engine.
Blocked on GPU availability, not on remaining gateway work; see §5c.

### 3. Per-key token budgets + concurrency caps

`ApiKeyRecord` already carries `token_budget` and `max_concurrency`
(`auth/keys.py:77-78`); nothing enforces them. `check_quota` is the named seam.

**The blocker here is gone.** The audit record used to log zeros for streamed
requests because usage was unavailable — `stream_options.include_usage` is now
**confirmed working end to end through the gateway's SSE passthrough**, verified
against the real vLLM on 2026-09-10. `loadgen.py` already sends it and reads the
usage frame, including the `choices: []` shape that a naive consumer crashes on.
So real streamed token counts are wiring, not research.

**A measured design input:** argon2id verification costs **~77 ms per request**
(`/healthz` 1.2 ms vs `/v1/models` 77.3 ms). That is fine in front of a 10 s
generation, but it means a concurrency cap implemented as "verify then queue"
burns 77 ms of CPU per rejected request too. Verify cheaply first, or cache
verified keys with a short TTL — at the cost of delaying revocation by that TTL,
which is a decision to make deliberately.

### 4. mTLS gateway→vLLM

Env-var cert/key/CA paths only (no cloud SDK — `tests/test_config.py`
enforces), passed to `httpx.AsyncClient(cert=..., verify=...)` in
`UpstreamPool.start()`. Scoped honestly: code, config surface and a
self-signed-cert test. PRD §4.8 is explicit that the security review runs
against the real deployment path, so this is not *validated* on a laptop.

### Still open elsewhere

App side: **S3** (`_cache_key` missing `role`), **S6** (Redis unauthenticated
and published), **S7–S9**, plus **E1–E3** above. Gateway side, from the Phase 1
audit: PRD §7 Phase 3's `vllm_remote.py` shipped as `openai_http.py` and
hardcodes `"stream": False` (`openai_http.py:177`), so the app still has no
streaming path and no TTFT — that is C2's missing plumbing, not just a product
decision. And the "vendored" Qwen3 tokenizer is still gitignored and untracked
with no fetch script, so B4's fix does not reproduce from a clean clone.

---

## 7. Next actions, in order

**Phase 1 is complete (§5b). `/metrics`, the breaker and retries are complete
(§5c).** The live work is **per-key token budgets + concurrency caps → mTLS.**
`stream_options.include_usage` is confirmed working end to end through the
passthrough on the wire, but the gateway still does not parse the usage frame
out of its own streamed passthrough — `bkn301_gateway_requests_missing_usage_total`
now makes that gap visible in production rather than silent, and closing it is
part of the budget work, not separate from it.

**Two measurements remain genuinely open, both blocked on GPU availability
rather than on more gateway code:** the wedge-detection window (still a
120s estimate; the stub can produce the signature but not calibrate the
window against a real engine), and confirming the argon2-threadpool fix (§5c
item 1) under real concurrent decode load rather than in isolation. vast.ai
had **zero A10 offers** as of 2026-09-11 — re-check before assuming Phase 1's
$0.24/hr pricing or availability still holds; see §5c and
`docs/VAST_AI_OPERATIONS.md`.

<details><summary>Phase 1 items, now closed — kept for the record</summary>


1. **Book the vast.ai A10** and follow `docs/VAST_AI_RUNBOOK.md` **rev 2**.
   Transfer with `scripts/pack_for_box.sh` — read the manifest it prints — then
   run `scripts/verify_box.sh` *before pulling weights* and paste its output
   into `docs/PHASE1_RESULTS.md` §1. Do **not** transfer the app repo (§5a F2).
2. **The single highest-value measurement** is vLLM's reported KV cache blocks.
   Every capacity figure in PRD §3.3 is *calculated, not measured*, and that
   number confirms or refutes the ~105,000-token AWQ budget the whole plan rests
   on.
3. **Cold-start time** → sets `startupProbe.failureThreshold` in
   `deploy/k8s/probes.yaml`, currently a placeholder.
4. **tok/s per user at 1/3/5/8 concurrent** — `scripts/loadgen.py`, run **on the
   box** so the tunnel is not in the TTFT. Per level, not aggregate. This is a
   procurement input; the script writes the provisional-baseline caveat into its
   own output header so it survives a copy-paste.
5. **Confirm B3 on real Qwen3** with `scripts/b3_probe.py`, including whether a
   user typing `/think` overrides `enable_thinking: false`. Run it twice —
   gateway and `--direct` — because a disagreement means the gateway is dropping
   `chat_template_kwargs` in passthrough and B3's fix is broken in production
   while every local test stays green.
6. Then Phase 2 gateway work from §6, in dependency order:
   **`/metrics` → breaker + health routing → budgets + caps → mTLS.** `/metrics`
   first because both the breaker and `PROBE_CONTRACT.md` §4's wedge detection
   consume it; budgets after the streamed token counts they need, which now work
   end to end through the SSE passthrough.

**Still blocking, and not ours:** PRD §8 Q1 — whether Azure staging is a
full-card A10. `NVadsA10_v5` starts at one-sixth of a card with a 4 GiB frame
buffer, and a fractional SKU boots, serves, and silently breaks the 8k context
requirement. Highest-priority question outstanding with DevOps.

</details>
