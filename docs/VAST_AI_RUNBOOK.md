# Rented A10 runbook (vast.ai)

**Status:** operational procedure for PRD §4.8
**Applies to:** Phase 1, on a personal rented GPU, before the DevOps VM lands

---

## 0. The one rule

**The host is untrusted.** It is someone else's machine, and the operator can in
principle read anything inside the container. Everything below follows from that.

| Never goes on the box | Why |
|---|---|
| `logs/chat_interactions.log` | 1188 **plaintext** staff queries and answers |
| `logs/token_stats.jsonl`, any real `logs/` content | same class of data |
| A real `ANTHROPIC_API_KEY` or any production secret | third-party host |
| A real gateway API key or signing key | generate throwaway ones on the box |
| `data/`, `.env` | runtime state and secret slot |

| Safe to send | Why |
|---|---|
| `bkn301-inference` (the whole repo) | contains no real data by construction |
| `scripts/bench_fixtures/` | fully synthetic — invented Riyadh-themed entities |
| `scripts/bench_llm_backends.py`, `app/` | code |

The transfer method in §3 enforces this mechanically rather than by care:
`git archive` ships **tracked files only**, and `logs/*`, `.env`, `models/*.gguf`
and the venvs are all gitignored. Do not substitute `scp -r` or `rsync` of the
working tree — those would carry the untracked real data straight up.

---

## 1. Book the box

Filter for:

| Requirement | Value | Why it matters |
|---|---|---|
| GPU | **A10, 1×** | Not A10G, not L4 — see §2 |
| VRAM | **24 GB** | Every figure in PRD §3.3 assumes a whole card |
| Disk | **≥ 30 GB** | ~6 GB weights + image layers + HF cache |
| Image | `vllm/vllm-openai:latest` or a CUDA 12.x base | avoids building vLLM |
| Port | expose one TCP port | for the SSH tunnel in §6 |

Roughly $0.20–0.50/hr at current listings — check before booking.

---

## 2. First boot: verify, and record the output

**Do this before anything else.** Listings are frequently wrong, and a
partitioned or mislabelled card silently invalidates the entire memory budget.

```bash
# on the box
bash verify_box.sh          # from scripts/, see §3
```

It checks, and fails loudly on:

| Check | Why |
|---|---|
| `nvidia-smi` reports **24 GiB on one device** | a partitioned card breaks §3.3 |
| The card is an **A10**, not A10G or L4 | A10G has different bandwidth and clocks — usable, but the throughput numbers would not transfer to procurement |
| Driver + CUDA version | this is the minimum we hand DevOps (Q17) |
| Disk free ≥ 30 GB | weights + layers + cache |

**Paste the output into the Phase 1 notes.** DevOps is sizing per-environment GPU
counts off measured TPS, so the provenance of every number matters.

---

## 3. Get the code up

From the laptop:

```bash
cd "/Users/scottbiggsbkn301aact/Desktop/BKN Code 2"

# The gateway repo — safe in its entirety.
git -C bkn301-inference archive --format=tar.gz -o /tmp/inference.tgz HEAD

# The AI service — TRACKED FILES ONLY. This is what keeps logs/, .env,
# models/ and the venvs on the laptop.
git -C baas-poc-templates archive --format=tar.gz -o /tmp/aiservice.tgz \
    HEAD:services/bkn301-ai-service

scp -P <PORT> /tmp/inference.tgz /tmp/aiservice.tgz root@<HOST>:/workspace/
```

Verify on the box **before** running anything:

```bash
cd /workspace && mkdir -p inference aiservice
tar xzf inference.tgz -C inference && tar xzf aiservice.tgz -C aiservice

# Must all print nothing.
find . -name '.env' -o -name 'chat_interactions.log' -o -name '*.gguf'
grep -rl 'sk-ant-' . 2>/dev/null
```

If any of those print a path, **stop and delete the box**.

---

## 4. Start vLLM

```bash
# Pin the revision. An unpinned model tag is not a reproducible benchmark.
vllm serve Qwen/Qwen3-8B-AWQ \
  --revision <COMMIT_HASH> \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --enable-prefix-caching \
  --disable-log-requests \
  --port 8000
```

**Record two things as it boots** — both are Phase 1 deliverables:

1. **Cold-start time**, from process start to `/health` returning 200. This sets
   `startupProbe.failureThreshold` in `deploy/k8s/probes.yaml`.
2. **The reported KV cache blocks**, from the startup log. This is the number
   that validates or refutes PRD §3.3's ~105,000-token AWQ budget — currently
   *calculated, not measured*, and the single highest-value thing this box is for.

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

---

## 5. Start the gateway

```bash
cd /workspace/inference
python3.12 -m venv .venv && .venv/bin/pip install -e .

# THROWAWAY credentials. Generated on the box, discarded with it.
export API_KEY_PEPPER="throwaway-$(openssl rand -hex 8)"
export UPSTREAM_URLS=http://127.0.0.1:8000/v1
export ALLOWED_MODELS=Qwen/Qwen3-8B-AWQ

.venv/bin/python -c "
from gateway.auth.keys import mint_key, KeyStore
s = KeyStore('keys.json'); pt, rec = mint_key(scopes=['chat'], label='vastai')
s.add(rec); s.save(); print(pt)"        # <- note this key, it is shown once

.venv/bin/python -m uvicorn gateway.main:app --host 127.0.0.1 --port 8080
```

Bind **127.0.0.1**, not `0.0.0.0`. Reach it through the tunnel in §6 — do not
publish an auth surface to the open internet from a rented host.

---

## 6. Benchmark from the laptop

Tunnel, so nothing is exposed publicly:

```bash
ssh -p <PORT> -N -L 8080:127.0.0.1:8080 root@<HOST>
```

Then, on the laptop:

```bash
cd "/Users/scottbiggsbkn301aact/Desktop/BKN Code 2/baas-poc-templates/services/bkn301-ai-service"

export VLLM_BASE_URL=http://127.0.0.1:8080/v1
export VLLM_MODEL=Qwen/Qwen3-8B-AWQ
export VLLM_API_KEY=<the key from §5>

.venv/bin/python scripts/bench_llm_backends.py \
    --backend vllm --suite chat --repeat 5 \
    -o logs/bench_vllm_a10.jsonl
```

**Two caveats on the numbers this produces:**

- **Latency includes the tunnel.** The box is not in-region. TTFT and wall-clock
  figures are inflated; tok/s during decode is much less affected. Report which
  is which.
- **Token counts are estimates.** `TOKENIZER_DIR` still points at the Qwen2.5
  tokenizer (PRD §6.1 B4), whose vocab differs from Qwen3's 151,552. Chunk
  budgets and token counts are wrong until it is re-vendored.

For the concurrency curve, run 1/3/5/8 in parallel and report **tok/s per user at
each level**, not one aggregate — that is the shape procurement needs.

---

## 7. Before you destroy the box

Pull everything off; it is not recoverable afterwards.

```bash
scp -P <PORT> root@<HOST>:/workspace/inference/logs/*.jsonl ./results/
```

Checklist:

- [ ] `verify_box.sh` output recorded (driver, CUDA, card, VRAM)
- [ ] Cold-start time recorded
- [ ] vLLM's reported KV cache blocks recorded — **validates or refutes §3.3**
- [ ] tok/s per user at 1 / 3 / 5 / 8 concurrent
- [ ] TTFT p50/p95, flagged as tunnel-inflated
- [ ] Qwen3 thinking-mode behaviour confirmed (B3), incl. whether a user typing
      `/think` overrides `enable_thinking: false`
- [ ] Throwaway gateway key discarded

Then **destroy the instance.** A stopped vast.ai instance still holds its disk.

---

## 8. What this box is not good for

Stated explicitly so nobody over-reads the results:

- **Numbers handed to procurement.** A vast.ai host has different CPU, PCIe
  topology and memory bandwidth from `VM.GPU.A10.1`. These are a **provisional
  baseline to be re-confirmed on the OCI card** before anyone multiplies them.
- **The Phase 2 security review**, which has to run against the real deployment
  path.
- **Latency figures that include network**, since the box is not in-region.
