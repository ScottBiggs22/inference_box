# bkn301-inference

Authenticating, OpenAI-compatible gateway in front of self-hosted **vLLM**
serving **Qwen3-8B-AWQ** on a single 24 GiB NVIDIA A10.

`docs/INFERENCE_SERVICE_PRD.md` is the source of truth for the design, the
memory budget, the security model and the phase plan. This README covers only
what you need to run the thing.

---

## Why a gateway exists at all

vLLM's OpenAI server has exactly one authentication primitive: `--api-key`, a
single static shared secret. It has **no per-tenant identity, no rate limiting,
no quotas, no per-key audit trail and no request-size limits**. It must never be
the edge. Everything security-shaped lives here (PRD §5.1).

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

---

## Running it locally, with no GPU

An M-series MacBook has no CUDA and Docker Desktop on macOS has no GPU
passthrough, so **vLLM cannot run on the laptop at all**. `stub/server.py` is a
deterministic fake serving the parts of the OpenAI surface this gateway touches,
which makes all gateway work unblocked locally (PRD §4.7).

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

`dev_key.sh` is a prerequisite, not a convenience. Compose bind-mounts
`deploy/local-keys.json` read-only, and Docker's response to a read-only bind of
a missing file is to create a *directory* at that path — after which the gateway
comes up unable to authenticate anybody. The store is generated rather than
committed because it is a per-environment credential registry, which is why
`keys.json` is gitignored in the first place.

Two build targets, and the distinction matters: `runtime` is the shipping image,
`dev` is `runtime` plus `stub/`. The stub is an OpenAI-compatible server with no
authentication of any kind, so it is neither in the installed package nor in the
image that deploys. CI asserts it.

The stub can reproduce the failure modes the gateway has to survive:

| Flag | Simulates |
|---|---|
| `--stub-think` | Qwen3 emitting a `<think>` block — exercises the B3 stripper |
| `--stub-latency 10` | a slow decode — exercises probe tuning and the read/connect split |
| `--stub-fail-after 5` | upstream overload (503) — exercises the circuit breaker |

---

## Layout

```
gateway/
  main.py            single app factory, lifespan-managed
  config.py          env vars only — no cloud SDK, ever (PRD §4.4d)
  auth/keys.py       argon2id, bkn_live_<keyid>_<secret>, expiry, revocation
  auth/jwt.py        asymmetric-only validation
  routes/            chat.py (SSE + non), models.py, health.py
  upstream/client.py replica pool, streaming passthrough
  audit.py           metadata only — never prompt text
stub/server.py       the no-GPU development upstream
scripts/
  pack_for_box.sh    the audited, data-free transfer to a rented box
  verify_box.sh      first-boot GPU + data-hygiene gate (has --self-test)
  loadgen.py         per-user tok/s and TTFT at 1/3/5/8 concurrent
  b3_probe.py        does /think override enable_thinking: false?
  dev_key.sh         mint a local dev key for the compose path
deploy/              Dockerfile (runtime + dev), compose, k8s probe config
docs/                the PRD, the probe contract, the rented-box runbook
```

---

## Things that are deliberate

**Environment variables are the only config surface.** Production cloud is
undecided (OCI or Azure) and staging is planned for Azure, so the image contains
no cloud SDK, no instance principal and no managed identity. The platform
injects; we read. `tests/test_config.py` asserts no cloud SDK is importable, and
that the documented comma-separated form actually parses — it did not, once.

**The audit log is metadata only.** Timestamp, key id, model, token counts,
latency, status. Never prompt or completion text — that is the difference between
an audit trail and a PII store, on a platform handling investor and KYC data.
`tests/test_audit_no_prompt_text.py` enforces it against the real writer.

**JWT validation is asymmetric only**, and a symmetric algorithm is refused at
construction. The gateway must be able to *verify* tokens and unable to *mint*
them, so compromising it cannot forge caller identity.

**Streaming is passed through, not buffered.** At 20 tok/s a 200-token answer is
ten seconds; buffering it to inspect the body would mean ten seconds of blank
screen. The consequence is real and worth stating: the gateway cannot
post-process a streamed answer, so validators needing the whole text live at the
consuming end (PRD §6.3 C2).

---

## Observability

`/metrics` is **unauthenticated on the main port**, like any Prometheus target —
restrict it with a network policy. That is why no label carries a `keyid`, an
upstream URL, or any caller-supplied string: on a surface with no access control
the protection has to be in what is collected, not in who may read it.

The gateway also scrapes each replica's own `/metrics` to derive the wedge
signature from `PROBE_CONTRACT.md` §4 — `num_requests_waiting > 0` while
`generation_tokens_total` stays flat. It does **not** re-export vLLM's series;
point Prometheus at vLLM directly for those. `bkn301_gateway_upstream_wedge_stall_seconds`
is exported continuously, including below the threshold, because that threshold
is still an estimate and this series is what will eventually replace it.

## Not built yet

PRD §7 Phase 2, in dependency order rather than the order §7 lists them. The
first two have landed:

1. ~~**`/metrics`**~~ — done. Plus the vLLM scraper and wedge detector.
2. ~~**Circuit breaker and health-aware routing**~~ — done. `pick()` skips open
   replicas and raises when all are open, which the route turns into a fast 503
   rather than a two-minute hang.
3. **Per-key token budgets and concurrency caps** — `ApiKeyRecord` already
   carries `token_budget` and `max_concurrency`. Note what is *not* yet true:
   `stream_options.include_usage` works end to end on the wire, but the gateway
   does not parse the usage frame out of its own passthrough, so every streamed
   request still audits zero tokens. `bkn301_gateway_requests_missing_usage_total`
   counts the gap. That parsing is this slice's work.
4. **Bounded retries with jitter on 429/503** — the breaker records outcomes but
   nothing retries yet.
5. **mTLS gateway→vLLM.**

---

## Status

**Phase 0 and Phase 1 complete. The core capacity requirements are met and
measured on real hardware, not calculated.** On a full-card A10:

| Requirement | Result |
|---|---|
| R1 ≥20 output tok/s/user | **50.9 tok/s/user** at 5 concurrent, 8k context — 2.54× |
| R4 8k context goal | **92,544 tokens** of KV cache — 2.26× headroom for 5×8k |
| Cold start | 45 s warm / 153 s cold → `startupProbe.failureThreshold` 60 |

PRD §3.3's KV budget was 12% optimistic; its throughput projection was accurate
within 6%. See `docs/PHASE1_RESULTS.md`, with raw artefacts in
`results/phase1-vastai-a10/`.

Two caveats that "capacity met" does not cover: **eval quality vs BF16 is
unmeasured** (it needs the app, which needs the real knowledge corpus, which
must not reach a rented host — so it runs on the OCI card), and these are
**rented-box numbers to be re-confirmed** on `VM.GPU.A10.1` before anyone
multiplies them for procurement.

`ruff check . && pytest` is green (**192 tests**), both image targets build, and
the compose stack is verified end to end.

**Start here:**

| Doc | What it is |
|---|---|
| `docs/HANDOFF.md` | Current state — read first. What is done, which PRD claims did not survive contact with the code, and the app defects eval calibration turned up. |
| `docs/INFERENCE_SERVICE_PRD.md` | Design source of truth (rev 3), with in-place corrections. |
| `docs/VAST_AI_RUNBOOK.md` | Getting this onto a rented A10 for Phase 1 (rev 2). |
| `docs/PHASE1_RESULTS.md` | The Phase 1 measurements, and the ten corrections they forced into the PRD. |
| `docs/NEXT_SESSION_PROMPT.md` | Paste-ready prompt for a fresh session. Start here if you are picking this up cold. |
| `docs/PROBE_CONTRACT.md` | The probe design, extracted for DevOps. |
