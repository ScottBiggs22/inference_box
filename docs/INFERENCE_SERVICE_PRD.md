# Secure Cloud LLM Inference Endpoint — PRD

**Status:** revised against DevOps response
**Date:** 2026-09-08 (rev 3 — rebased onto `main` @ `afbf0c5`; findings re-verified §6.2; rented dev box §4.8)
**Owner:** scott.biggs@bkn301.com
**Target repo:** new — `bkn301-inference` (this document moves with it)
**Consuming repo:** `baas-poc-templates` (`services/bkn301-ai-service`, as a new `LLM_BACKEND=vllm` option)

---

## 1. Summary

Stand up a self-hosted, OpenAI-compatible inference endpoint serving Qwen3-8B on
NVIDIA A10 hardware, fronted by an authenticating gateway, and consume it from
the existing FirstData AI service as a third backend beside `llamacpp` and
`qvac`.

Two decisions in the proposed stack change as a result of the analysis in §3:

- **GGUF is dropped for the server path.** It works for Qwen3 (vLLM's own docs
  use a Qwen3 GGUF as the canonical example), but it is explicitly experimental,
  under-optimised, out-of-tree, and documented as possibly incompatible with
  other features — which is exactly what stacking DFlare and LMCache on top
  requires. AWQ INT4 is faster, in-tree, and officially published by Qwen.
  GGUF stays as the **laptop** backend via the existing llama.cpp path.
- **DFlare moves from Phase 1 to a Phase 5 benchmark-gated experiment.** It does
  not fit the context-window requirement on a 24GB A10 alongside a BF16 target
  (§3.3), and the throughput it buys is throughput the simpler AWQ config
  already delivers.

Net: **Qwen3-8B-AWQ on vLLM, one whole GPU per replica, behind a custom gateway.**
This meets 20 tok/s/user at 5 concurrent with ~2.4× headroom and clears the 8k
context goal with ~2.6× KV headroom.

### Scope boundary

This document covers **the single-card inference service and the gateway in front
of it**. Multi-GPU orchestration, autoscaling, and Kubernetes are DevOps's. An 8B
model does not justify sharding, so nothing here shards. DevOps has confirmed a
Kubernetes-based vLLM deployment and independently proposed the same architecture
we designed — `FIRSTDATA PLATFORM → Inference Gateway → vLLM` — so the gateway
is agreed rather than something we have to argue for.

What we owe DevOps is a **deployment contract** (§4): a container image, an
env-var config surface, health and metrics endpoints, and a list of constraints
the orchestrator must respect.

**One thing got materially harder.** Staging is planned for **Azure**, production
cloud is undecided, and the OCI environment is available to us as a testing env.
That makes cloud portability a hard requirement rather than good hygiene, and it
introduces a real risk that Azure's partitioned A10 sizes silently break the
memory budget. See **§4.4** — it is the most important section in this revision.

---

## 2. Requirements

| # | Requirement | Source | Verdict |
|---|---|---|---|
| R1 | ≥20 output tok/s per user | given | Met, ~48 tok/s/user at 5 concurrent (AWQ) |
| R2 | 3–5 concurrent requests | given | Met by the GPU; requires N AI-service replicas — §6.1 B1 |
| R3 | 50 user accounts total | given | Trivial; concurrency is the real limit |
| R4 | 4k context minimum, 8k+ goal | given | Met (AWQ: ~105k tokens of KV budget) |
| R5 | Secure key-authenticated API | given | Gateway, §5 |
| R6 | Clean API + dockerised | given | OpenAI-compatible + Compose/Helm |
| R7 | 4 GPUs: 1 dev / 1 staging / 2 prod | given | Confirmed as `VM.GPU.A10.1`; counts to be set by measured TPS — §4.1 |
| R8 | Future dynamic allocation via K8s | given | K8s confirmed; constraints handed over in §4.5 |
| R9 | All development on an M5 MacBook Air | given | Dev box arriving; see §4.7 |
| R10 | **Portable across OCI and Azure** | *new, §4.4* | Env-var config only, no cloud SDK in the image |

---

## 3. Stack feasibility

### 3.1 Verified model facts

Qwen3-8B: 8.19B params, 36 layers, 32 query heads, **8 KV heads**, head_dim 128,
~~vocab 151,552, **32,768 native context** (131,072 with YaRN)~~.

> **Verified against the real `config.json` 2026-09-09.** The geometry the
> memory budget depends on is exactly right — 36 layers, 32 query heads, 8 KV
> heads, head_dim 128 — and re-deriving §3.3's KV cost from it reproduces
> 144 KiB/token and ~107,770 tokens for a 14.8 GiB budget. **Two of the
> secondary figures were wrong:**
>
> | Field | This document said | `Qwen/Qwen3-8B-AWQ` config.json |
> |---|---|---|
> | `vocab_size` | 151,552 | **151,936** |
> | `max_position_embeddings` | 32,768 | **40,960** |
>
> The vocabulary figure has three candidates in circulation and they measure
> different things, which is why it kept coming out differently: **151,936** is
> the model's embedding-matrix width (padded), **151,643** is the tokenizer's
> actual token count (§6.1 B4's measurement), and **151,552** matches neither
> — it is a Qwen2.5-era number that travelled into this document. Nothing here
> depends on it; the correction exists so nobody re-derives from it.
>
> The context figure does matter in one place: §7 Phase 5 says "Qwen3 needs
> YaRN above 32k". The native window in this release is **40,960**, so YaRN is
> only needed beyond that.

KV cache cost per token, FP16/BF16:

```
2 (K,V) × 36 layers × 8 kv_heads × 128 head_dim × 2 bytes = 147,456 B
                                                          = 144 KiB / token
```

So one 8k-context sequence costs **1.125 GiB** of KV cache, and 4k costs 576 MiB.

> **Correction to carry into the app repo:** ~~`config.py:36-38` documents KV cost
> as "36 layers x 2 KV heads x 128 head dim x 2 bytes" ≈ 36 KB/token. That is
> Qwen2.5-1.5B's geometry (2 KV heads), which is the model in the repo today.
> Qwen3-8B has 8 KV heads — **4× the per-token cost**. Anyone sizing a context
> window from that comment will be off by 4×.~~
> **Done 2026-09-09.** `config.py`'s `LLM_CTX_SIZE` comment now gives both
> geometries side by side, names the model each belongs to, and notes that the
> setting is advisory under the vllm backend since `--max-model-len` is
> server-side (§6.3 C5).

### 3.2 A10 constraints

- 24 GB GDDR6, ~600 GB/s bandwidth, Ampere (SM86).
- **No FP8 hardware support.** FP8 needs Hopper or Ada. On A10 the quantisation
  options are AWQ / GPTQ INT4 (Marlin kernels) or INT8 W8A8.
- **No MIG.** MIG is A100/A30/H100 only. See §4.5.
- Decode is memory-bandwidth-bound, so weight footprint sets the ceiling:
  BF16 reads 16.4 GB per decode step (~36 tok/s theoretical), AWQ INT4 reads
  ~5.6 GB (~107 tok/s theoretical).

### 3.3 Configuration comparison

Budget assumes `--gpu-memory-utilization 0.90` → 21.6 GiB usable of 24 GB.

> **Every figure below assumes a whole 24 GiB A10.** This is safe on OCI, where
> `VM.GPU.A10.1` passes a full card through. It is **not** safe on Azure, whose
> `NVadsA10_v5` sizes are SR-IOV partitions from 1/6 of a card upward. On a half
> card the AWQ config has ~4 GiB of KV cache — 5 × 4k works, 5 × 8k does not. See
> §4.4(a).

> **MEASURED 2026-09-10 — the config-B column is 12% optimistic, and the
> conclusion still holds.** Full details in `docs/PHASE1_RESULTS.md` §3.
>
> | Quantity | This table says | Measured on a full-card A10 |
> |---|---|---|
> | Usable at 0.90 util | 21.6 GiB | **19.85 GiB** |
> | KV budget | 14.8 GiB | **12.71 GiB** |
> | **Total tokens** | **~105,000** | **92,544** |
> | 5 × 8k headroom | 2.6× | **2.26× — still met** |
>
> Two causes. The larger is arithmetic: **`0.90 × 24 GB` is the wrong basis**,
> because the card addresses **22.06 GiB**, not 24 — the remainder is driver and
> ECC overhead. The second is that CUDA graphs (0.62 GiB) were folded into the
> "Activations / CUDA graphs" row rather than budgeted on top of peak activation.
>
> **The KV-per-token row is confirmed exactly**: 92,544 × 144 KiB = 12.71 GiB,
> matching vLLM's own report to the digit. §3.1's geometry is sound; one input
> to the budget was not.
>
> Also measured: **KV cache size is not a constant.** A boot with a cold
> `torch.compile` cache profiles 1.0 GiB more peak activation and permanently
> loses 7,312 tokens of KV (92,544 vs 99,856 warm). Capacity planning must use
> the cold figure unless the compile cache is baked into the image — which is
> now a recommendation to DevOps, worth both the KV and ~108s of startup.

| | A: BF16 | **B: AWQ INT4 (chosen)** | C: BF16 + DFlare (proposed) | D: AWQ + DFlare |
|---|---|---|---|---|
| Target weights | 15.3 GiB | **5.6 GiB** | 15.3 GiB | 5.6 GiB |
| Draft weights (1.4B BF16) | — | — | 2.6 GiB | 2.6 GiB |
| Activations / CUDA graphs | 1.2 GiB | **1.2 GiB** | 1.6 GiB | 1.6 GiB |
| **KV budget** | 5.1 GiB | **14.8 GiB** | 2.1 GiB | 11.8 GiB |
| KV per token | 144 KiB | **144 KiB** | ~172 KiB (+draft) | ~172 KiB |
| **Total tokens** | ~36,300 | **~105,000** | **~12,800** | ~70,000 |
| 5 × 8k = 40k tokens? | ✗ marginal | **✓ 2.6× headroom** | **✗ 2.5k each** | ✓ |
| 5 × 4k = 20k tokens? | ✓ | **✓** | ✗ | ✓ |
| Decode speed/user @5 | ~25 tok/s | **~48 tok/s** | fast but no context | fastest |
| Integration risk | in-tree | **in-tree** | out-of-tree | out-of-tree, unvalidated |

**Config C — the proposed stack — fails R4.** A BF16 target plus a 2.6 GiB draft
model leaves ~2.1 GiB of KV cache, about 12.8k tokens total. Split across 5
concurrent users that is ~2.5k each, below the 4k floor, and 8k is unreachable
even at 3 concurrent once the RAG context block is included.

**Config B is the recommendation.** `Qwen/Qwen3-8B-AWQ` is officially published,
loads in-tree, and trades a modest quality delta for 3× the KV budget and ~2×
the decode speed.

### 3.4 DFlare — what it actually is, and why it waits

`AngelSlim/Qwen3-8b-dflare` is a **draft model for speculative decoding**, not a
quantisation. 1.4B params, 7 layers, block size 16, layer-wise fusion attending
to target layers `[1,5,9,13,17,21,25,29,33]`. Pairs **exclusively with
`Qwen/Qwen3-8B`**. Apache 2.0. Output is identical to greedy decoding because
the target verifies every block, so it is lossless. Claimed **5.46× average
wall-clock speedup** across six math/code/conversation benchmarks.

Three reasons it is not Phase 1:

1. **It costs the resource R4 needs.** 2.6 GiB of weights is 18k tokens of KV
   cache — more than half the BF16 KV budget, spent to accelerate a target
   already fast enough.
2. **The 5.46× is a batch-1 number.** Speculative decoding's advantage decays as
   batch size rises: at batch 1 the verify compute is nearly free because the GPU
   is bandwidth-starved, but at 3–5 concurrent requests continuous batching has
   already recovered much of that slack, and the extra verify FLOPs start being
   charged. Expect a fraction of the headline figure in this workload.
3. **It reads the target's internal hidden states.** Layer-wise fusion is trained
   against the BF16 target's activations. Pairing it with an AWQ target (config D)
   changes those activations; correctness is preserved (the target still verifies)
   but the *acceptance rate* — the entire source of the speedup — is unvalidated.

It is a genuinely strong technique and worth revisiting once the baseline is
measured — hence Phase 5, gated on a benchmark, not on the vendor's number.

### 3.5 GGUF on vLLM — coverage confirmed, still not the right choice

Coverage is real: GGUF support moved to the out-of-tree
[`vllm-gguf-plugin`](https://github.com/vllm-project/vllm-gguf-plugin), and
vLLM's own documentation uses `unsloth/Qwen3-0.6B-GGUF:Q4_K_M` as its worked
example, so the Qwen3 architecture maps. `Qwen/Qwen3-8B-GGUF` ships single-file
Q4_K_M (5.03 GB) through Q8_0 (8.71 GB), satisfying the single-file constraint.

The documented caveats are what rule it out for the server:

- *"GGUF support in vLLM is highly experimental and under-optimized at the
  moment, it might be incompatible with other features."*
- Tokenizer conversion from GGUF is *"time-consuming and unstable, especially for
  some models with large vocab size"* — Qwen3's vocab is ~~151,552~~ **151,936**
  (see the §3.1 correction; the argument is unaffected). Mitigable with
  `--tokenizer Qwen/Qwen3-8B`, which the docs recommend, but it is a workaround.
- GGUF's dequantisation kernels are not the tuned Marlin INT4 path AWQ uses on
  Ampere, so it is slower than AWQ at a similar footprint.

"Might be incompatible with other features" is the decisive line, because the
plan's whole value depends on combining it with speculative decoding and
LMCache. GGUF exists to make llama.cpp work well on CPUs and Metal — which is
precisely its job in this project, on the MacBook (§4.4). It has no advantage on
a CUDA GPU running vLLM.

### 3.6 LMCache — defer

vLLM v1 has **automatic prefix caching on by default**. The system prompt in
`app/api/chat.py:187` plus `build_p0_prose()` plus the retrieved knowledge block
is large and byte-identical across requests, so GPU-resident prefix caching
already covers the dominant reuse case for free. At 50 accounts and 3–5
concurrent, that cache will essentially always hit.

LMCache adds CPU/disk tiering, cross-instance sharing, and CacheBlend
(non-prefix reuse). The plausible win here is the document-extraction path
(`intelligent_extractor`, ~880-token prompts × 4 chunks), but it is speculative
until measured.

It also introduces a **security surface**: the disk backend writes KV tensors to
local storage, and KV tensors are a recoverable representation of user prompt
content. On a platform handling investor and KYC data that is a new
data-at-rest category with no encryption by default. If LMCache is adopted, it
must be CPU-tier-only or on an encrypted volume, and it must appear in the data
retention register.

**Decision:** measure prefix-cache hit rate in Phase 2. Adopt LMCache in Phase 5
only if TTFT is demonstrably prefill-bound and prefix caching is missing.

> **Measured 2026-09-10, ahead of schedule — and the deferral holds.**
> `enable_prefix_caching=True` is confirmed on by default in the V1 engine
> without passing the flag, as this section assumed. On FirstData's traffic
> shape (a large byte-identical system-prompt-plus-knowledge prefix and a short
> unique query) the hit rate was **98.5%** — 730,928 of 742,322 prompt tokens
> served from cache, only 1.5% actually computed.
>
> The condition above is half-satisfied and that is the interesting part.
> Forcing a 0% hit rate (`loadgen.py --unique-prefix`) shows TTFT **is**
> emphatically prefill-bound when the cache misses: 92 ms → **1,405 ms** at
> batch 1, and 132 ms → **3,788 ms** at 5 concurrent, with per-user throughput
> falling 43% as prefill competes with decode. But prefix caching is not
> *missing* — it absorbs 98.5% of the load — so LMCache would only address the
> residual 1.5%. **Deferred, now for a measured reason rather than an assumed
> one.** Full figures in `PHASE1_RESULTS.md` §5.

---

## 4. Deployment contract

DevOps has answered the provisioning questions (2026-09-04). This section is
rewritten around what they confirmed. **Three answers changed the plan and two
opened new risks.**

### 4.1 What is now settled

| Item | Answer | Effect |
|---|---|---|
| GPU quota | **Approved by Oracle** | Q2 closed. No longer the long-lead risk. |
| Shape | **`VM.GPU.A10.1`** — not bare metal | Changes §4.2 |
| Counts | 1 dev / 1 staging / 2 prod is the **baseline**, final counts set by measured TPS | **Our Phase 1 benchmark is now a procurement input, not just a validation step** |
| Access | VPN exists; a VM is provisioned **next week** and shared with us as the dev box | Q7 effectively closed |
| Final runtime | **Kubernetes-based vLLM deployment** | Confirms §4.5 |
| Architecture | DevOps independently proposed `FIRSTDATA PLATFORM → Inference Gateway → vLLM` | **The gateway is agreed, not something we have to sell** |
| Environment isolation | Every environment segregated, no resource overlap; staging and prod in different compartments | Q11–Q13 answered affirmatively |
| Config surface | **Environment variables** | Confirms our plan, and it is the cloud-portable choice — see §4.4 |
| Probes | `/healthz` and `/readyz`, with startup + readiness + liveness combined to detect a wedged model; internals to be defined by test | We propose the design in §4.6 |
| HA | Full HA evaluation at staging; production "has to be highly available" | ≥2 prod replicas, different fault domains |

### 4.2 VMs instead of bare metal — the blast-radius question survives

The §4.3 recommendation in the previous revision was bare metal with dev split
off. DevOps has gone with four `VM.GPU.A10.1` instances instead, which is the
cleaner choice — but it does **not** by itself buy four failure domains, and
DevOps flagged exactly this:

> "We will see how the VM's are distributed on hardware nodes, and if we have any
> way to split the VM's on different nodes."

`VM.GPU.A10.1` passes a whole A10 through to the guest, so a single physical node
with four A10s can host four of these VMs. Four VMs is not automatically four
hosts. The previous concern therefore carries over in a new form: **dev and prod
VMs could still land on the same physical node**, and a host-level fault or a
driver reset would take both.

### 4.3 Fault domains are the answer to that question

OCI already has the primitive for this, and it is free: a **fault domain** is a
grouping of hardware within an availability domain, and instances in different
fault domains do not share a single point of failure. There are three per AD, and
the fault domain is chosen **at launch time**.

**Recommendation to hand back to DevOps:**

- Place the **dev** VM in a different fault domain from every production VM.
- Place the **two production** VMs in **different fault domains from each
  other** — this is what makes the "production has to be highly available"
  requirement real rather than nominal. Two prod replicas on one physical node
  are not HA.
- Staging can share a fault domain with dev; it is the lower risk pairing.

If stronger separation is needed later, different **availability domains** (where
the region has more than one) or capacity reservations give hard placement
guarantees. Fault domains are the cheap 80% and cost nothing to specify now.

### 4.4 The multi-cloud problem — new, and the biggest change

DevOps added:

> "Currently the plan is to setup staging on Azure and there is no clarity on
> production yet. Hence currently OCI has a functional staging but not in use so
> can be considered as our testing env."

Three consequences, in order of severity.

**(a) Azure's A10 offering is partitioned by default.** The `NVadsA10_v5` series
uses SR-IOV GPU partitioning, and sizes range from **one-sixth of an A10 with a
4 GiB frame buffer** up to a full 24 GiB card. Every memory figure in §3.3
assumes a **whole 24 GiB A10**. On a half card (12 GiB) the AWQ config has only
~4 GiB of KV cache — about 28k tokens, enough for 5 × 4k but **not** 5 × 8k. On a
third (8 GiB) the weights barely fit and the service is unusable.

> **This is the same class of constraint as the MIG point, and it needs to be
> stated to DevOps explicitly: if staging is Azure, the SKU must be a full-card
> size with the complete 24 GiB frame buffer** (believed to be
> `NV36ads_A10_v5` or larger — please confirm the exact size against current Azure
> documentation). A fractional NVads size silently breaks the context-window
> requirement.

**(b) NVads is a vGPU/GRID line, not a datacenter-inference line.** These sizes
ship with an NVIDIA RTX/GRID licence and are marketed for graphics and VDI. CUDA
compute works on vGPU compute profiles, but it is a different guest driver stack
from the datacenter driver we would run on OCI. vLLM on a vGPU profile is
plausible but **unvalidated by us**, and it is not the well-trodden path. If
Azure staging is firm, this needs a spike before it is trusted.

**(c) A different GPU in staging means staging stops validating the numbers.**
The deeper issue is not the SKU but the principle: staging exists to be
production-identical. If staging runs a partitioned A10 — or an Azure NC-series
card, which is a different architecture entirely — then it validates the
*application* but not the memory budget, the throughput target, or the
quantisation choice. Those would only ever be exercised on the OCI card.

**Practical resolution:** use the OCI environment DevOps described as "functional
staging but not in use" as our **performance and capacity reference**, and treat
Azure staging as an application-integration environment. Say so explicitly, so
nobody later reads a green Azure staging run as evidence the throughput target is
met.

**(d) Portability is now a hard requirement, not a nicety.** Production cloud is
undecided. The container must therefore contain **no cloud-specific dependency**:

- Secrets arrive as **environment variables** — which DevOps has already chosen,
  and which is the portable answer. No OCI SDK, no instance-principal call, no
  Azure managed-identity call inside our image. The platform injects; we read.
- No OCI Object Storage or Azure Blob SDK in the model-load path. Take a URL and
  a checksum, or a mounted volume.
- The isolation boundaries in §4.4 of the previous revision were OCI-specific.
  Their portable form:

| Concern | OCI | Azure |
|---|---|---|
| Environment boundary | Compartment | Resource group / subscription |
| Network boundary | VCN + subnet + security list | VNet + subnet + NSG |
| Secret store | Vault + instance principal | Key Vault + managed identity |
| Workload identity | Instance principal | Managed identity |
| Registry | OCIR | ACR |

We depend on the *shape* of that table — one of each per environment, no
cross-environment reuse — not on either column.

### 4.5 Constraints the orchestrator must respect

Unchanged from the previous revision except item 3, which the Azure news makes
load-bearing rather than theoretical.

1. **One whole GPU per pod.** `nvidia.com/gpu: 1`, never a fraction. vLLM
   pre-allocates its KV cache at startup; two vLLM pods on one card means the
   second **fails at boot**, not degrades.
2. **A10 does not support MIG** (MIG is A100/A30/H100). Partitioning on OCI means
   time-slicing or MPS, neither of which gives memory isolation.
3. **The card must be a full 24 GiB A10, not a vGPU partition.** See §4.4(a).
   Every capacity figure in this document assumes the whole card.
4. **Nothing else may share the card** — no graphics workload, no second
   container, no other `nvidia-smi`-visible tenant.
5. **No tensor parallelism.** An 8B AWQ model fits in one card and A10s have no
   NVLink.
6. ~~**Cold start is ~30–60s** for model load.~~ **Measured 2026-09-10: 45s
   warm, 153s cold.** The 30–60s estimate is right for a restart with the
   weights and the `torch.compile` cache already present, and **wrong by 2.5×
   for a fresh pod** that must pull ~6 GB of weights and compile from scratch.
   `deploy/k8s/probes.yaml` now carries `failureThreshold: 60` (a 300s budget);
   its previous placeholder of 24 (120s) would have **boot-looped** a cold pod.
   This governs the probe design below.

### 4.6 Health probe design

DevOps asked for this and is right that it is not standard: *"the way LLM
inferencing works is different from general k8s semantics."* The two specific
hazards are that **model load takes 30–60s** and **a legitimate generation can
take 10s or more under load** — so a naively tuned liveness probe kills healthy
pods, and a naively tuned readiness probe removes every replica from service at
exactly the moment of peak traffic.

Proposed division of labour:

| Probe | Endpoint | Checks | Why |
|---|---|---|---|
| `startupProbe` | vLLM `/health` | Engine initialised | vLLM's `/health` returns 200 only once the engine is up. `periodSeconds: 5`, `failureThreshold: 24` → a 120s load budget. **While this runs, Kubernetes suppresses the other two probes** — which is what makes them safe to tune tightly. |
| `readinessProbe` | `/readyz` | Engine health **only** | See the warning below. `periodSeconds: 10`, `failureThreshold: 2`. |
| `livenessProbe` | `/healthz` | Engine loop alive. **Never a test generation.** | Restart costs 30–60s of reload, so liveness is the last resort. `periodSeconds: 20`, `timeoutSeconds: 5`, `failureThreshold: 3` → ~60s to restart. |

> **Do not put queue depth in the readiness probe.** It is the intuitive design
> and it fails badly: if `/readyz` goes red when the instance is *busy*,
> Kubernetes pulls every replica out of the endpoint list during a traffic spike
> and the service collapses precisely when it is needed. Readiness should answer
> "is this instance broken", not "is this instance busy". Load-aware routing
> belongs in the gateway, which already tracks in-flight requests per replica.

**Detecting a genuinely wedged model** does not fit an HTTP probe, because the
signature is a *relationship between two metrics over time*: requests are queued
**and** the generated-token counter has not moved for N seconds. From vLLM's
`/metrics`:

- `vllm:num_requests_waiting` — queued but not started
- `vllm:num_requests_running` — in flight
- `vllm:generation_tokens_total` — a counter; **flat while `waiting > 0` is the
  wedge signature**
- `vllm:time_to_first_token_seconds` — for the TTFT alert in §7 Phase 4

Two ways to act on it, and we recommend both with different roles:

1. **Gateway circuit breaker (primary).** The gateway already needs one. It sees
   the stall first, stops routing to that replica, and alerts. No restart, no
   data loss, human in the loop.
2. **Sidecar backstop (automated).** A small process scrapes `/metrics` and fails
   `/healthz` if the wedge signature holds for a conservative window — 120s, not
   20s. This is what finally triggers the `livenessProbe`.

Weighting matters: an automatic restart on a false positive is worse than a late
alert, because the restart costs a minute of reload and can loop. Start with the
breaker and a generous sidecar window, then tighten once real failure data
exists. **Both thresholds are Phase 1 measurements, not guesses** — which is
exactly what DevOps meant by "to be determined based on the tests."

### 4.7 The MacBook Air constraint


An M5 MacBook Air has no CUDA and Docker Desktop on macOS has no GPU
passthrough, so **vLLM cannot run locally at all**. The dev loop must be:

1. **Gateway development** — fully local. Runs against a stub OpenAI-compatible
   server (a ~100-line FastAPI fake, or `vllm serve Qwen/Qwen3-0.6B` on the dev
   A10). This is where most Phase 1–3 work happens and it is unblocked today.
2. **App-side backend development** — fully local, against the same stub.
3. **vLLM server configuration** — only on the dev A10, over the tunnel. Treat
   `vllm serve` flags as infrastructure-as-code reviewed and applied remotely,
   never iterated by hand.
4. **Local inference for offline work** — keep `LLM_BACKEND=llamacpp` with the
   existing GGUF path. This is GGUF's correct home in the project.

### 4.8 Rented A10 as the interim dev box (vast.ai)

Rather than wait on the shared DevOps VM, Phase 1 runs on a personal rented A10.
This is a good call: it turns Phase 1 from blocked into startable, it is cheap
(roughly $0.20–0.50/hr for an A10 at current listings — check before booking),
and vast.ai is container-native, which is exactly the artefact we are building.

**Treat the host as untrusted. This is now operative, not hypothetical.**

A rented GPU is someone else's machine, and the operator can in principle reach
anything inside the container. Therefore, without exception:

- **Synthetic prompts only.** No investor data, no KYC data, no real asset
  documents, nothing drawn from `chat_interactions.log`. The Phase 0 eval set
  must have a synthetic variant for exactly this reason — build that variant
  first, not as an afterthought.
- **No production secrets.** No `ANTHROPIC_API_KEY`, no OCI credentials, no
  gateway signing key. Generate throwaway keys for the box and discard them.
- **No real gateway API keys.** Test the gateway's auth against locally
  generated keys with no real grant behind them.

**Verify on first boot, and record it:**

| Check | Why |
|---|---|
| `nvidia-smi` reports **24 GiB** on one device | Listings vary; a partitioned or smaller card invalidates §3.3 |
| The card is an **A10**, not an A10G or L4 | A10G has different bandwidth and clocks; usable, but the throughput numbers would not transfer |
| Driver + CUDA version | Sets the minimum we hand DevOps (Q17) |
| Disk free ≥ 30 GB | ~6 GB weights plus image layers plus HF cache |

**What the rented box is good for:**

- Validating the §3.3 config-B memory math against real `nvidia-smi` and vLLM's
  reported KV cache blocks. **This is the single highest-value thing to do on it**
  — every capacity claim in this document is currently calculated, not measured.
- Building and iterating the container image and the probe endpoints (§4.6).
- Measuring cold-start time, which sets `startupProbe.failureThreshold`.
- Establishing the *shape* of the throughput curve at 1/3/5/8 concurrent.
- Confirming Qwen3 thinking-mode behaviour (B3) and the `/think` soft-switch
  question.

**What it is not good for:**

- **Numbers handed to procurement.** DevOps is sizing per-environment GPU counts
  off measured TPS, and a vast.ai host has different CPU, PCIe topology, and
  memory bandwidth from `VM.GPU.A10.1`. Treat rented-box figures as a
  **provisional baseline to be re-confirmed on the OCI card** before anyone
  multiplies them. Say so explicitly when reporting.
- Anything in the Phase 2 security review, which has to run against the real
  deployment path.
- Latency figures that include network, since the box is not in-region.

---

## 5. Security design

> **Agreed with DevOps (2026-09-04).** They independently proposed
> `FIRSTDATA PLATFORM → Inference Gateway → vLLM`, which is the architecture
> below. The gateway is settled; what remains is building it.

### 5.1 The core principle

vLLM's OpenAI server has exactly one auth primitive: `--api-key`, a single
static shared secret. It has **no per-tenant identity, no rate limiting, no
quotas, no per-key audit trail, and no request-size limits**. It must never be
the edge. Everything in R5 lives in a gateway we own.

### 5.2 Layers

```
Client (C# API / Web / Mobile)
  │  TLS 1.3, short-lived JWT or API key
  ▼
Edge — Cloudflare Tunnel or LB + WAF
  │  no inbound ports on the GPU host; global rate limit; TLS termination
  ▼
Gateway (our service — the deliverable)
  │  authN/authZ · per-key quota + concurrency cap · request validation
  │  token accounting · metadata audit log · SSE streaming passthrough
  │  circuit breaker · model allowlist
  ▼  private subnet, mTLS
vLLM replicas (no public IP, --disable-log-requests)
```

### 5.3 Gateway requirements

**Authentication.** Two credential types:

- **Short-lived JWTs** for service-to-service (preferred). The C# API already
  resolves caller identity from a Bearer token; it mints an asymmetrically-signed
  JWT with `sub`, `scope`, and a ≤5-minute expiry. The gateway validates the
  signature — no shared secret to leak, no revocation lag.
- **API keys** for non-interactive jobs and break-glass. Format
  `bkn_live_<keyid>_<secret>` so `keyid` is indexable without a table scan.
  **Store only `argon2id(secret + pepper)`, never the raw key.** Per-key expiry,
  rotation with an overlap window, and a revocation list checked on every
  request.

**Authorisation & quotas.** Per key: scopes (which models, which endpoints),
a **token budget** (not just a request count — one 8k-context request costs
what 20 short ones do), and a **concurrency cap** so one tenant cannot
monopolise a GPU and starve the other four users.

**Request validation.** Reject before the GPU sees it: max prompt tokens, a
ceiling on caller-supplied `max_tokens`, model allowlist, max body size,
per-request timeout.

**Audit.** Log **metadata only** — timestamp, key id, model, prompt/completion
token counts, latency, status. Never prompt or completion text. This is the
difference between an audit trail and a PII store.

**Resilience.** Circuit breaker per replica, bounded retries with jitter on
429/503, health-check-based routing so a wedged replica is removed rather than
retried into.

### 5.4 On "encrypted messages/content as well?"

Worth being direct, because this is where effort is most often misspent.

**End-to-end encryption to the model is not achievable on this hardware.** The
GPU must see plaintext to run attention over it. Adding application-layer payload
encryption on top of TLS only helps against an attacker who can read the
gateway's memory but not the GPU's — and since the gateway holds the decryption
key, that gap is largely illusory. It is cost without benefit here.

What actually reduces exposure, in descending order of value:

1. **TLS 1.3 everywhere**, including gateway→vLLM (mTLS on the private subnet).
2. **No-retention by default.** ~~`--disable-log-requests` on vLLM~~; no prompt
   logging in the gateway; metadata-only audit (§5.3).
   > **Corrected 2026-09-10.** `--disable-log-requests` **does not exist in
   > vLLM 0.28.0** — `vllm serve` fails to start with it. It was replaced by
   > `--enable-log-requests` / `--no-enable-log-requests` with the **polarity
   > inverted**: request logging is now off by default and opt-in. There is also
   > `--enable-log-outputs` for completion text, likewise off by default. So this
   > requirement is now satisfied *by default*, which is a better posture than
   > relying on a flag — but the correct launch flags are
   > **`--no-enable-log-requests --no-enable-log-outputs`**, stated explicitly so
   > the intent survives a future default change. See `PHASE1_RESULTS.md` §2.
3. **Redact before send.** The platform already tokenises assets — extend that
   discipline to prompts. Strip or pseudonymise investor identifiers in the
   context block before it leaves the app.
4. **Encrypt everything that does persist**: Redis AOF, chat interaction logs,
   any LMCache disk tier, model cache volumes.
5. **Model supply chain**: pin model revision by commit hash, safetensors only
   (never pickle), verify checksums at image build, scan images, produce an SBOM.

**Confidential computing** — memory encrypted from the host, with remote
attestation — requires H100 CC or AMD SEV-SNP with a CC-capable GPU. **The A10
cannot do this.** If a regulator ever demands cryptographic isolation from the
infrastructure operator, this hardware does not satisfy it. Better to know that
before the GPUs are bought than after.

---

## 6. What in `baas-poc-templates` is affected

The good news first: **`app/core/llm_backends/base.py` is already the right
abstraction.** A remote backend is a new file implementing `load`, `generate`,
`count_tokens`, `status`, `shutdown` — no call-site changes. The items below are
the places where the current design assumes a *local, single-threaded* model.

### 6.1 Blocking — the endpoint is useless without these

| # | Finding | Location | Why it blocks |
|---|---|---|---|
| B1 | **The app can only ever have one request in flight — and infra cannot fix that.** `LLMEngine._lock` is an `RLock` held *around* `self._backend.generate(...)`, so for a remote backend the HTTP call happens inside the lock. A load balancer can only distribute requests it has received; if the client emits one at a time, there is never a second request to split by card capacity. | `llm_engine.py:70,261,269` | R2 asks for 3–5 concurrent. The symptom is not slowness but silent quality loss: `max_wait_sec` defaults to 30s, and on timeout `generate()` returns `("", True)` → a RAG-only answer. At ~10s per generation, user 2 waits and is served; users 4–5 wait 30s and get a non-LLM answer. **Resolution: scale AI-service replicas, do not add in-process concurrency** — see §6.4. |
| B2 | **`EXTRACTION_LIMITER = CapacityLimiter(1)`** serialises extraction across `/assets` and `/media`. | `llm_engine.py:35` | Same root cause, same resolution — one in-flight extraction per replica is correct; add replicas. |
| B3 | **Qwen3 thinking mode will empty every answer.** Qwen3-8B is a hybrid reasoning model; the default chat template emits `<think>…</think>`. `FAQ_MAX_TOKENS = 160` would be consumed entirely by an unterminated think block, `generate()` returns empty text, and the facade maps empty → `("", True)` → degraded. | `chat.py:157`, `llm_engine.py:274` | **The entire app would silently fall back to RAG-only mode** with no error, only the degraded banner. **Fix:** `chat_template_kwargs: {"enable_thinking": false}`. Raising the token allowance instead is *not* an alternative — the trace still reaches `_cap_answer(text, 800)`, which truncates at 800 chars and would ship the reasoning as the answer. A `<think>` stripper is required either way, and once present, disabling is free. ~~Two things to verify in Phase 1: whether `enable_thinking: false` overrides a user typing `/think` in their query (Qwen3 parses that as a soft switch)~~ **— VERIFIED 2026-09-10 on real Qwen3-8B-AWQ: the request flag WINS, in both directions.** `/think` does not re-enable reasoning against `enable_thinking: false`, and `/no_think` does not suppress it against `enable_thinking: true`. `chat_template_kwargs` is authoritative and the soft switch is subordinate to it, so **no input sanitisation is needed at the app boundary** and `strip_think` is a belt rather than the primary defence. B3's premise was also confirmed (the model does default to emitting `<think>`) and its predicted failure reproduced exactly at `max_tokens: 160` — unterminated block, `finish_reason: length`, nothing surviving the stripper. The gateway's passthrough was separately confirmed to preserve `chat_template_kwargs`, which is the failure that would have broken this fix in production while every stub test stayed green. See `PHASE1_RESULTS.md` §6. Still open: that the `presence_penalty` mapping does not silently drop. |
| B4 | ~~**Tokenizer mismatch.**~~ **RESOLVED 2026-09-09, and the stated impact was wrong.** Original claim: `TOKENIZER_DIR` points at a vendored Qwen2.5 tokenizer, Qwen3's vocab is 151,552 and differs, so "wrong counts silently mis-size every chunk". **Measured: they do not.** Both tokenizers report `vocab_size` **151643**, and token IDs were byte-identical across **all 349 distinct strings** in `chat_interactions.log` plus all five extraction fixtures — zero count differences on English, Arabic, hex-dense and money-dense text. Chunk budgets were never mis-sized. What genuinely differs is **four added control tokens** — `<think>`, `</think>`, `<tool_response>`, `</tool_response>` — which Qwen3 encodes as **one token each** and Qwen2.5 splits into three or four. Narrow, but it lands precisely on the B3 reasoning-block surface. **Fixed** by vendoring both and selecting from `LLM_BACKEND` (`config.py`), since the right tokenizer is a property of the model served, not of the service. Pinned by `tests/test_tokenizer.py::TestQwen3Divergence` and `::TestBackendSelection`. | `config.py` | — |

> ### Correction — B4's fix is real but is not "vendored"
>
> **Found 2026-09-09 while preparing the rented box.** B4 above, and the
> handoff, both describe the two tokenizers as *vendored*. They are not, in the
> sense that word implies:
>
> ```
> $ git ls-files services/bkn301-ai-service/models/
> (nothing)
> ```
>
> Both directories are **gitignored and untracked** — 11 MB for Qwen2.5, 15 MB
> for Qwen3, present only on the laptop that downloaded them. The `.gitignore`
> comment says they are "fetched by start.sh alongside the GGUF", but no script
> in the repo references `qwen3-tokenizer`. So B4's fix:
>
> - does not reproduce from a clean clone;
> - cannot reach CI, DevOps, a container image, or a rented box;
> - is pinned by `tests/test_tokenizer.py`, which therefore passes only on a
>   machine where the directory happens to already exist.
>
> Not urgent — the rented box no longer runs the app at all (§4.8), so nothing
> in Phase 1 is blocked. But "vendored" should mean committed, fetched by a
> pinned script with a checksum, or baked into the image, and right now it means
> none of those. Phase 3 needs one of them chosen before `LLM_BACKEND=vllm`
> runs anywhere but this laptop.
>
> **Related trap, worth fixing at the same time.** `.env.example:65` hardcodes
> `TOKENIZER_DIR=./models/qwen2.5-tokenizer`, and an explicit `TOKENIZER_DIR`
> **overrides** the `LLM_BACKEND`-based selection (`config.py:141`). Anyone who
> copies the example to `.env` — which is exactly what an example file invites —
> silently gets the Qwen2.5 tokenizer while serving Qwen3, losing precisely the
> single-token `<think>` handling that was B4's only genuine finding.

### 6.2 Security gaps in the app

> **Re-verified against `main` on 2026-09-08**, after rebasing onto `afbf0c5`.
> Two findings were independently fixed by other people, one new one appeared,
> and four are still open.
>
> | | Status |
> |---|---|
> | **S2** loopback bind | ✅ Fixed on main (`c98d2f9`) — but see the S10 correction |
> | **S4** plaintext logs | ✅ Largely fixed on main (`0f13cac`) — content masked; rotation still open |
> | **S1** live Anthropic key | ✅ **Closed 2026-09-09.** See correction (a) below |
> | **S5** no `.dockerignore` | ✅ **Fixed 2026-09-09.** Build context 5.4 GB → 1.32 MB |
> | **S10** compose publish | ✅ **Fixed 2026-09-09**, but its diagnosis was wrong — see correction (b) |
> | **S3** cache key missing `role` | ❌ Still open — `_cache_key` unchanged |
> | **S6** Redis unauthenticated + published | ❌ Still open (Phase 4) |
> | **S9** dev CORS | ❌ Still open (dev-only, fine for now) |
>
> ---
>
> ### Correction (a) — S1: there was no key in `.env`
>
> Verified 2026-09-09 while executing Phase 0. `services/bkn301-ai-service/.env`
> is **2 bytes** (`# `) with zero `KEY=` assignments. `ANTHROPIC_API_KEY` appears
> nowhere in the service; there is no Anthropic SDK in `requirements.txt`, no
> `load_dotenv()` call, and no `env_file:` in compose — so **`.env` was inert**:
> nothing read it. It was never committed. (The `sk-ant-` matches in
> `ConfigController.cs` history are `body.ApiKey.StartsWith("sk-ant-")`, a prefix
> validation check, not a key.)
>
> The key was independently rotated at the console. S1 closes. The residual
> defect — that `.env` was a trap, gitignored and unread with no example file, so
> a future secret would silently fail to load *and* bake into the image — is
> addressed by the new `.env.example` and by compose now reading `env_file`.
>
> ### Correction (b) — S10's end state was right, its diagnosis was not
>
> S10 below claims the containerised service "binds the *container's* loopback
> and is unreachable through the published port". It does not.
> **`Dockerfile:26` launches `main:app` as an ASGI target with `--host 0.0.0.0`
> hardcoded**, so `main.py:112`'s `if __name__ == "__main__":` never runs and
> `main.py:129-135`'s `uvicorn.run(host=settings.HOST)` is dead code in the
> container. `settings.HOST` is irrelevant there.
>
> The consequence is the inverse of what S10 describes, and worse: the compose
> path was never broken, it was **exposed** — `docker-compose.yml:50` published
> `"8000:8000"` on all host interfaces in front of a service with no
> authentication on any endpoint. The `c98d2f9` S2 fix never reached the
> container path at all.
>
> Both prescribed changes still landed, but with their roles reversed: narrowing
> the publish to `"127.0.0.1:8000:8000"` was the entire fix, and `HOST: 0.0.0.0`
> in `environment:` is a **no-op today**, kept only as a belt for anyone running
> `python main.py` inside the container and commented as such.
>
> `main` also gained upload-path sanitisation, DOCX decompression bounds,
> content-vs-extension checks, and prompt-injection scanning of document text
> (`c98d2f9`) — none of which were on our list, and all of which reduce the
> surface the gateway has to defend.

| # | Finding | Location | Severity |
|---|---|---|---|
| S1 | **STILL OPEN as of 2026-09-08.** **A live Anthropic API key sits in plaintext** in `services/bkn301-ai-service/.env`. Correctly gitignored (`.gitignore:77`) and absent from git history — but it is a working production credential on a laptop, and it has now passed through a tooling context. **Rotate it, and move it to a secret manager.** | `.env:1` | High |
| S2 | **FIXED ON MAIN by `c98d2f9` (Sanskar, 2026-09-04)** — `config.py` now defaults `HOST` to `127.0.0.1` and `app/main.py` binds `settings.HOST` rather than a hardcoded `0.0.0.0`, citing the no-auth reason. The underlying authz gap (below) remains by scope decision, but it is no longer exposed by default. **See S10 for a new inconsistency this created.** — original finding: **The AI service has no authentication whatsoever.** `/v1/conversations` takes `userId` as a **query parameter** and trusts it. Anyone able to reach port 8000 can list, read, rename, and delete any user's entire conversation history. `/v1/chat-rag` likewise trusts body-supplied `role` and `userId`. **Deferred by scope decision** — the AI service stays local for now, so the loopback bind is the containment. But that containment is currently nominal: `docker-compose.yml:50` publishes `"8000:8000"`, which binds *all* host interfaces, and `config.py:8` sets `HOST="0.0.0.0"`. **Do now, one line:** change the compose publish to `"127.0.0.1:8000:8000"`, so the day this runs on a cloud VM it is not open by default. Full auth lands whenever the AI service itself moves. | `conversations.py:28,41,56,66`; `chat.py:160,166`; `docker-compose.yml:50` | **Critical if exposed** — contained by loopback while local |
| S3 | **The answer cache is not keyed by `role` or `userId`.** `_cache_key` covers lang, knowledge fingerprint, context hash, and normalised query — but `role` shapes the system prompt and guardrails, so a `staff`-generated answer can be served verbatim to a `retail` caller asking the same question. | `chat.py:92-119` | High |
| S4 | **LARGELY FIXED ON MAIN by `0f13cac`** — a new `app/core/masking.py` is the single choke point, and `log_chat_interaction` now writes `mask_text(...)` for both query and answer plus `stable_hash(...)` of the raw values so lines stay joinable without content reaching disk. `ResponseValidator._mask_pii` delegates to the same module, so the two cannot disagree. **Rotation and retention are still unaddressed**, but severity drops from High to Low now that content is masked. — original finding: **Plaintext prompt/response logs, unrotated, no retention.** `chat_interactions.log` records every query and answer; `token_stats.jsonl` likewise. Both host-mounted via Compose. No redaction, no rotation, no expiry — while conversations in Redis correctly expire after 30 days. | `chat.py:32-45`; `telemetry_logger.py:12-13`; `docker-compose.yml:61` | High |
| S5 | **No `.dockerignore`.** `COPY . .` bakes `.env` (with S1's key), `logs/`, `.venv*`, and `models/` into the image. | `Dockerfile:21` | High |
| S6 | **Redis has no password, no TLS, and publishes 6379 to the host.** Holds 30 days of conversation history. | `docker-compose.yml:14-15` | Medium |
| S7 | Container runs as root; `build-essential` and `cmake` remain in the final image; no multi-stage build. | `Dockerfile` | Medium |
| S8 | No rate limiting, request-size limit, or request timeout on any FastAPI route. | `app/main.py` | Medium |
| S9 | `CORSMiddleware` with `allow_credentials=True` and `allow_headers=["*"]`. Correct for localhost dev; must not ship. | `app/main.py:7-13` | Low (dev-only today) |
| S10 | **NEW, introduced by the S2 fix.** `docker-compose.yml` publishes `"8000:8000"` and does **not** set `HOST`, so the containerised ai-service now binds the *container's* loopback and is unreachable through the published port — the compose path is silently broken. **Fix:** set `HOST: 0.0.0.0` in the compose `environment:` block **and** change the publish to `"127.0.0.1:8000:8000"`. That combination is the correct one: the container accepts forwarded traffic, and the host only exposes it on loopback. | `docker-compose.yml:50-53` | <span>Medium — breaks compose, not security</span> |

### 6.3 Behavioural changes to plan for

| # | Item | Detail |
|---|---|---|
| C1 | **Sampling defaults must change.** Qwen3 non-thinking mode wants temp 0.7 / top_p 0.8 / top_k 20 / presence_penalty 1.5. Current: temp 0.2 / top_p 0.9 / top_k 0 / repeat_penalty 1.05. Note `repeat_penalty` is not an OpenAI-API parameter — it maps to vLLM's `repetition_penalty` via `extra_body`, *not* to `presence_penalty`. Qwen also documents "do not use greedy decoding". | Needs an explicit param-mapping layer, and re-tuning: `numeric_guard` and `response_validator` thresholds were calibrated at temp 0.2 against a 1.5B model. |
| C2 | **Streaming is now required, and it conflicts with the validators.** 200 tokens at 20 tok/s is 10 seconds of blank screen unstreamed. But `_cap_answer`, `numeric_guard`, and `response_validator` all need the complete text. Streaming optimistically and correcting after the fact is visible to the user; buffering discards the latency win. | Product decision — flag for Phase 4. Recommended: stream, but hold the final 10% and run validators before emitting the tail. |
| C3 | **Timeouts and retries are local-CPU numbers.** `LLM_EXTRACTION_TIMEOUT_SEC = 180` assumes a slow local model. | Remote needs separate connect/read timeouts, bounded retries with jitter on 429/503, and a breaker that trips to RAG-only rather than hanging. |
| C4 | **`REQUIRE_LLM` semantics change.** Locally, model availability is decided at load time. Remotely it is a per-request property. | Startup should `/health`-probe the endpoint; `degraded` must be recoverable. `_enter_degraded(fatal=...)` already distinguishes the two cases — build on it. |
| C5 | **`LLM_CTX_SIZE` becomes advisory.** Locally it allocates; remotely `--max-model-len` is server-side. | Config must not imply the client controls it. Gateway should reject over-length requests (§5.3) rather than letting vLLM truncate. |
| C6 | `queue_depth` in `/health` is not a queue depth. It increments only on failed lock acquisition and decrements on success, floored at 0 — it reports "failures minus successes". | `llm_engine.py:262,268`. Pre-existing; replace with a real gauge when B1 lands. |

---

### 6.4 Concurrency: replicas, not a semaphore

B1 has two candidate fixes. **Scaling AI-service replicas is the better one**,
and the codebase itself is the argument.

Raising in-process concurrency (`RLock` → `BoundedSemaphore`, `max_concurrency`
on the backend protocol) is a ten-line change with a much wider blast radius,
because the whole service is written on a single-threaded-model assumption:

- `_ANSWER_CACHE` (`chat.py:88`) is an `OrderedDict` mutated with no lock. The
  `get` → `move_to_end` → `while len > max: popitem` sequences interleave under
  threads, so LRU accounting and eviction become unreliable.
- `_CACHE_STATS` increments are unsynchronised.
- `llm_engine.py:254` records that `last_usage` **already had exactly this bug**:
  usage was written before the lock, so "a caller arriving mid-inference reset
  the running caller's counters and the winner reported the loser's numbers."

Replicas preserve the assumption instead of breaking it in a dozen places that
would each have to be found. And with a remote backend a replica is cheap — no
GPU weights, just the reranker (~120 MB), CLIP (~600 MB, lazy), and Chroma, so
roughly 1–2 GB of RAM each.

**Therefore:** `WORKERS=1` and the Dockerfile's `--workers 1` stay exactly as
they are. The lock stays. Concurrency comes from running N AI-service instances
behind the C# API, which is a deployment change rather than an application one.

Two consequences to carry forward:

- **The replica count is a capacity requirement, not an implementation detail.**
  N replicas ⇒ N maximum concurrent LLM requests. To meet R2, N ≥ 5.
- `_ANSWER_CACHE` is per-process, so N replicas mean N independent caches and a
  roughly 1/N hit rate on first contact. If that materially hurts, the fix is a
  shared cache in Redis — which is already a dependency — not in-process threads.

Also note for the `vllm` remote backend (`openai_http.py`, not the
`vllm_remote.py` this document originally named): `LLMEngine.__init__` fatally degrades
when `os.path.exists(settings.TEXT_MODEL_PATH)` is false (`llm_engine.py:76`).
A remote backend has no local model file and must bypass that check.

---

## 7. Phased plan

### Phase 0 — Baseline & decisions (1 week, no GPU needed)

Unblocked on the MacBook today.

- [ ] **Send DevOps the three items in §8 Q1–Q3:** fault-domain placement
      (§4.3), the Azure full-card SKU requirement (§4.4a), and confirmation that
      the OCI card — not Azure staging — is the performance reference (§4.4c).
- [x] ~~Fix `.gitignore` so `docs/*` stops hiding this document~~ — done,
      committed in `bea69dd`.
- [x] ~~Rotate the Anthropic key (S1)~~ — **closed 2026-09-09**. Rotated at the
      console, and there was no key in `.env` to remove. See §6.2 correction (a).
- [x] ~~Add a `.dockerignore` (S5)~~ — **done 2026-09-09**. Allowlist form, so it
      fails closed as the tree grows. Build context **5.4 GB → 1.32 MB**;
      `.env`, `logs/` (1188 plaintext records), `models/` and both venvs verified
      absent from the image.
- [x] ~~Fix the compose publish (S10)~~ — **done 2026-09-09**, though the finding's
      diagnosis was wrong; see §6.2 correction (b).
- [x] ~~Change the Compose publish so the AI service is not exposed by
      default~~ — superseded: `c98d2f9` on `main` fixed the bind itself
      (`HOST` now defaults to `127.0.0.1`). See S10 for the leftover.
- [x] ~~Write the eval set~~ — **done 2026-09-09.** `chat_cases.json` extended
      12 → **36 cases, 25% Arabic**, calibrated over three runs against
      `llamacpp`. All 24 new cases pass; see §7.1 for what calibration exposed.
- [x] ~~Build a synthetic variant~~ — **done 2026-09-09**, but *not* the way this
      line assumed. `masking.py` turned out to be a head start only for
      DETECTION: it masks irreversibly (`****1234`) rather than pseudonymising,
      and its six patterns match SSN/email/card/wallet/10+-digit runs, **none of
      which occur anywhere in the corpus**. What is actually sensitive there is
      names, companies and figures, which `mask_text` never touches, and masking
      would break every numeric extraction golden besides. Delivered instead as
      `scripts/pseudonymise.py`: format-preserving, deterministic via
      `stable_hash`, with `masking.py`'s patterns reused as a leak detector in
      `tests/test_pseudonymiser.py`.
- [x] ~~Extend `scripts/bench_llm_backends.py`~~ — **done 2026-09-09.**
      `--backend vllm` via a new `app/core/llm_backends/openai_http.py`.
      `model_sig` now records the endpoint for a remote run, since hashing an
      absent local GGUF would silently make two different models look identical.
- [x] ~~Build the stub OpenAI-compatible server~~ — **done 2026-09-09**, in the
      new repo at `stub/server.py`.
- [ ] Draft the probe contract from §4.6 and send it to DevOps — they asked for
      the internals and it is cheap to propose before the dev box lands.

**Exit:** eval set committed, harness runs against a stub, key rotated. ✅ **Met
2026-09-09.**

### 7.1 What building the eval set exposed

Calibrating 24 new cases against `llamacpp` took three rounds, and the failures
were more useful than the passes. Four are defects in the app, not the fixture,
and none was on any list before this week.

| # | Finding | Evidence |
|---|---|---|
| E1 | **The out-of-scope guard is English-only.** "What do you think about the weather in Paris tomorrow?" takes the `no_model` refusal path. The *same question in Arabic* reaches the model. On a bilingual platform, Arabic speakers walk through a door that is shut for English speakers, at a full inference each time. | `ar_refuse_out_of_scope` vs `refuse_out_of_scope` |
| E2 | **The model fabricates investor contact details.** Asked for names and email addresses it has no access to, it does not decline — it invents a plausible roster including `@example.com` addresses. The data is obviously fake on inspection, which is what makes it dangerous: an operator who asks for a contact list and gets a well-formatted one has no signal it was invented. Confabulation beats refusal for looking like success. | `refuse_pii_request`, marked `known_failure` |
| E3 | **`live_ctx` omits more than §6.1 recorded.** Token *price* and *supply* figures are in the context payload but never rendered into the prompt, so the model cannot answer questions about them — the same class of gap already noted for mint/burn/transfer and `draftAssetNames`. Both the English and Arabic supply questions failed identically, ruling out a language cause. | `model_asset_price`, `en_supply_overview`, `ar_supply_overview` |
| E4 | **The committed baselines are not comparable to anything run today.** `scripts/bench_baselines/*.jsonl` were captured at `model_sig …/2104932768` — the 2.0 GB **3B** model. The default is now the 1.2 GB **1.5B** model (`…/1285494304`), and the knowledge fingerprint has changed too. Three *pre-existing* cases (`en_kyc_pipeline`, `en_treasury_ops`, `refuse_gibberish`) pass in the baseline and fail today. **These were left failing rather than relaxed**: they are a real quality signal from the model downgrade, and silencing them would destroy it. | header diff, `bench_phase0_calibration3.jsonl` |

**Also worth carrying forward:** routing is far more sensitive to phrasing than
expected. "Give me the top 3 assets by AUM" renders a widget; "Which three assets
have the highest AUM?" is answered from live context; "Summarise the KYC
pipeline" reaches the model while "Summarise total supply" does not. Any prompt
change is a routing change.

### Phase 1 — Single-GPU vLLM on dev (1–2 weeks)

> **Phase 1 is now a procurement input, not just a validation step.** DevOps set
> the per-environment GPU counts against measured TPS, so these numbers decide
> how many cards get bought. Treat the benchmark as a deliverable with an
> audience, and report tok/s/user at 1/3/5/8 concurrent rather than a single
> aggregate figure.

- [ ] **Start on the rented A10 (§4.8)**, not the DevOps VM. Run the first-boot
      verification table, and record driver + CUDA version for DevOps Q17.
- [ ] Later, take delivery of the shared dev VM over the existing VPN. Confirm
      it is a full-card `VM.GPU.A10.1` and confirm its fault domain (§4.3).
      **Re-run the benchmark here before any number goes to procurement.**
- [ ] `vllm serve Qwen/Qwen3-8B-AWQ --max-model-len 8192
      --gpu-memory-utilization 0.90 --enable-prefix-caching
      --disable-log-requests`, pinned to a model revision hash.
- [ ] Dockerise: pinned vLLM image, model baked or pulled from an internal
      mirror with checksum verification, non-root, healthcheck.
- [ ] Confirm the §3.3 config-B memory math against `nvidia-smi` and vLLM's
      reported KV cache blocks. **Numbers in this document are calculated, not
      measured — this is the step that validates them.**
- [ ] Benchmark against the Phase 0 eval set at 1, 3, 5, 8 concurrent. Record
      tok/s/user, TTFT p50/p95, and quality vs the BF16 reference.

- [ ] Measure the probe thresholds §4.6 leaves open: actual cold-start time (sets
      `startupProbe.failureThreshold`) and the wedge-detection window.

**Exit gate:** ≥20 tok/s/user at 5 concurrent with 8k context, and quality within
tolerance of BF16 on the eval set. **If AWQ quality fails the gate, fall back to
config A (BF16) at 4×8k or 5×6k** — R1 is still met at ~25 tok/s/user.

**Second deliverable:** a TPS-per-card figure DevOps can multiply, plus the
cold-start and wedge-window measurements for the probe config.

### Phase 2 — Gateway (2–3 weeks)

The core deliverable of the new repo. Developed entirely on the MacBook against
the stub, then pointed at the dev GPU.

- [ ] OpenAI-compatible surface: `/v1/chat/completions` (streaming + non-),
      `/v1/models`, `/healthz`, `/metrics`.
- [ ] API key store: `argon2id` hashes, `keyid` prefix, expiry, revocation list.
- [ ] JWT validation path for service-to-service (§5.3).
- [ ] Per-key token quota, concurrency cap, request validation, model allowlist.
- [ ] Metadata-only audit log. Explicit test asserting no prompt text is logged.
- [ ] Circuit breaker + health-based routing (single replica for now, but the
      router interface lands here).
- [ ] mTLS gateway→vLLM.
- [ ] Measure prefix-cache hit rate to inform the §3.6 LMCache decision.

**Exit gate:** security review of the gateway; abuse tests (quota exhaustion,
oversized prompt, revoked key, expired JWT, concurrency starvation) all pass.

### Phase 3 — App integration (2 weeks, in `baas-poc-templates`)

- [ ] **B1/B2: no app change.** The lock, `EXTRACTION_LIMITER`, and `WORKERS=1`
      all stay (§6.4). Instead, specify the replica count as a capacity
      requirement to DevOps: N replicas ⇒ N concurrent requests, so N ≥ 5.
- [x] ~~`app/core/llm_backends/vllm_remote.py` implementing the protocol.
      `LLM_BACKEND=vllm` in `_build_backend`. Bypass the
      `os.path.exists(TEXT_MODEL_PATH)` startup check (§6.4).~~ **Done
      2026-09-09, under a different filename.** It shipped as
      `app/core/llm_backends/openai_http.py` (class `OpenAIHttpBackend`,
      `name = "vllm"`), not `vllm_remote.py` — there is no file by that name and
      searching for one finds nothing. Wired into `_build_backend`
      (`llm_engine.py:53-71`), with the `TEXT_MODEL_PATH` bypass at
      `llm_engine.py:102-112` and remote-aware `status()` at `:204-212`, so
      `LLM_BACKEND=vllm` works end to end in the app and not only in the bench
      script. **One gap remains:** it hardcodes `"stream": False`
      (`openai_http.py:177`), so there is no streaming path and no TTFT
      available from the app — which is why C2's streaming work in Phase 4 is
      not merely a product decision but also missing plumbing.
- [x] ~~**B3:** `enable_thinking: false` + `<think>` stripper.~~ **Done
      2026-09-09**, and moved earlier than planned: Phase 1 benchmarks Qwen3 on
      the rented box, and without B3 every answer returns empty and the whole run
      reads as uniformly degraded. Lives in `llm_backends/openai_http.py`, with
      `strip_think` handling the unterminated block that `max_tokens` produces.
- [x] ~~**B4:** vendor the Qwen3 tokenizer.~~ **Done 2026-09-09**, selected from
      `LLM_BACKEND`. See the corrected §6.1 B4 — the count-drift impact it
      claimed does not exist; the four control tokens are the real difference.
- [ ] **C1:** sampling param mapping layer; re-tune and re-validate
      `numeric_guard` / `response_validator` thresholds.
- [ ] **C3–C5:** timeouts, retries, breaker, `/health` probe, `LLM_CTX_SIZE`
      semantics. **C6:** real queue-depth gauge.
- [ ] **S3:** add `role` to `_cache_key`. **S4:** redact + rotate + expire the
      interaction logs. (**S2** full auth is deferred — the AI service stays
      local; the loopback-bind hedge lands in Phase 0.)
- [ ] Validate all three builds per `CLAUDE.md`: `dotnet build`,
      `npm run build`, `npx tsc --noEmit` — 0 errors each.

**Exit gate:** eval set passes via `LLM_BACKEND=vllm` at parity or better with
`llamacpp`; `llamacpp` still works unchanged for offline laptop development.

### Phase 4 — Staging & production (2 weeks)

- [ ] Staging A10, production-identical config. Run the full eval set.
- [ ] **C2:** streaming decision implemented end-to-end (gateway → AI service →
      Web/Mobile).
- [ ] Two prod vLLM instances behind the gateway router, and ≥5 AI-service
      replicas (§6.4). Rolling deploy verified with zero dropped requests.
- [ ] **S6–S9:** Redis auth + TLS + unpublished port; non-root multi-stage
      image; rate limits and request-size caps; production CORS.
- [ ] Observability: Prometheus scrape of vLLM + gateway, alerts on tok/s/user
      below target, TTFT p95, KV cache utilisation, breaker trips.
- [ ] Runbook: replica wedge, OOM, model rollback, key compromise.

**Exit gate:** load test at 5 concurrent sustained for 1 hour; failover drill
(kill one replica under load, zero errors); external security review.

### Phase 5 — Optimisation, benchmark-gated (opportunistic)

Each item is an experiment with a measured before/after, not a commitment.

- [ ] **DFlare** (§3.4). Test on the dev GPU only. Config D (AWQ + DFlare) is the
      only variant that fits R4. Measure the acceptance rate at batch 1, 3, and 5
      — if the batch-5 speedup is under ~1.5×, the added footprint and
      out-of-tree dependency are not worth it. **Do not put an out-of-tree
      speculative-decoding path on a shared prod host (§4.3).**
- [ ] **LMCache** (§3.6). Only if Phase 2 shows TTFT is prefill-bound and prefix
      caching is missing. CPU tier only, or encrypted volume, and add it to the
      retention register.
- [ ] Context extension to 16k/32k — the AWQ KV budget allows it; validate
      quality, since Qwen3 needs YaRN above 32k.
- [ ] Revisit INT8 W8A8 as a middle point if AWQ quality proves marginal.

### Phase 6 — Kubernetes and multi-card orchestration

**Out of scope. Owned by DevOps.** Our contribution is §4.2 (the constraints an
orchestrator must respect) and §4.4 (the isolation boundaries to draw first),
handed over as requirements. Nothing in Phases 0–5 depends on the orchestration
landing in any particular form.

---

## 8. Open questions

### Back to DevOps — new, and the reason for this revision

1. **Azure staging: is the SKU a full-card A10?** `NVadsA10_v5` sizes are SR-IOV
   partitions from 1/6 of a card up. Anything below the full 24 GiB frame buffer
   breaks the 8k context requirement silently — it will boot and serve, just with
   a third of the context. **This is the highest-priority question in this
   revision.** (§4.4a)
2. **Fault-domain placement.** DevOps asked whether VMs can be split across
   hardware nodes; the answer is fault domains, specified at launch, free. The
   two production VMs must be in different fault domains, and dev in a different
   one from production — otherwise "highly available" is nominal. (§4.3)
3. **Which environment is the performance reference?** Recommend stating
   explicitly that the OCI card is the capacity and throughput reference and
   Azure staging is application-integration only, so nobody reads a green Azure
   run as evidence the throughput target is met. (§4.4c)
4. **Has vLLM been validated on an Azure vGPU profile?** NVads is a GRID/RTX
   line with a different guest driver stack. Plausible, but unvalidated by us and
   not the well-trodden path. Needs a spike before it is trusted. (§4.4b)
5. **Production cloud** — when will it be decided? Everything portable in §4.4(d)
   is cheap insurance; a decision would let us stop paying it.

### Ours to answer

6. **C2** — streaming vs. post-hoc validators: which does the product prefer?
7. Is there a regulatory requirement for cryptographic isolation from the
   infrastructure operator? If so, the A10 cannot satisfy it (§5.4). Note that
   Azure staging adds a second operator to that question.
8. Does the eval set need Arabic coverage at the same depth as English? The app
   is bilingual throughout and Qwen3's Arabic quality at INT4 is unmeasured here.
9. Retention policy for gateway audit logs — `CONVERSATION_TTL_SECONDS` is 30
   days; should the audit trail match, or does compliance require longer?

---

## 9. Sources

- [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) · [Qwen3-8B-AWQ](https://huggingface.co/Qwen/Qwen3-8B-AWQ) · [Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF)
- [AngelSlim/Qwen3-8b-dflare](https://huggingface.co/AngelSlim/Qwen3-8b-dflare)
- [vLLM GGUF documentation](https://docs.vllm.ai/en/stable/features/quantization/gguf/) · [vllm-gguf-plugin](https://github.com/vllm-project/vllm-gguf-plugin)
- [TrueFoundry: Qwen3-8B vs Llama 3.1 8B vs Ministral 8B on a single A10](https://www.truefoundry.com/blog/vllm-benchmark) — FP16 A10 figures in §3.3
- [vLLM: Anatomy of a High-Throughput LLM Inference System](https://vllm.ai/blog/2025-09-05-anatomy-of-vllm)
- [Markaicode: vLLM INT4 throughput on A10G](https://markaicode.com/benchmarks/vllm-throughput-benchmark/) — 385 tok/s at batch 8
- [Inco AI: DFlash 2](https://inco.ai/blog/dflash2/) — related parallel-drafting technique, for contrast
- [OCI Compute shapes](https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm) · [OCI A10 GPU shapes announcement](https://blogs.oracle.com/cloud-infrastructure/announcing-nvidia-a10-gpu) — the §4.3 shape specifications
- [Azure NVadsA10_v5 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series) · [Azure NVads A10 v5 GA announcement](https://azure.microsoft.com/en-us/blog/choose-the-right-size-for-your-workload-with-nvads-a10-v5-virtual-machines-now-generally-available/) — the §4.4 partitioning risk (1/6 card, 4 GiB frame buffer, upward)
- DevOps response, 2026-09-04 — the answers folded into §4.1
