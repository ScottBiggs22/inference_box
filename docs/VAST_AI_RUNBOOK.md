# Rented A10 runbook (vast.ai)

**Status:** operational procedure for PRD §4.8
**Applies to:** Phase 1, on a personal rented GPU, before the DevOps VM lands
**Revised:** 2026-09-09 (rev 2 — see "What changed in rev 2" at the end)

---

## 0. The one rule

**The host is untrusted.** It is someone else's machine, and the operator can in
principle read anything inside the container. Everything below follows from
that, including the single biggest change in this revision:

> **The AI service does not go on the box. At all.**

The reason is not squeamishness, it is mechanical. The eval suite calls the real
`app.api.chat.chat_rag`, which retrieves from the real knowledge base and
injects the retrieved chunks into the prompt. So **real platform content travels
in the request body** regardless of how carefully the code got there — the
synthetic `chat_cases.json` queries are only the outer layer. Nothing needed
from this box requires the app: KV cache capacity, cold start, the throughput
curve and the B3 question are all answerable from this repo alone. Eval-set
quality belongs on the OCI card, which PRD §4.8 already names as the only place
numbers are allowed to come from anyway.

| Never goes on the box | Why |
|---|---|
| **The whole `baas-poc-templates` repo** | see above — the retrieval corpus is the problem, not just the logs |
| `logs/chat_interactions.log` | 1188 **plaintext** staff queries and answers |
| `data/pending_knowledge.json` | 40 records of verbatim extracted document text. **git-tracked** |
| `knowledge/.faiss_index.{json,bin}` | 182 verbatim knowledge chunks and their embeddings. **git-tracked** |
| A real `ANTHROPIC_API_KEY` or any production secret | third-party host |
| A real gateway API key or signing key | generate throwaway ones on the box |

| Safe to send | Why |
|---|---|
| `bkn301-inference` (this repo, in full) | contains no real data by construction, and `scripts/pack_for_box.sh` proves it per transfer |

### Why `git archive` alone was not the guarantee it looked like

Rev 1 argued the transfer was safe *mechanically*, because `git archive` ships
tracked files only and the sensitive paths are gitignored. The first half is
true. **The conclusion does not follow: tracked is not the same as safe.** An
audit on 2026-09-09 found that of the AI service's 2.4 MB tracked payload,
**1.2 MB was real platform content** — the two `data/` and `knowledge/` entries
in the table above. `data/` was not gitignored at all, so `git archive HEAD`
would have put all of it on a rented host, and `verify_box.sh` — which only
looked for `.env` and the interaction logs — would have said the box was clean.

So the transfer is now an **allowlist with a printed manifest**, the same
discipline the `.dockerignore` uses: gitignore stays the base mechanism, a
deny-list sits on top, and every file being shipped is listed before it is
packed. `tests/test_pack_for_box.py` plants those exact decoys and asserts the
pack refuses.

---

## 1. Book the box

Filter for:

| Requirement | Value | Why it matters |
|---|---|---|
| GPU | **A10, 1× exactly** | Not A10G, not L4 — see §3. More than one visible card makes the numbers' provenance ambiguous |
| VRAM | **24 GB** | Every figure in PRD §3.3 assumes a whole card |
| Disk | **≥ 30 GB** | ~6 GB weights + image layers + HF cache |
| Image | `vllm/vllm-openai:<pinned tag>` | avoids building vLLM. **Pin the tag** — see §4 |
| Port | expose one TCP port | for the SSH tunnel in §6 |

Roughly $0.20–0.50/hr at current listings — check before booking.

**Pick the image tag before you book**, and write it down. "latest" is not a
reproducible benchmark, and vLLM's CLI flags have moved across releases (§4).

---

## 2. Get the code up — one data-free tarball

From the laptop:

```bash
cd "/Users/scottbiggsbkn301aact/Desktop/BKN Code 2/bkn301-inference"

# Prints the full manifest, applies the deny-list, scans for credential-shaped
# material, and refuses to pack if anything trips. Read the manifest.
bash scripts/pack_for_box.sh /tmp/inference.tgz

scp -P <PORT> /tmp/inference.tgz root@<HOST>:/workspace/
```

`pack_for_box.sh` builds the archive from a **throwaway git index**, so
uncommitted work-in-progress travels while `.gitignore` still excludes `.env`,
`keys.json` and `logs/`. Your real index and working tree are never touched.
Run it with `--dry-run` first if you want to see the manifest without writing
anything.

**Do not substitute `scp -r` or `rsync` of the working tree**, and do not
`git clone` the app repo onto the box.

On the box:

```bash
cd /workspace && mkdir -p inference
tar xzf inference.tgz -C inference
```

---

## 3. Verify the box, and record the output

**Before pulling any weights.** Listings are frequently wrong, and a
partitioned or mislabelled card silently invalidates the entire memory budget.
Model weights are ~6 GB of download you do not want to pay for on a box you are
about to destroy.

```bash
cd /workspace/inference
bash scripts/verify_box.sh
```

It fails loudly on:

| Check | Why |
|---|---|
| exactly **one** GPU visible | the container assumes it owns the whole card (PRD §4.5) |
| the card is an **A10**, not A10G or L4 | A10G has different bandwidth and clocks — usable, but its numbers do not transfer to `VM.GPU.A10.1` |
| `nvidia-smi` reports **24 GiB** | a partitioned card breaks §3.3 |
| the card is idle | vLLM pre-allocates its KV cache; another tenant makes it fail at boot, not degrade |
| disk free ≥ 30 GB | weights + layers + cache |
| data hygiene | the `data/`, `knowledge/`, `logs/` and credential checks from §0 |

Driver and CUDA version are printed for recording — that is the minimum we hand
DevOps (Q17).

**Paste the whole output into `docs/PHASE1_RESULTS.md` §1 now**, not later.
DevOps sizes per-environment GPU counts off measured TPS, so the provenance of
every number matters.

If it fails, **destroy the box and book another one.** A benchmark from a
half-card A10 is worse than no benchmark, because it looks like data.

> `bash scripts/verify_box.sh --scan-only /workspace` re-runs just the hygiene
> checks. Use it after copying anything onto the box by hand.

---

## 4. Start vLLM

```bash
vllm serve Qwen/Qwen3-8B-AWQ \
  --revision 4da05a8edb55c6046cce958586c33b61da07bb79 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --disable-log-requests \
  --port 8000 \
  2>&1 | tee /workspace/vllm-boot.log
```

The revision is pinned to a real commit, not a placeholder: an unpinned model
tag is not a reproducible benchmark, and **the OCI card must be given the same
hash** or the two runs are not comparable. That commit was verified on
2026-09-09 as the current `main` of `Qwen/Qwen3-8B-AWQ` — 12 files, safetensors
only, no pickle, which is PRD §5.4 item 5 satisfied at the source.

**Check the flags against the image you pinned, once, before trusting them:**

```bash
vllm serve --help 2>&1 | grep -iE 'prefix-caching|log-requests|max-model-len'
```

`--enable-prefix-caching` is deliberately *absent* from the command above.
Automatic prefix caching is on by default in vLLM's V1 engine (PRD §3.6 relies
on this), and the flag's spelling and default have moved across releases — as
has `--disable-log-requests`. Record what `--help` actually says for your
pinned tag in `PHASE1_RESULTS.md` §2 rather than assuming either way; a flag
that no longer exists is a startup failure, and one that silently became a
no-op is worse.

### Record three things as it boots — all Phase 1 deliverables

**1. Cold-start time**, from process start to `/health` returning 200. This
sets `startupProbe.failureThreshold` in `deploy/k8s/probes.yaml`, a placeholder
today.

```bash
# In a second shell, started at the same moment as vllm serve:
start=$(date +%s)
until curl -sf http://127.0.0.1:8000/health >/dev/null; do sleep 1; done
echo "cold start: $(( $(date +%s) - start ))s"
```

**2. The reported KV cache size.** *This is the single highest-value number the
box exists for* — every capacity claim in PRD §3.3 is currently calculated and
never measured.

```bash
grep -iE 'kv cache|gpu blocks|graph captur|memory profil' /workspace/vllm-boot.log
```

Grep rather than a fixed string, because the log wording differs by version:
recent vLLM prints a "GPU KV cache size: N tokens" line, older builds print
"# GPU blocks: N". If you get blocks rather than tokens, multiply by the block
size (16 by default) and record both, so the cross-check is on the page.

The number to compare against is **~105,000 tokens** (PRD §3.3 config B).
Arithmetic re-derived from the real `config.json` on 2026-09-09: 36 layers × 8
KV heads × 128 head_dim × 2 × 2 bytes = 147,456 B = **144 KiB/token**, so a
14.8 GiB KV budget is **~107,770 tokens**. The PRD's figure is sound on paper.
What is unknown is how much of the 21.6 GiB vLLM actually leaves for KV after
weights, activations and CUDA graphs, and that is what this line measures.

**3. Memory actually used:**

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

## 6. Measure — on the box, not over the tunnel

**Run the load generator on the box.** This is the other significant change in
rev 2. Rev 1 ran the app's bench harness from the laptop through an SSH tunnel
and attached a caveat to every latency figure it produced. Two problems with
that, and both are fixed by moving the generator:

- **The tunnel inflated TTFT** by a round trip to a box that is not in-region,
  contaminating a number that is a procurement input.
- **The app harness cannot measure any of this.** It has no concurrency support
  at all — no flag, no threads, no asyncio — so "run 1/3/5/8 in parallel" was an
  instruction it could not carry out. It also never takes a first-token
  timestamp (its vllm backend hardcodes `stream: False`), and its `decode_tok_s`
  divides completion tokens by whole-request latency, conflating prefill with
  decode. PRD §3.3's ~48 tok/s/user is a decode figure; the two are not
  comparable.

```bash
# On the box. Levels 1/3/5/8, 8k-shaped context, through the gateway.
.venv/bin/python scripts/loadgen.py \
    --url http://127.0.0.1:8080/v1 \
    --api-key <the key from §5> \
    --ctx-tokens 7000 --max-tokens 512 \
    -o logs/loadgen_gateway.jsonl

# Again, straight at vLLM. The difference is the gateway's overhead.
.venv/bin/python scripts/loadgen.py \
    --url http://127.0.0.1:8000/v1 --direct \
    --ctx-tokens 7000 --max-tokens 512 \
    -o logs/loadgen_direct.jsonl
```

`--ctx-tokens 7000` matters. The exit gate is "≥20 tok/s/user at 5 concurrent
**with 8k context**", and five short prompts do not test it — they test a card
with an almost-empty KV cache. 7000 in + 512 out is the 8k shape; the script
refuses to start if context plus output would exceed `--max-model-len`.

It reports **per-user decode tok/s at each level** alongside the aggregate,
plus TTFT p50/p95. Both matter and they answer different questions: per-user
falls with concurrency, aggregate rises as continuous batching does its job.
R1 is about the per-user figure.

Add `--unique-prefix` for a worst-case run with prefix caching defeated. The
default deliberately models real traffic instead: a large byte-identical
prefix and a short unique query, which is what the FirstData system prompt plus
knowledge block looks like (PRD §3.6).

### Then the B3 question

```bash
.venv/bin/python scripts/b3_probe.py \
    --url http://127.0.0.1:8080/v1 --api-key <key> \
    -o logs/b3_gateway.jsonl

.venv/bin/python scripts/b3_probe.py \
    --url http://127.0.0.1:8000/v1 --direct \
    -o logs/b3_direct.jsonl
```

It runs a seven-case matrix and prints a verdict on the open question from PRD
§6.1 B3: **does a user typing `/think` override the request-level
`enable_thinking: false`?** It sends the exact wire payload the app builds —
a top-level `chat_template_kwargs`, not nested under `extra_body`.

**Run it twice, gateway and direct, and compare.** If the two disagree, the
gateway is dropping `chat_template_kwargs` in passthrough, which would break
B3's fix in production while every local test stayed green.

### And capture vLLM's own metrics

```bash
curl -s http://127.0.0.1:8000/metrics | grep -E 'prefix|cache|num_requests|generation_tokens'
```

This gives the prefix-cache hit rate PRD §7 Phase 2 asks for, and a real
reading of the four gauges the wedge-detection design in `PROBE_CONTRACT.md` §4
depends on.

---

## 7. Before you destroy the box

Pull everything off; it is not recoverable afterwards.

```bash
scp -P <PORT> root@<HOST>:/workspace/inference/logs/*.jsonl ./results/
scp -P <PORT> root@<HOST>:/workspace/vllm-boot.log ./results/
```

Checklist — every line maps to a slot in `docs/PHASE1_RESULTS.md`:

- [ ] `verify_box.sh` output recorded verbatim (driver, CUDA, card, VRAM)
- [ ] `vllm serve --help` flag reality for the pinned tag
- [ ] Cold-start time → `startupProbe.failureThreshold`
- [ ] **vLLM's reported KV cache size — validates or refutes §3.3's ~105,000**
- [ ] `nvidia-smi` memory used after load
- [ ] tok/s per user at 1 / 3 / 5 / 8 concurrent, at 8k-shaped context
- [ ] TTFT p50/p95 — now measured on-box, so **not** tunnel-inflated
- [ ] Gateway overhead (gateway run vs `--direct` run)
- [ ] B3 matrix, incl. whether `/think` overrides `enable_thinking: false`
- [ ] B3 gateway-vs-direct comparison (does passthrough preserve the kwargs?)
- [ ] Prefix-cache hit rate from vLLM `/metrics`
- [ ] Throwaway gateway key discarded

Then **destroy the instance.** A stopped vast.ai instance still holds its disk.

---

## 8. What this box is not good for

Stated explicitly so nobody over-reads the results:

- **Numbers handed to procurement.** A vast.ai host has different CPU, PCIe
  topology and memory bandwidth from `VM.GPU.A10.1`. These are a **provisional
  baseline to be re-confirmed on the OCI card** before anyone multiplies them.
  `loadgen.py` writes that warning into its own output header so the caveat
  survives being pasted into a spreadsheet.
- **Eval-set quality.** Needs the app, which needs the real knowledge corpus.
  OCI card only.
- **The Phase 2 security review**, which has to run against the real deployment
  path.
- **Latency figures that include the tunnel.** Not a problem any more for the
  numbers above, because the generator runs on the box — but it still applies to
  anything driven from the laptop.

---

## What changed in rev 2

Rev 1 was written before the code it describes was audited against it. Five of
its instructions did not survive that:

1. **The app-repo transfer shipped the wrong tree.** `git archive HEAD:...`
   predated every uncommitted Phase 0 change — including untracked
   `openai_http.py`, the whole vllm backend and B3 fix. A box built from it had
   no `--backend vllm`, so the benchmark could not run and B3 could not be
   confirmed. Resolved by not transferring the app at all (§0).
2. **The "tracked files only" safety argument had a 1.2 MB hole** (§0).
3. **The bench instructions could not be carried out** — no concurrency, no
   TTFT, and a `decode_tok_s` that was not a decode rate (§6).
4. **The tokenizer caveat repeated a claim the PRD had already retracted.** Rev
   1 warned that `TOKENIZER_DIR` still pointed at Qwen2.5 and that its vocab
   "differs from Qwen3's 151,552". Both halves were false: PRD §6.1 B4 records
   both tokenizers reporting **151643** with byte-identical IDs across all 349
   distinct strings, and `config.py` now selects the Qwen3 tokenizer from
   `LLM_BACKEND`. The caveat is deleted rather than corrected, because with the
   app off the box there are no app-side token counts to caveat.
5. **`verify_box.sh` would have hard-failed on a false positive.** Its
   credential scan was `grep -rl 'sk-ant-'`, and `tests/test_auth.py`
   deliberately contains `"sk-ant-something"` as a malformed-credential fixture
   — so the first command of the session exited 1 saying an Anthropic key was
   present, under a banner reading "do not benchmark this box". The scan is now
   shape-aware and has a `--self-test`.

Also fixed: §3 used to say run `verify_box.sh` "before anything else" while the
script only arrived with the §3 transfer, and the retrieval step used to pull
bench output off the box that rev 1 had written on the laptop.
