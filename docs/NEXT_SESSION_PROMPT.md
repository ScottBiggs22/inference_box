# Fresh-session prompt

**Updated 2026-09-11**, after Phase 1 completed on a rented A10.

Copy the block below into a new Claude Code session started **from this repo**:

```bash
cd "/Users/scottbiggsbkn301aact/Desktop/BKN Code 2/bkn301-inference"
claude
```

---

I'm building the inference stack for FirstData: a secure OpenAI-compatible
gateway in front of self-hosted vLLM running Qwen3-8B-AWQ on a single 24 GiB
NVIDIA A10. This repo is the gateway and the deployment artefacts.

Read these three first, in order:

- `docs/HANDOFF.md` — current state. Read this before anything else. §5b is the
  Phase 1 measurement session; §6 is what is deliberately not built yet and
  what Phase 1 changed about each item.
- `docs/PHASE1_RESULTS.md` — the measurements, with raw artefacts in
  `results/phase1-vastai-a10/`. §8 lists ten corrections the measurements forced
  into the PRD.
- `docs/INFERENCE_SERVICE_PRD.md` — rev 3, the design source of truth. It carries
  the feasibility analysis, memory budget, security design, the DevOps deployment
  contract (§4), probe design (§4.6), rented-box rules (§4.8) and the phase plan
  (§7). **Don't re-derive any of it.** §3.1, §3.3, §3.6, §4.5, §5.4, §6.1 B3/B4
  and §7.1 carry in-place corrections — several of them measured on real
  hardware. **Trust the corrections over the original claims.**

## Where things stand

**Phase 0 and Phase 1 are complete. The core capacity requirements are met and
measured, not calculated.** On a full-card A10:

| Requirement | Result |
|---|---|
| R1 ≥20 output tok/s per user | **MET at 50.9 tok/s/user** at 5 concurrent, 8k context — 2.54× |
| R2 3–5 concurrent | **MET** — measured at 1/3/5/8 |
| R4 4k min / 8k goal | **MET** — 92,544 tokens of KV, 2.26× headroom for 5×8k |
| Cold start → probe config | **Measured** 45 s warm / 153 s cold; `failureThreshold` 24 → 60 |

PRD §3.3's KV budget was 12% optimistic (92,544 measured vs ~105,000
calculated) but its conclusion held, and its throughput projection was accurate
within 6%. B3 is fully resolved on real Qwen3: `chat_template_kwargs` beats the
`/think` soft switch in both directions, so no input sanitisation is needed.

**Two things that "capacity met" does NOT yet cover, so nobody over-reads it:**

1. **Quality is unmeasured.** Eval-set parity vs BF16 was deliberately not run
   on the rented box — it needs the app, which needs the real knowledge corpus,
   which must not reach a third-party host. That runs on the OCI card.
2. **These are rented-box numbers.** DevOps sizes GPU counts off measured TPS,
   and a vast.ai host has different CPU, PCIe topology and memory bandwidth from
   `VM.GPU.A10.1`. Re-confirm on the OCI card before anyone multiplies them.
   (The *capacity* figures in §3 of the results transfer — KV cache is a property
   of card, model and vLLM accounting, not of the host.)

**Test/build state:** 108 tests here, ruff clean, both image targets build,
compose stack verified end to end. 179 tests in the app repo; its three builds
were green as of Phase 0 and it has not been touched since.

The consuming app is a **sibling** repo, not a subdirectory:
`../baas-poc-templates/services/bkn301-ai-service`. Its Phase 0 changes are
uncommitted on purpose; its working remote is `raghav`, not `origin`.

## The work now, in order

Capacity is settled, so the remaining work is **logging, robust handling, and
security** — then connecting the gateway to the FirstData app for a full test.
That maps onto PRD §7 Phase 2 and Phase 3. `docs/HANDOFF.md` §6 has the seams,
the file:line for each, and what Phase 1 measured that feeds into it.

1. **Logging / observability — `/metrics`.** First because the breaker and the
   wedge detection both consume it. `prometheus-client` is already a dependency
   with no endpoint. Keep `keyid` out of the labels (unbounded cardinality, and
   it puts tenant identity on an unauthenticated scrape surface). The real vLLM
   series names are captured from the live server in
   `results/phase1-vastai-a10/vllm-metrics-final.txt` — write against those, not
   against the doc.
2. **Robust handling — circuit breaker + health-aware routing.**
   `UpstreamPool.pick()` is round-robin and `health()` is unused by the router.
   `stub/server.py --stub-fail-after N` exists for this test. **The
   wedge-detection window is still unmeasured** — Phase 1 never wedged the
   engine naturally, so `PROBE_CONTRACT.md` §4's 120s is still an estimate, and
   inducing a wedge deliberately is how to settle it.
3. **Security — per-key token budgets, concurrency caps, then mTLS.** The
   `ApiKeyRecord` fields exist and nothing enforces them.
   `stream_options.include_usage` is now confirmed working end to end through
   the SSE passthrough against real vLLM, so real streamed token counts are
   wiring rather than research. Note the measured cost: argon2id verification is
   **~77 ms per request**, fine in front of a 10 s generation but worth knowing
   before designing a cap that verifies-then-rejects.
4. **Then Phase 3 — connect to the FirstData app for a full test.** This is
   where the gateway stops being tested against a stub. Expect PRD §6.3 C1
   (sampling param mapping and re-tuned `numeric_guard` / `response_validator`
   thresholds), C3–C5, and C6. Note `openai_http.py:177` hardcodes
   `"stream": False`, so the app has no streaming path and no TTFT at all —
   that is C2's missing plumbing, not merely a product decision.

## Hard constraints

- **No cloud-specific SDK in the container.** Production cloud is undecided (OCI
  or Azure); config is env vars only. `tests/test_config.py` enforces this.
- **The container assumes it owns one whole 24 GiB GPU.** Never a fraction — vLLM
  pre-allocates its KV cache, so a second pod on the card fails at boot. Phase 1
  sharpened why: the real KV budget on a full card is 12.71 GiB, so a half-card
  Azure `NVadsA10_v5` would have ~4 GiB ≈ 28,000 tokens and **5×8k becomes
  unreachable**.
- **A rented box is untrusted.** Synthetic prompts only, throwaway keys only, and
  the app repo does not go on one at all — its eval suite retrieves from the real
  knowledge base and injects the chunks into the prompt, so real content travels
  in the request body regardless of how the code got there. Use
  `scripts/pack_for_box.sh`, which prints its manifest and fails closed; never
  `scp -r` or `rsync` of a working tree.
- **The audit log is metadata only.** Never prompt or completion text. Verified
  against 78 real requests in Phase 1 — keep it that way.
- **No push or merge without my approval.** Local commits in this repo are fine.
- Validate builds per the app repo's `CLAUDE.md` when touching it: `dotnet
  build`, `npm run build`, `npx tsc --noEmit` — 0 errors each.

## Two things I'd rather you did than didn't

**Tell me when a documented claim doesn't survive contact with the code or the
hardware.** This has paid off every session: three PRD claims died in Phase 0,
six more plus the whole transfer procedure in the toolkit build, and ten in
Phase 1 — including a `vllm serve` flag the PRD named in three places that does
not exist in vLLM 0.28.0 and would have failed at startup, and a `startupProbe`
threshold that would have boot-looped a cold pod. Building on any of them would
have produced no-op changes and a false "done".

**Report failures as failures.** Three pre-existing eval cases in the app repo
are red from a model downgrade and were deliberately left that way rather than
relaxed. The wedge-detection window is still an estimate and is labelled as one.
Don't tidy either away.

## Still blocking, and not ours

**PRD §8 Q1 — is Azure staging a full-card A10?** `NVadsA10_v5` starts at
one-sixth of a card with a 4 GiB frame buffer, and a fractional SKU boots,
serves, and silently breaks the 8k context requirement. Phase 1 put a measured
number behind the risk (see the constraint above). Highest-priority question
outstanding with DevOps, along with fault-domain placement (§4.3) and confirming
the OCI card — not Azure staging — is the performance reference (§4.4c).
