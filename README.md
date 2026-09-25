# bkn301-inference

An authenticating, OpenAI-compatible gateway that sits in front of a
self-hosted **vLLM** server running **Qwen3-8B-AWQ** on a single 24 GiB
NVIDIA A10 GPU. It exists because vLLM's OpenAI-compatible server has exactly
one authentication primitive — `--api-key`, a single static shared secret —
and **no per-tenant identity, no rate limiting, no quotas, no per-key audit
trail and no request-size limits**. vLLM must never be the edge; everything
security-shaped for this service lives here instead.

The gateway is consumed by a fintech platform ("FirstData")'s C#/.NET API
(`baas-poc-templates`) as a third LLM backend, alongside `llamacpp` and
`qvac`. It handles investor/KYC-adjacent data, which is why the security and
audit posture below is stricter than a typical internal proxy.

This doc is the onboarding entry point for DevOps and new developers. For
design rationale and full history, see [Where to go deeper](#where-to-go-deeper).

---

## Architecture

```
Client (C# API / Web / Mobile)
  │  TLS 1.3, short-lived JWT or API key
  ▼
Edge — Cloudflare Tunnel or LB + WAF
  ▼
Gateway  ← this repo
  │  authN/authZ · request validation · model allowlist
  │  metadata-only audit · SSE passthrough
  ▼  private subnet
vLLM replicas (no public IP, --no-enable-log-requests)
```

The gateway itself is a single async FastAPI app (`gateway/main.py`), CPU/IO
bound — it does not need a GPU. vLLM is the GPU-bound piece it fronts.

| Path | Responsibility |
|---|---|
| `gateway/main.py` | App factory, lifespan-managed startup/shutdown; runs background tasks for metrics scraping, breaker probing, and event-loop lag monitoring. |
| `gateway/config.py` | Env-vars-only settings (no cloud SDK, ever — see [Configuration](#configuration)). |
| `gateway/auth/keys.py` | Argon2id-hashed API keys (`bkn_live_<keyid>_<secret>`), expiry, revocation, per-key token budget and concurrency limits. |
| `gateway/auth/jwt.py` | Asymmetric-only JWT validation (RS256) — the gateway can verify tokens but never mint them. |
| `gateway/routes/chat.py` | Chat completions (SSE + non-streaming), request validation, quota/concurrency gating. |
| `gateway/routes/health.py`, `models.py`, `metrics.py` | `/healthz`, `/readyz`, `/v1/models`, `/metrics`. |
| `gateway/upstream/client.py` | Replica pool, shared `httpx.AsyncClient`, streaming passthrough, bounded retries with jitter, mTLS wiring. |
| `gateway/upstream/breaker.py` | Per-replica circuit breaker (weighted consecutive-failure score, not a rate). |
| `gateway/upstream/metrics_scraper.py` | Scrapes each vLLM replica's own `/metrics` to derive a "wedge" (frozen-engine) signal. |
| `gateway/quota.py` | Token-budget and in-flight-concurrency enforcement, wired into `chat.py`. |
| `gateway/audit.py` | Metadata-only audit log writer — never prompt/completion text. |
| `gateway/metrics.py` | Prometheus metric definitions + ASGI middleware. |
| `stub/server.py` | Deterministic, unauthenticated fake OpenAI-compatible upstream — lets the whole gateway run with no GPU. Excluded from the packaged distribution and the production image. |
| `scripts/` | Ops tooling: `pack_for_box.sh` (audited transfer to a rented GPU box), `verify_box.sh` (first-boot GPU + data-hygiene gate), `loadgen.py` (tok/s and TTFT benchmarking), `b3_probe.py` (Qwen3 `<think>`-block probe), `dev_key.sh` (mint a local dev key). |
| `deploy/` | `Dockerfile.gateway` (multi-stage: build → runtime → dev), `docker-compose.yml`, `k8s/probes.yaml`. |
| `docs/` | Design docs, session history, operational runbooks — see [Where to go deeper](#where-to-go-deeper). |

**Design choices worth knowing up front:**

- **Streaming is passed through, not buffered.** At ~20 tok/s a 200-token
  answer takes ten seconds; buffering it to inspect the body would mean ten
  seconds of blank screen. The gateway cannot post-process a streamed answer
  — validators needing the whole text live at the consuming end.
- **The audit log is metadata only** — timestamp, key id, model, token
  counts, latency, status. Never prompt or completion text. That's the
  difference between an audit trail and a PII store on a platform handling
  investor/KYC data.
- **JWT validation is asymmetric only**, and a symmetric algorithm is refused
  at construction, so compromising the gateway cannot forge caller identity.

---

## Deployment model

vLLM needs a **whole 24 GiB A10 GPU per replica** — no MIG, no fractional/vGPU
sharing, because vLLM pre-allocates KV cache and sharing fails at boot. The
gateway itself is IO-bound and runs anywhere; scale it via replicas, not
worker processes (`--workers 1` in the shipped image).

### Local development, no GPU needed

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# 1. the fake upstream
.venv/bin/python -m stub.server --port 8001

# 2. a throwaway key
export API_KEY_PEPPER=local-dev-pepper
.venv/bin/python -c "
from gateway.auth.keys import mint_key, KeyStore
s = KeyStore('keys.json'); pt, rec = mint_key(scopes=['chat'], label='dev')
s.add(rec); s.save(); print(pt)"

# 3. the gateway
.venv/bin/python -m uvicorn gateway.main:app --port 8080
```

```bash
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/v1/models
```

Or the whole stack in containers, with no local Python at all:

```bash
bash scripts/dev_key.sh          # mints deploy/local-keys.json — do this FIRST
docker compose -f deploy/docker-compose.yml up --build
```

(`dev_key.sh` is a prerequisite, not a convenience: compose bind-mounts
`deploy/local-keys.json` read-only, and Docker turns a missing read-only bind
into a directory, which leaves the gateway unable to authenticate anybody.)

**Docker image:** two build targets — `runtime` (ships, gateway only) and
`dev` (`runtime` + `stub/`, for compose). The default/final stage is
explicitly pinned to alias `runtime`: a past bug had `dev` as the last stage,
so a plain `docker build` with no `--target` shipped the unauthenticated stub
as the default image. CI now asserts the stub isn't importable in the runtime
image, no compiler survives into it, and it doesn't run as root.

**vast.ai** is used only as a temporary rented-GPU box for dev/benchmarking
(never production — see `docs/VAST_AI_RUNBOOK.md` / `docs/VAST_AI_OPERATIONS.md`).

### Provisioning a GPU host (staging/production)

This is what DevOps needs to have, do, or run — the specifics come from
DevOps's own answers recorded in PRD §4.1 and §4.4, not from guesswork.

- **Shape/SKU.** OCI: `VM.GPU.A10.1` — confirmed and quota-approved. Baseline
  count is 1 dev / 1 staging / 2 prod, adjusted by measured throughput
  (`docs/PHASE1_RESULTS.md`). Azure (currently the staging plan): the
  `NVadsA10_v5` series is **SR-IOV partitioned by default**, from 1/6 of a
  card (4 GiB frame buffer) up to a full 24 GiB card — a fractional size
  boots and serves normally but **silently breaks the 8k-context
  requirement**. The SKU must be confirmed as a full-card size (believed
  `NV36ads_A10_v5` or larger — verify against current Azure docs before
  provisioning). This is the single highest-priority open question with
  DevOps (PRD §8, Q1).
- **One whole GPU per pod, never a fraction.** `nvidia.com/gpu: 1` in the pod
  spec. A10 has no MIG (that's A100/A30/H100 only), so nothing else —
  no second container, no graphics workload, no other `nvidia-smi`-visible
  tenant — may share the card.
- **Driver/CUDA.** vLLM `v0.28.0` is a CUDA 13.0 build; a CUDA-12.x-only
  driver cannot run it. Verified working combination: driver 595.84 / CUDA 13.2.
- **Disk.** ≥30 GB free (~6 GB of model weights, plus image layers and the
  HF cache).
- **Network.** vLLM replicas get **no public IP** — private subnet only,
  reachable solely by the gateway.
- **Fault-domain placement (OCI).** Put the dev VM in a different fault
  domain from every production VM, and the two production VMs in different
  fault domains from each other — two prod replicas on one physical node is
  not actually HA. Staging can share a fault domain with dev (lower risk
  pairing). Fault domains are free and chosen at launch time; confirm with
  DevOps that they're actually being placed this way (PRD §8, Q2).
- **Which environment is the performance reference.** State explicitly that
  OCI (once its "functional but not in use" staging is exercised) is the
  capacity/throughput reference, and Azure staging is application-integration
  only — a green Azure run should not be read as evidence the tok/s target is
  met if its card isn't production-identical (PRD §4.4c, §8 Q3).

### Running vLLM

- Image: `vllm/vllm-openai:v0.28.0`, model pinned by commit hash
  (`--revision=4da05a8edb55c6046cce958586c33b61da07bb79`) rather than a
  moving tag.
- Launch flags: `--model=Qwen/Qwen3-8B-AWQ --max-model-len=8192
  --gpu-memory-utilization=0.90 --no-enable-log-requests
  --no-enable-log-outputs`. **Not** `--disable-log-requests` — that flag
  doesn't exist in `v0.28.0` and `vllm serve` refuses to start with it.
- Probes: apply `deploy/k8s/probes.yaml` as-is for the vLLM Deployment — it
  targets vLLM's own `/health`, not the gateway's. The numbers are measured,
  not placeholders: `startupProbe` gives a 300s boot budget (60 × 5s) against
  a measured 153s cold start / 45s warm restart — do not tighten this, a
  120s budget boot-loops a cold pod. `readinessProbe` checks engine health
  only, deliberately never queue depth (a busy-but-healthy replica pulled
  from rotation during a traffic spike takes the whole service down with it —
  load-aware routing belongs in the gateway, which already tracks in-flight
  requests). `livenessProbe` is a last resort (~60s to restart) and is never
  a test generation, since a legitimate decode can take 10s+ under load. Full
  rationale in `docs/PROBE_CONTRACT.md`.
- HA target: ≥2 production replicas, in different fault domains, listed
  together in the gateway's `UPSTREAM_URLS`.

### Running the gateway

- Build `deploy/Dockerfile.gateway --target runtime` explicitly and push to
  whichever registry the environment uses (OCIR on OCI, ACR on Azure — same
  shape, no image reuse across environments). CI's `image` job builds both
  targets and asserts the stub isn't in the runtime image, no compiler
  survived into it, and it doesn't run as root — treat a failure there as a
  blocker, not a warning.
- Run one process per pod, `--workers 1` (already the image's `CMD`) — the
  gateway is IO-bound, so scale by adding replicas, not workers. Exposes
  `8080`. Already runs non-root.
- **No Kubernetes manifest exists yet for the gateway itself** —
  `deploy/k8s/probes.yaml` is vLLM's only. Whoever deploys this needs to
  write a Deployment + Service (+ Ingress/edge wiring) for the gateway, with
  its liveness/readiness pointed at the gateway's **own** `/healthz` and
  `/readyz` — not vLLM's `/health` (a mix-up the PRD explicitly calls out as
  having happened once already).
- Edge: TLS termination and rate-limiting/WAF sit in front of the gateway
  (Cloudflare Tunnel or LB+WAF) — the gateway itself should have no public
  exposure beyond that edge. `/metrics` is unauthenticated by design, so it
  needs a network policy restricting who can reach it.

### Secrets and config to provision

Everything below arrives as an **environment variable** — no cloud SDK is
baked into the image, so whatever secret store the platform uses (OCI Vault +
instance principal, or Azure Key Vault + managed identity) just needs to land
as env vars or mounted files the env vars point at:

| Variable | What it is |
|---|---|
| `UPSTREAM_API_KEY` | vLLM's static shared secret — private-subnet-only, never leaves it |
| `API_KEY_PEPPER` | Argon2id pepper, generated per environment, never committed, never stored beside the key store |
| `JWT_PUBLIC_KEY` | The issuing API's asymmetric signing public key (RS256) — the gateway must never hold a private or symmetric key |
| `UPSTREAM_CLIENT_CERT_PATH` / `UPSTREAM_CLIENT_KEY_PATH` / `UPSTREAM_CA_BUNDLE_PATH` | mTLS material for gateway→vLLM — sourced from whatever CA issues certs on the private subnet |
| `API_KEY_STORE_PATH` | Writable path for the per-environment API key registry (generated, not committed) |
| `AUDIT_LOG_PATH` | Writable/persisted path for the metadata-only audit log |

### Observability to wire up

- Prometheus scrape targets: the gateway's own `/metrics` (behind a network
  policy) and each vLLM replica's `/metrics` directly — the gateway doesn't
  re-export vLLM's series, only derives a wedge signal from them.
- Alerts worth having from day one: tok/s/user below target, TTFT p95,
  KV-cache utilization, circuit-breaker trips, and
  `bkn301_gateway_upstream_wedge_stall_seconds` crossing its (currently
  estimated, not measured) window.
- The PRD also recommends a small sidecar that scrapes vLLM's `/metrics` and
  fails `/healthz` if the wedge signature holds for ~120s+, as an automated
  backstop behind the gateway's own circuit breaker (the primary defense).
  **Not built yet** — see [Current status](#current-status).

### Open questions this repo can't close alone

These need a DevOps (or product) answer before staging/production sizing and
placement are real — see PRD §8 for the full list:

- Is the Azure staging SKU actually a full 24 GiB card, or a fractional
  `NVadsA10_v5` partition? (Highest priority.)
- Can the four VMs (dev/staging/2×prod) actually be placed in different
  fault domains, or do they land on the same physical node?
- Has vLLM been validated on an Azure vGPU (NVads/GRID) profile at all? Not
  yet, as of this writing — treat it as a spike, not a given, if Azure is firm.
- What's the network path from the consuming app to the gateway — same-VCN,
  or a tunnel/VPN/cross-region hop? This is currently the single largest
  unknown in the latency budget.
- When will production cloud (OCI vs. Azure) actually be decided?

---

## Configuration

**Environment variables are the only config surface** — no cloud SDK, no
instance principal, no managed identity anywhere in the image, because the
target cloud is still undecided and every candidate platform supports "inject
an env var." `tests/test_config.py` enforces this. Full reference:
[`.env.example`](.env.example) and `gateway/config.py`.

Knob categories:

| Category | Examples |
|---|---|
| Server | `GATEWAY_HOST`, `GATEWAY_PORT` |
| Upstream vLLM | `UPSTREAM_URLS` (comma-separated replicas), `UPSTREAM_API_KEY`, connect/read timeouts, mTLS cert/key/CA paths |
| Resilience | `BREAKER_ENABLED`, `BREAKER_OPEN_SEC`, `BREAKER_PROBE_TICK_SEC` |
| Authentication | `API_KEY_STORE_PATH`, `API_KEY_PEPPER`, `AUTH_VERIFY_CONCURRENCY`, JWT public key/issuer/audience |
| Request validation | `ALLOWED_MODELS`, `MAX_BODY_BYTES`, `MAX_COMPLETION_TOKENS`, `MAX_PROMPT_TOKENS` |
| Audit | `AUDIT_LOG_PATH`, `AUDIT_INCLUDE_PROMPT_TEXT` (must stay `false`) |
| Observability | `METRICS_ENABLED`, `UPSTREAM_METRICS_POLL_SEC`, `UPSTREAM_WEDGE_WINDOW_SEC` |

`/metrics` is **unauthenticated on the main port**, like any Prometheus
target — restrict it with a network policy. That's why no label carries a
`keyid`, an upstream URL, or any caller-supplied string.

---

## Testing

Framework: **pytest** (config lives entirely in `pyproject.toml`, no
Makefile). No GPU or external services are needed anywhere in the suite —
`stub/server.py` fakes the OpenAI surface, so it's fully laptop-runnable.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

This is exactly what CI runs (`.github/workflows/ci.yml`, `check` job):
`ruff check .` then `pytest`. A separate `image` job builds both Docker
targets and asserts the image-hygiene properties above.

| Test file | Covers |
|---|---|
| `test_auth.py` | Key format/roundtrip, revoked/expired/unknown-key rejection, JWT verifier (asymmetric-only, algorithm-confusion incl. `alg=none`), missing/garbage credentials → 401. |
| `test_auth_concurrency.py` | Argon2id verification runs off the event loop and is bounded. |
| `test_audit_no_prompt_text.py` | The audit log never contains prompt/completion text, asserted with a canary string against the real writer. |
| `test_request_validation.py` | Malformed requests return 4xx, never 500. |
| `test_config.py` | Env-var-only config surface: `UPSTREAM_URLS` list parsing, mTLS cert/key/CA validation. |
| `test_breaker.py` | Pure breaker state machine (injected clock): trip thresholds, wedge-signal confirmation, open/reclose backoff, probation. |
| `test_breaker_routing.py` | Breaker as the router sees it over a real socket: all-replicas-open fails fast (not a 120s hang), `/readyz` behavior, half-open recovery. |
| `test_retries.py` | Bounded retries with jitter on 429/502/503/504 — connect errors retried, read timeouts never retried, `Retry-After` handling, `MAX_ATTEMPTS == 2`. |
| `test_metrics.py` | `/metrics` endpoint format, cardinality bounds, request instrumentation. |
| `test_upstream_metrics.py` | Parses vLLM's own `/metrics` (against a real captured scrape) to derive the wedge signal. |
| `test_mtls.py` | Gateway→vLLM mTLS wiring: rejects missing/wrong-CA client certs, succeeds with a correct chain, fails closed if not configured. |
| `test_quota.py` | Per-key token budgets and concurrency caps, including that a streamed response holds its concurrency slot for the life of the stream. |
| `test_streaming_status.py` | The streaming path reports the upstream's real HTTP status (regression test for a prior bug where a 503 reached the caller as 200). |
| `test_loadgen.py` | `scripts/loadgen.py` end-to-end: concurrent workers genuinely overlap, TTFT/decode-rate reporting. |
| `test_pack_for_box.py` | `pack_for_box.sh`'s deny-list refuses to ship data/credential decoys. |
| `test_verify_box.py` | `verify_box.sh`'s data-hygiene scan (`--self-test`/`--scan-only` — GPU checks can't run on a laptop). |

A handful of tests (anything using the `live_stub` or `live_gateway`
fixtures — loadgen, breaker-routing's real-socket cases, retries-over-a-real-stub,
quota concurrency, streaming-status, mTLS) spawn a **real subprocess** and
health-poll it against a 30-second wall-clock deadline. **If the laptop
sleeps mid-run, this class of test can fail spuriously** (the poll loop
burns its budget without the subprocess getting real wall-clock time to
boot) — that's a stall artifact, not a regression, if you see it.

---

## Current status

PRD Phase 0–2 are complete: `/metrics` + vLLM metrics scraping, the circuit
breaker with health-aware routing, per-key token budgets, bounded retries
with jitter, and gateway→vLLM mTLS have all shipped and are tested (as of
commit `c9af5ca`). Core capacity requirements (≥20 tok/s/user, 8k context)
were measured on a real rented A10 with 2×+ headroom on both — see
`docs/PHASE1_RESULTS.md`.

Work is currently parked waiting on an external answer, not a technical
blocker in this repo: whether Azure staging provides a full-card A10, since
some Azure GPU SKUs boot as a fraction of a card with a reduced VRAM frame
buffer that would silently break the 8k-context requirement (PRD §8 Q1).

One open technical item, no GPU required to pick up: the wedge-detection
signal (`num_requests_waiting > 0` while `generation_tokens_total` stays
flat) doesn't reliably fire on a real frozen-engine process — a
complementary signal (e.g. alerting on elevated in-flight requests with no
matching completions, or a synthetic probe through the real request path) is
still needed.

---

## Where to go deeper

| Doc | What it is |
|---|---|
| `docs/HANDOFF.md` | Session history and current state — read first if picking this up cold. |
| `docs/INFERENCE_SERVICE_PRD.md` | Design source of truth: requirements, stack feasibility, deployment contract, security design, phased plan. |
| `docs/PROBE_CONTRACT.md` | Health-probe design, extracted for DevOps. |
| `docs/VAST_AI_RUNBOOK.md` | Getting onto a rented A10 for dev/benchmarking. |
| `docs/VAST_AI_OPERATIONS.md` | vast.ai booking/session-management mechanics. |
| `docs/PHASE1_RESULTS.md`, `docs/PHASE2_L4_RESULTS.md` | Raw capacity/behavior measurements from real GPU sessions. |
| `docs/NEXT_SESSION_PROMPT.md` | Paste-ready prompt for picking up a fresh working session. |
