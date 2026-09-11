# Health probe contract — vLLM and the inference gateway

**Status:** proposed, for DevOps review
**Date:** 2026-09-09
**Owner:** scott.biggs@bkn301.com
**Source:** `INFERENCE_SERVICE_PRD.md` §4.6, extracted so it can be reviewed on its own
**Applies to:** `deploy/k8s/probes.yaml`

---

## 1. Why this needs its own document

DevOps asked for the probe internals and was right that they are not standard:

> "the way LLM inferencing works is different from general k8s semantics."

Two properties of an LLM server break the usual defaults, and they break them in
opposite directions:

| Property | What it breaks |
|---|---|
| Model load takes **30–60s** | A liveness probe with normal timings kills the pod before it ever serves |
| A legitimate generation takes **10s+** under load | A readiness or liveness probe tuned for a web app marks a *healthy, working* pod as failed |

Getting either wrong is expensive rather than merely noisy: a restart costs
another full model reload, and a restart loop costs the service.

---

## 2. The division of labour

| Probe | Endpoint | Checks | Timings | Why |
|---|---|---|---|---|
| `startupProbe` | vLLM `/health` | Engine initialised | `periodSeconds: 5`, `failureThreshold: 60` → **300s budget** | vLLM returns 200 here only once the engine is up. **While this runs, Kubernetes suppresses the other two** — which is what makes them safe to tune tightly. **Measured 2026-09-10, see §5.** |
| `readinessProbe` | `/health` | Engine health **only** | `periodSeconds: 10`, `failureThreshold: 2` | Answers "is this instance broken", never "is it busy". See §3. |
| `livenessProbe` | `/health` | Engine loop alive. **Never a test generation.** | `periodSeconds: 20`, `timeoutSeconds: 5`, `failureThreshold: 3` → ~60s | Restart costs 30–60s of reload, so this is the last resort. |

The gateway exposes the same split on its own process: `/healthz` for liveness,
`/readyz` for readiness. `/readyz` goes red only when **no** upstream replica is
reachable — one replica down is what having replicas is for, and belongs in an
alert rather than in this endpoint's verdict.

---

## 3. Do not put queue depth in the readiness probe

This is the single most important line in the document, because the wrong design
is the intuitive one.

If `/readyz` goes red when an instance is **busy**, then under a traffic spike
every replica reports not-ready at once, Kubernetes removes all of them from the
endpoint list, and the service fails **precisely at peak load** — the moment it
exists to handle. The failure is self-reinforcing: removing replicas concentrates
load on whatever is left, which makes them report busy too.

Readiness answers *"is this instance broken"*. Load-aware routing belongs in the
gateway, which already tracks in-flight requests per replica and can shed or
queue without removing anything from service discovery.

---

## 4. Detecting a genuinely wedged model

A wedged engine does not fail an HTTP probe — the process is alive and the
socket answers. The signature is a **relationship between two metrics over
time**, which is why no single endpoint can express it:

> `vllm:num_requests_waiting > 0` **and** `vllm:generation_tokens_total` has not
> advanced for N seconds.

Relevant series from vLLM's `/metrics`:

| Metric | Use |
|---|---|
| `vllm:num_requests_waiting` | queued but not started |
| `vllm:num_requests_running` | in flight |
| `vllm:generation_tokens_total` | a counter — **flat while `waiting > 0` is the wedge** |
| `vllm:time_to_first_token_seconds` | the TTFT alert in PRD §7 Phase 4 |

Two mechanisms, with different roles — we recommend both:

1. **Gateway circuit breaker (primary).** The gateway sees the stall first, stops
   routing to that replica, and alerts. No restart, no dropped work, human in the
   loop.
2. **Sidecar backstop (automated).** Scrapes `/metrics` and fails `/healthz` if
   the signature holds for a **conservative** window — 120s, not 20s. This is
   what finally trips the `livenessProbe`.

The weighting is deliberate: **an automatic restart on a false positive is worse
than a late alert**, because it costs a minute of reload and can loop. Start with
the breaker and a generous sidecar window, then tighten once real failure data
exists.

---

## 5. Which numbers are measured, and which are still estimates

Phase 1 (2026-09-10, full-card A10) measured one of the two and **not** the
other. The distinction is load-bearing, so it is tabulated rather than described:

| Number | Status | Value |
|---|---|---|
| Cold-start budget → `startupProbe.failureThreshold` | **MEASURED** | 45s warm, **153s cold** → `failureThreshold: 60` (300s) |
| Liveness/readiness timings | derived from the above | unchanged |
| **Wedge-detection window** → the sidecar's patience | **STILL AN ESTIMATE** | 120s, from §4, never validated |

**The cold-start figure corrects this document in the dangerous direction.** It
previously specified `failureThreshold: 24` — a 120s budget against a 153s
measured cold start. Kubernetes would have killed the pod at 120s, and the
replacement would also have started cold: a boot loop, not a slow start.
`deploy/k8s/probes.yaml` now carries 60, about 2× the measurement. Generous on
purpose — a loose startup probe costs a slower failure verdict, a tight one costs
a loop that never converges.

**The wedge window was not measured and is still a guess.** PRD §7 Phase 1 listed
it as a deliverable; the engine never wedged naturally during the session, and
inducing one was not attempted. Nothing in the gateway is designed to be correct
only if 120 is the right number: the breaker requires **two consecutive**
confirmations before acting on the signal, and alerts on the first. The gateway
now exports `bkn301_gateway_upstream_wedge_stall_seconds` **continuously**,
including well below the threshold, specifically so that the first real incident
replaces this estimate with a measurement.

---

## 6. Constraints that sit alongside the probes

From PRD §4.5, repeated here because they belong to whoever writes the manifest:

1. **One whole GPU per pod** — `nvidia.com/gpu: 1`, never a fraction. vLLM
   pre-allocates its KV cache at startup, so a second vLLM pod on the same card
   **fails at boot** rather than degrading.
2. **The A10 does not support MIG** (A100/A30/H100 only). Partitioning means
   time-slicing or MPS, neither of which gives memory isolation.
3. **The card must be a full 24 GiB A10, not a vGPU partition.** Azure's
   `NVadsA10_v5` sizes start at one-sixth of a card with a 4 GiB frame buffer. A
   fractional size boots, serves, and silently breaks the 8k context
   requirement — see PRD §4.4(a), which is the highest-priority open question.
4. **Nothing else may share the card.**
5. **No tensor parallelism.** An 8B AWQ model fits one card, and A10s have no
   NVLink.
