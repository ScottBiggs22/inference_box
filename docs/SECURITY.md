# Security features and behavior — vLLM box (`bkn301-inference`)

**Status:** Current as of 2026-10-15. Based on a read-through of the code and docs; nothing was re-run to produce this document unless marked *measured*.
**Applies to:** this repo — the authenticating gateway (`gateway/`) plus deployment artefacts for vLLM (`deploy/`). vLLM itself is not in the repo, only its launch flags.
**Sister doc:** [`bkn301-qvac-inference/docs/SECURITY.md`](../../bkn301-qvac-inference/docs/SECURITY.md) (QVAC box). **One-page version for DevOps:** [`SECURITY_DEVOPS_SUMMARY.md`](SECURITY_DEVOPS_SUMMARY.md).

Status tags used throughout:

| Tag | Meaning |
|---|---|
| **[IMPLEMENTED]** | In code or a manifest in this repo, with a test or assertion. |
| **[DESIGN ONLY]** | Documented as the plan; nothing built. |
| **[NOT DONE]** | Absent, or the code contradicts a claim. |
| **[UNVERIFIED]** | Claimed or built but never exercised on the real path. |

**The short version.** The gateway is a reasonably hardened authenticating edge, and the gateway-to-vLLM mTLS client is implemented and tested. But the vLLM side of that mTLS is not specified in any deployment artefact, vLLM has been observed listening on `0.0.0.0:8000` with no `--api-key`, and the edge (TLS, WAF, rate limit) is design-only. Network isolation is an assumption, not something the repo enforces.

---

## 1. Trust model

```
caller (C# app) ──TLS 1.3──> edge (Cloudflare Tunnel / LB+WAF)   [DESIGN ONLY]
                              │
                              ▼
                      gateway :8080  (auth, quotas, audit)        [IMPLEMENTED]
                              │  mTLS client                      [IMPLEMENTED, UNVERIFIED on real path]
                              ▼
                    vLLM :8000 (private subnet only)              [isolation NOT ENFORCED]
```

- Rented GPU boxes (vast.ai) are treated as **untrusted** (PRD §4.8, `docs/VAST_AI_RUNBOOK.md:9-35`): synthetic prompts only, no production secrets.
- Redact-before-send (PRD §5.4 item 3) and encrypt-at-rest for persistent stores (items 4–5) are app/platform-side and **[NOT DONE]** here.

## 2. TLS / mTLS

- **[IMPLEMENTED] Gateway → vLLM client side.** `gateway/upstream/client.py:55-78` builds an `ssl.SSLContext` by hand (`create_default_context(cafile=…)` + `load_cert_chain`) because httpx 0.28.1 silently drops `cert=` when `verify=<str>` is passed. One shared `httpx.AsyncClient` carries chat, probes and the vLLM `/metrics` scraper (`client.py:225-238`), so nothing bypasses it. Config: `UPSTREAM_CLIENT_CERT_PATH`, `UPSTREAM_CLIENT_KEY_PATH`, `UPSTREAM_CA_BUNDLE_PATH` (`config.py:135-144`). A half-set cert/key pair raises (`config.py:278-294`). Active only for `https://` upstreams; the default is plain HTTP.
- **Tests** (`tests/test_mtls.py`): no client cert is rejected (l.242), wrong-CA server cert is rejected (248), correct chain succeeds (256), the pool completes the handshake (271), fails closed when unconfigured (291).
- **[UNVERIFIED]** Proven with self-signed certs and a bare `ssl`-wrapped socket — not uvicorn or a real vLLM (`docs/HANDOFF.md:521-585`). PRD §4.8 requires the security review to run on the real path; it has not.
- **[NOT DONE] vLLM server side.** Neither `deploy/k8s/probes.yaml:21-32` nor the runbook command (`docs/VAST_AI_RUNBOOK.md:166-175`) has `--ssl-certfile`, `--ssl-ca-certs` or `--ssl-cert-reqs`. Cert issuance and rotation are undocumented. The probes in `probes.yaml` use plain `httpGet` on port 8000 with no `scheme: HTTPS`, so they would break if vLLM were switched to mTLS.
- **[NOT DONE] Inbound TLS on the gateway.** uvicorn runs plain HTTP (`deploy/Dockerfile.gateway:42`). Client-to-edge TLS 1.3 is **[DESIGN ONLY]** (README diagram; PRD §5.2) — no Cloudflare/LB config exists. There is no inbound mTLS for the C# caller; it authenticates with a JWT or API key.
- **Certificate rotation / expiry monitoring:** not addressed. The client is built once at `pool.start()`, so new certs need a gateway restart.

## 3. Authentication and authorization

### Gateway (`gateway/auth/`)

- **[IMPLEMENTED] Two credential types** on `Authorization: Bearer`, dispatched by shape (`auth/__init__.py:155-196`): three dot-separated segments not starting with `bkn_` is a JWT; otherwise an API key. Missing/empty/non-Bearer gives a generic 401 with `WWW-Authenticate: Bearer`; the reason is never disclosed (`tests/test_auth.py:126`).
- **[IMPLEMENTED] API keys** (`auth/keys.py`): `bkn_live_<12hex keyid>_<token_urlsafe(32)>`. Only `argon2id(secret + pepper)` is stored (argon2id over bcrypt for the 72-byte truncation). The keyid gives a dict lookup, so one hash per request. Expiry, revocation and scopes are enforced. Plaintext is returned once.
- **[IMPLEMENTED] Enumeration resistance:** an unknown keyid still runs a dummy argon2 verify (`keys.py:198-202, 217-224`).
- **[IMPLEMENTED] Argon2 DoS bound:** verify runs in a thread limited by `AUTH_VERIFY_CONCURRENCY=6` (`auth/__init__.py:29-80`). The code comments that this is reachable pre-auth; loop-lag measurements are in `auth/__init__.py:40-47`.
- **[IMPLEMENTED] JWT** (`auth/jwt.py`): asymmetric algorithms only (RS/ES 256/384/512, `jwt.py:39`); a symmetric `JWT_ALGORITHM` raises at construction; decode pins `algorithms=[self.algorithm]`. Required claims `exp, iat, sub, iss, aud`, with `iss`/`aud` verified; 30 s leeway. No `JWT_PUBLIC_KEY` means the JWT path is disabled and rejects. Tests parametrise HS256/384/512 and `none` (`tests/test_auth.py:88-96`).
- **[IMPLEMENTED] Scopes:** only `chat` is checked, on `/v1/chat/completions` (`chat.py:91-92`). `/v1/models` needs a valid principal but no scope. Per-model scoping (PRD §5.3) is **[NOT DONE]**.

### Caveats

- **Pepper is optional.** `API_KEY_PEPPER` unset only logs a startup warning (`main.py:117-124`); a deployment can run unpeppered.
- **"JWT lifetime ≤ 5 min" is advice to the minter,** not enforced: the gateway checks `exp`/`iat` but does not cap `exp − iat`. No JWT revocation, by design.
- **Revocation needs a restart.** `KeyStore.reload()` is called only in `__init__` (`keys.py:162`); `get_key_store()` is a singleton. No hot reload, admin endpoint or SIGHUP.
- **Timing leak:** revoked/expired keys return before the argon2 hash runs (`keys.py:204-207`), so latency distinguishes "valid keyid, revoked/expired" from "wrong secret" — the oracle the docstring says is avoided (`keys.py:191-194`).
- **Key store file perms:** `save()` is a plain `write_text` with no `chmod 600` (`keys.py:173-176`). Compose mounts it `:ro`.
- **No key-management CLI** (inline `python -c` in the README, or `scripts/dev_key.sh`). Rotation with overlap window (PRD §5.3) has no tooling.

### vLLM upstream auth

- `UPSTREAM_API_KEY` is sent as `Authorization: Bearer` on every upstream call (`client.py:222-224`). It is empty by default (`.env.example:23`), and nothing warns when it is empty.
- **[UNVERIFIED] Whether any real deployment sets `--api-key`.** It is absent from the non-default args in both measured vLLM boot logs (`results/phase1-vastai-a10/vllm-boot-cold.log:7`), from the runbook command, and from `probes.yaml`. Nothing in the repo sets it.

## 4. Network exposure

| Item | Value | Source |
|---|---|---|
| Gateway bind | `0.0.0.0:8080` (deliberate inside the container) | `config.py:96-105`, `.env.example:9-13` |
| Image CMD | `--host 0.0.0.0 --port 8080 --workers 1` (so `GATEWAY_HOST/PORT` have no in-container effect) | `Dockerfile.gateway:32,42` |
| Compose gateway | `127.0.0.1:8080:8080` only | `docker-compose.yml:31-34` |
| Compose stub | `expose: 8001`, not published | `docker-compose.yml:20-21` |
| Rented box | gateway on `127.0.0.1`, reach via SSH tunnel | `VAST_AI_RUNBOOK.md:277-281` |
| vLLM | observed `http://0.0.0.0:8000`, no `--host` set anywhere | `vllm-boot-cold.log:52`, `phase2-l4-wedge-argon2/vllm-boot.log:52` |
| CORS | none, by design | `main.py:155-158` |

- **[DESIGN ONLY]** vLLM "no public IP, private subnet only, reachable solely by the gateway" (README; PRD §5.2:850-851). Edge is Cloudflare Tunnel or LB+WAF with no inbound ports on the GPU host (PRD:843-845). No manifest or IaC enforces either.
- **[NOT DONE]** No firewall, NSG, security list or NetworkPolicy artefact anywhere (grep for `NetworkPolicy`, `securityContext`, `iptables`, `ufw`, `firewall` over `deploy/` and `docs/` found nothing). README says to restrict `/metrics` with a network policy, but none is provided. There is no gateway k8s manifest.
- **Documentation inconsistency:** the runbook booking table says "expose one TCP port" (`VAST_AI_RUNBOOK.md:69`) while the operations doc says no published ports (`VAST_AI_OPERATIONS.md:157-158`).
- Protection of vLLM therefore depends entirely on the unverified network placement.

## 5. Container and privilege hardening

**There are no systemd units in this repo.** The only process managers are the container and a manual `nohup`-style launch on the rented box (`VAST_AI_OPERATIONS.md:276-288`).

- **[IMPLEMENTED] Non-root gateway:** `useradd --system --no-create-home --shell /usr/sbin/nologin gateway`, `USER gateway`; `/app/logs` is the only writable surface (`Dockerfile.gateway:22,29-30`). CI asserts uid ≠ 0 (`ci.yml:47-50`).
- **[IMPLEMENTED] Multi-stage build:** no compiler in the runtime image; CI asserts no `gcc`/`cc`/`make` (`ci.yml:42-46`).
- **[IMPLEMENTED] Stub isolation:** excluded from the package (`pyproject.toml:23-41`) and by a `.dockerignore` allowlist; the final stage is pinned to `FROM runtime AS default` so a plain `docker build` ships the gateway; CI asserts the stub is not importable.
- **[IMPLEMENTED]** No cloud SDK in the image (`tests/test_config.py:129`); single worker; loopback `HEALTHCHECK` on `/healthz`.
- **[NOT DONE]** No compose `read_only`, `cap_drop`, `no-new-privileges`, tmpfs or limits; no k8s `securityContext`, `readOnlyRootFilesystem`, `runAsNonRoot`, seccomp; no gateway manifest. No explicit numeric UID in the Dockerfile.
- **vLLM container** is stock `vllm/vllm-openai:v0.28.0` (`probes.yaml:20`); the runbook runs it as `root` over SSH. Its user, filesystem and caps are unspecified; `probes.yaml:33-42` sets only GPU limits.

## 6. Secrets handling

- **[IMPLEMENTED] Env vars only,** no cloud SDK or managed identity (`config.py:3-21`). Secrets: `UPSTREAM_API_KEY`, `API_KEY_PEPPER`, `JWT_PUBLIC_KEY` (public). mTLS material is by path so it can be a mounted Secret.
- **[IMPLEMENTED] Ignore rules:** `.gitignore` covers `.env*`, `keys.json`, `deploy/local-keys.json`, `logs/`, `*.jsonl`; `.dockerignore` excludes `keys.json` and `.env`. `results/**` is deliberately re-included (committed evidence, e.g. `audit.jsonl` with throwaway keyid `223811148d7a`).
- **[IMPLEMENTED] Dev-only literal pepper** `local-dev-pepper-not-a-secret` in `docker-compose.yml:39-40`.
- **[IMPLEMENTED] Pre-transfer hygiene for untrusted hosts:** `scripts/pack_for_box.sh` builds the tarball from a throwaway index, applies a deny-list (`.env*`, `*keys.json`, `*api_key*`, logs, data, knowledge, models, `*.gguf`, `*.faiss*`, `*.jsonl`), runs a shape-aware credential regex (`sk-ant-…{20,}|bkn_live_…{12}_…{30,}`), refuses to run inside `baas-poc-templates`, and prints the manifest. `scripts/verify_box.sh` has a matching `hygiene_scan` with `--self-test`/`--scan-only`. Tests: `test_pack_for_box.py`, `test_verify_box.py`.
- **vast.ai instance-scoped credentials:** `/root/.vast_api_key` is host-injected and tolerated by `verify_box.sh` as a WARN. **[UNVERIFIED]** that it differs from the account key — a hash comparison done manually once on 2026-09-10. The `instance_api_key` printed by `vastai create` is to be treated as a secret; SSH keys are throwaway ed25519.
- **[NOT DONE]** Secret scanning in CI (the regexes live only in the two box scripts; no gitleaks/trufflehog). The pack-script regex misses `bkn_test_` keys, JWT private keys and PEMs. No k8s secret-injection guidance.

## 7. Model and image integrity

- **[IMPLEMENTED] Model revision pinned by commit hash:** `--revision=4da05a8edb55c6046cce958586c33b61da07bb79` (`probes.yaml:23`, runbook:168), present in the real boot log args. The OCI model card must use the same hash.
- **[IMPLEMENTED, manual] Safetensors only, no pickle** — verified by inspection on 2026-09-09 against the 12-file repo listing. No automated check.
- **[IMPLEMENTED] vLLM image tag pinned** to `v0.28.0`; flags already drifted once (`--disable-log-requests` removed), so re-check `--help` per tag.
- **[IMPLEMENTED] Python deps pinned with exact `==`** (`pyproject.toml:49-80`).
- **[NOT DONE] No checksum/signature verification of weights.** PRD §5.4 item 5 ("verify checksums at image build, scan images, produce an SBOM") is not implemented. Weights are pulled from the HF hub at pod start (~6 GB, 153 s cold). A commit-hash revision is content-addressed at the git layer, but the repo doesn't verify it. No `HF_HUB_OFFLINE`/mirror and no guidance on `--trust-remote-code`.
- **[NOT DONE]** No digest pinning (`python:3.12-slim` is a floating tag for both stages; vLLM uses a tag, not `@sha256`); no hash-locked/transitive lockfile (`pip install .` resolves transitives freely); no `pip-audit`, SBOM, Trivy or Dependabot; CI actions pinned by tag only.
- `deploy/k8s/probes.yaml` is a Deployment fragment: no Service, no Secret refs, no HF cache `volumeMounts`.
- **[UNVERIFIED] CI has never demonstrably run** (`pyproject.toml:30-34` says no remote existed; `HANDOFF.md:48-51` says one does now, with no evidence of a run).

## 8. Logging, audit and redaction

- **[IMPLEMENTED] Metadata-only audit log** (`gateway/audit.py:44-67`): timestamp, event, keyid, subject, auth_method, model, status, latency_ms, token counts, upstream, reason (closed vocabulary), streamed. Thread-locked append-only JSONL; a write failure logs loudly but doesn't fail the request. `AUDIT_INCLUDE_PROMPT_TEXT` defaults false and isn't wired to write anything (setting it only logs an error). Tests: `tests/test_audit_no_prompt_text.py` (canary, dataclass fields, flag default).
- **[IMPLEMENTED] vLLM no-retention flags:** `--no-enable-log-requests --no-enable-log-outputs` (`probes.yaml:31-32`, runbook:171-172). These are the 0.28.0 defaults; they're explicit to survive a future default change.
- **[IMPLEMENTED]** Closed vocabularies for rejection reasons and metric labels; unknown model strings fold to `other`; chat code never logs bodies; stream failures become a generic SSE error frame.
- **[NOT DONE] Failed authentications are not audited.** `gateway/metrics.py:238-241` acknowledges this; `keys.py:193` still claims otherwise. Neither are 404s or 5xx on other routes.
- **[NOT DONE]** Audit-log integrity, rotation, retention (open question, PRD Q9), access control, or SIEM shipping. `JWT sub` is written unsanitised to `subject` (JSON-encoded, but unbounded).
- Request bodies are never logged, but are sent to the model host in plaintext as long as the §2 gaps stand.

## 9. Health, metrics and the unauthenticated surface

Unauthenticated gateway endpoints: `/healthz`, `/readyz`, `/metrics`, and FastAPI's `/docs`, `/redoc`, `/openapi.json`.

- **`/healthz`** — liveness only (`routes/health.py:24-27`).
- **`/readyz`** — **leaks private upstream URLs.** It returns `"upstreams"` and `"breakers"` keyed by raw replica URL (`health.py:53-57`, `client.py:414-432`, `breaker.py:252-253`), contradicting the policy of labelling replicas by index because these endpoints are unauthenticated (`client.py:318-328`, `metrics.py:25-26`). `tests/test_breaker_routing.py:157,173` assert on this shape. *(Confirmed by reading the code; not exercised at runtime.)*
- **`/metrics`** — unauthenticated on the main port by design. Labels are bounded: no `keyid`, no URLs, `model` folded to the allowlist or `other`, route templates. `METRICS_ENABLED=false` returns 404. Needs a network policy that doesn't exist.
- **`/docs`, `/redoc`, `/openapi.json`** — `create_app()` passes no `docs_url=None` (`main.py:146-154`); the schema is public. Not mentioned in any doc.
- **[UNVERIFIED] Wedge detection:** the signal (`num_requests_waiting > 0` with `generation_tokens_total` flat) did **not** fire on a real frozen engine (`HANDOFF.md:610-622`, `docs/PHASE2_L4_RESULTS.md`). `UPSTREAM_WEDGE_WINDOW_SEC=120` is an estimate.

## 10. Rate limiting and input limits

**Quotas (per API key,** `gateway/quota.py`**):** cumulative token budget (an in-flight request still finishes) and a concurrency cap (held until the stream ends). **In-memory, per-process** (reset on restart), **unlimited by default** unless set at mint time, and **JWT principals are exempt by design.** The usage frame is forced via `stream_options.include_usage` unless the caller sets it false.

**Not done:** request-rate limiting (no per-IP/key/subject RPS or token bucket). The PRD assigns the global rate limit to the edge (design-only) and lists it as S8 for the app (PRD:1043).

| Limit | Value | Behavior |
|---|---|---|
| Body size | `MAX_BODY_BYTES` = 1 MiB | 413, but `await request.body()` buffers everything **before** the check (`chat.py:76-79`); no `Content-Length` pre-check. The 413 path isn't cleanly tested (`test_auth.py:165` accepts 400 or 413) |
| Completion tokens | 2048 | clamped; non-int/≤0 → 400 `max_tokens_invalid` |
| Prompt tokens | 7000 | whitespace word-count estimate, errs low by design; won't catch CJK/Arabic (the app is bilingual, PRD Q8) or token-dense input. vLLM `--max-model-len` is the real limit |
| Model | allowlist | 400 |
| `messages` | non-empty list | 400 |
| Upstream | 5 s connect / 120 s read | 2 attempts with jitter on 429/502/503/504; per-replica circuit breaker; fast 503 with `Retry-After` when all replicas are open |

Other gaps:
- **Request fields are not allowlisted.** Only `max_tokens` and `stream_options` are modified; `n`, `best_of`, `logprobs`, `tools`, `chat_template_kwargs`, `stop` etc. pass through. `n` could multiply decode cost against per-key accounting. (`chat_template_kwargs` pass-through is relied on by the app.)
- 429 responses carry no `Retry-After`.
- No per-request total-duration cap beyond the 120 s upstream read timeout (the PRD lists a per-request timeout).
- Upstream 4xx/5xx bodies are drained (≤64 KiB) and discarded; callers see only `"upstream error"`.

## 11. Open items for DevOps (prioritised)

**P0 — before any non-throwaway traffic**
1. Set `--api-key` (and `UPSTREAM_API_KEY`) on vLLM, bind it appropriately, and enforce isolation with a NetworkPolicy/SG. Nothing does today.
2. Specify vLLM-side mTLS (`--ssl-certfile`, `--ssl-ca-certs`, `--ssl-cert-reqs`), fix the probes for HTTPS, document cert issuance/rotation, and run the PRD §4.8 security review on the real path.
3. Provide edge TLS 1.3, WAF and a global rate limit.

**P1**
4. Make `/readyz` return replica indexes, not URLs; disable `/docs`, `/redoc`, `/openapi.json`.
5. Add `securityContext`, resource limits and a gateway manifest; numeric UID in the Dockerfile.
6. Checksum weights in the deploy path; pin images by digest; lockfile with hashes; `pip-audit`/Trivy/SBOM; pin CI actions by SHA.
7. Require `API_KEY_PEPPER` (fail, not warn).

**P2**
8. Audit failed auths; log rotation/retention/shipping.
9. Key reload and rotation tooling; `chmod 600`; fix the revoked/expired-key timing difference.
10. Gateway request-rate limiting; mandatory per-key quotas; shared quota store for multiple replicas; cap `exp − iat` on JWTs.
11. Streaming body-size limit; allowlist forwarded request fields.
12. Get CI to run once and record the result.

**Never validated:** mTLS against a real vLLM/private CA; the Phase 2 and external security reviews; CI; wedge detection on a real freeze; Azure staging SKU (full 24 GiB card); the vast.ai instance-key vs account-key check beyond the one manual comparison. Phase 1 results used loopback binds and throwaway keys, so they say nothing about production network posture.
