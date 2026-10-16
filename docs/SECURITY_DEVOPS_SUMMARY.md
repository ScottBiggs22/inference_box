# Inference boxes — security summary for DevOps

**Date:** 2026-10-15 · **Covers:** `bkn301-inference` (vLLM) and `bkn301-qvac-inference` (QVAC / llama-server) · **Basis:** code and doc read-through; nothing re-run. Full detail with file:line cites: `docs/SECURITY.md` in each repo.

## TL;DR

- Both boxes share one gateway codebase: an authenticating edge with argon2id API keys, asymmetric-only JWTs, per-key quotas, a metadata-only audit log, and a non-root container. That part is solid.
- **Neither box enforces network isolation.** The engine is bound `0.0.0.0:8000` in both; "private subnet only" is an assumption. No NetworkPolicy, firewall or `securityContext` exists in either repo.
- **Edge TLS 1.3, WAF and global rate limiting are design-only** — the gateway speaks plain HTTP and has no request-rate limiting.
- **Engine-side mTLS isn't built on either box.** The gateway's mTLS client is implemented and tested (self-signed only). vLLM needs `--ssl-*` flags nobody has specified; QVAC's build has no SSL at all, so it needs a sidecar that is designed but unwritten.
- QVAC is the weaker engine: one static bearer key (passed in argv), no native TLS, no deploy-path weight checksum, placeholder image.

## Side by side

| | vLLM box | QVAC box |
|---|---|---|
| Engine native TLS | Available (`--ssl-*`); **not configured** in any artefact | **Not available** in measured CUDA build |
| Gateway → engine mTLS client | Implemented, tested (self-signed) | Same code, same tests |
| Engine-side mTLS | Not specified | **Design only**: nginx/envoy sidecar, engine on 127.0.0.1; nothing written |
| Engine auth | `--api-key` **never set** in any artefact or boot log | `--api-key` set, but **visible in argv**; single static key |
| Engine bind | `0.0.0.0:8000` (observed) | `0.0.0.0:8000` (k8s args) |
| Model integrity | Revision pinned; safetensors checked by hand once; **no checksum** | Revision + sha256 pinned, verified **only in bench scripts**, not in deploy path |
| Engine image | `vllm/vllm-openai:v0.28.0` (tag, not digest) | `REPLACE_ME/...:UNPINNED`; non-hermetic source build |
| Prompt retention | Off (`--no-enable-log-requests/outputs`, explicit) | Off by default (canary-tested); **`--log-prompts-dir` must never be set**, nothing enforces it |
| Unauth engine endpoints | n/a (not characterised) | `/health` only; `/slots`, `/metrics`, `/v1/models` need the key |
| systemd units | None | None |

## Same in both (gateway)

**Implemented:** non-root image, multi-stage, no compiler, stub excluded from shipping image; argon2id + pepper with dummy-verify against enumeration; argon2 concurrency bound; JWT algorithm pinning; scopes; per-key token budget and concurrency cap; metadata-only audit (canary-tested); body/model/token limits; no CORS; bounded metric labels; pre-transfer secret scan for rented boxes.

**Gaps:**
- `/readyz` returns **private upstream URLs** unauthenticated.
- `/docs`, `/redoc`, `/openapi.json` are **public**; `/metrics` is unauthenticated by design.
- **No request-rate limiting.** Quotas are in-memory, per-process, unlimited unless set at mint time, and JWT callers are exempt.
- Pepper is **optional** (startup warning only).
- **Revocation needs a restart** (key store never reloaded); no rotation tooling; key store isn't `chmod 600`.
- **Failed auths aren't audited**; audit log has no rotation, retention, integrity or shipping.
- Body is fully read before the size check; extra request fields (`n`, `tools`, …) pass through unfiltered; prompt-length estimate is a word count.
- Floating `python:3.12-slim`, no lockfile hashes, no `pip-audit`/SBOM/scan, CI actions pinned by tag; **CI has never demonstrably run.**

## Action list

**P0 — before non-throwaway traffic**
1. NetworkPolicy/SG so only the gateway reaches engine `:8000` (both boxes).
2. vLLM: set `--api-key` + `UPSTREAM_API_KEY`, add `--ssl-*` flags, move probes to HTTPS, document cert issuance and rotation. QVAC: build the mTLS sidecar (or an OpenSSL engine build) and move the key out of argv.
3. Edge TLS 1.3 + WAF + global rate limit (Cloudflare Tunnel or LB).
4. Run the PRD §4.8 security review on the real deployment path — it hasn't happened for either box.

**P1**
5. `/readyz` → replica indexes only; disable `/docs`, `/redoc`, `/openapi.json`.
6. `securityContext` (numeric non-root UID, read-only rootfs, drop caps), resource limits, gateway manifest.
7. Image digests, hash-locked deps, `pip-audit`/Trivy/SBOM, CI actions pinned by SHA; QVAC: real engine image and a sha256 init-container for the GGUF; vLLM: checksum weights.
8. Make `API_KEY_PEPPER` mandatory.

**P2**
9. Audit failed auths; rotation/retention/shipping for the audit log.
10. Key reload + rotation tooling; atomic, `chmod 600` key store; cap JWT `exp − iat`.
11. Gateway request-rate limiting, mandatory quotas, shared quota store for >1 replica.
12. Run CI once and record the result.

## What has not been validated

mTLS against a live engine on either box; the Phase 2 / external security reviews; CI; wedge detection on a real freeze (it did not fire in testing); the QVAC 24 h soak; the network path from the consuming app to the gateway (README calls it "the single largest unknown"). Benchmarks ran on rented vast.ai boxes with loopback binds and throwaway keys, so they say nothing about production network posture.
