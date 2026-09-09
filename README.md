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
vLLM replicas (no public IP, --disable-log-requests)
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
docker compose -f deploy/docker-compose.yml up --build
```

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
deploy/              Dockerfile, compose, k8s probe config
docs/                the PRD, and the probe contract for DevOps
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

## Not built yet

The first slice stops short of these deliberately. They are PRD §7 Phase 2:

- per-key token budgets and concurrency caps — the record fields and the seam exist
- circuit breaker and health-aware routing — the router interface exists
- mTLS gateway→vLLM
- `/metrics` beyond the stub's

---

## Status

Phase 0 complete; Phase 1 waits on a GPU. `ruff check . && pytest` is green
(58 tests), and the stack has been verified end to end against the stub.
