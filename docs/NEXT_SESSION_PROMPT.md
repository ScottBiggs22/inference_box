# Fresh-session prompt

Copy the block below into a new Claude Code session started **from this repo**:

```bash
cd "/Users/scottbiggsbkn301aact/Desktop/BKN Code 2/bkn301-inference"
claude
```

---

I'm building the inference stack for FirstData: a secure OpenAI-compatible
gateway in front of self-hosted vLLM running Qwen3-8B-AWQ on a single 24 GiB
NVIDIA A10. This repo is the gateway and the deployment artefacts.

Read these two first, in order:

- `docs/HANDOFF.md` — current state. Read this before anything else; it records
  what is done, three PRD claims that turned out to be wrong, and four app
  defects found during eval calibration.
- `docs/INFERENCE_SERVICE_PRD.md` — rev 3, the design source of truth. It carries
  the feasibility analysis, memory budget, security design, the DevOps deployment
  contract (§4), probe design (§4.6), rented-box rules (§4.8) and the phase plan
  (§7). **Don't re-derive any of it.** §6.2, §6.1 B4 and §7.1 already carry
  in-place corrections from the last session — trust the corrections over the
  original claims.

Phase 0 is complete. 58 tests here, 179 in the app repo, ruff clean, all three
app builds green. Phase 1 is next and needs a GPU.

The consuming app is a **sibling** repo, not a subdirectory:
`../baas-poc-templates/services/bkn301-ai-service`. Its Phase 0 changes are
uncommitted on purpose; its working remote is `raghav`, not `origin`.

Immediate work, in order:

1. Walk me through booking a vast.ai A10 and getting this running on it, per
   `docs/VAST_AI_RUNBOOK.md`. `scripts/verify_box.sh` runs first and its output
   gets recorded — a partitioned card or an L4 invalidates the whole memory
   budget.
2. Capture the Phase 1 measurements, in priority order: vLLM's **reported KV
   cache blocks** (this confirms or refutes PRD §3.3's ~105k-token AWQ budget,
   which is currently calculated and never measured — it is the highest-value
   thing the box is for), then **cold-start time** (sets
   `startupProbe.failureThreshold`, a placeholder today), then **tok/s per user
   at 1/3/5/8 concurrent**.
3. Confirm B3 on real Qwen3 — specifically whether a user typing `/think`
   overrides the request-level `enable_thinking: false`.
4. Then Phase 2 gateway work: per-key token budgets and concurrency caps,
   circuit breaker and health-aware routing, mTLS, `/metrics`. The seams for all
   four already exist — see `docs/HANDOFF.md` §6.

Hard constraints:

- **No cloud-specific SDK in the container.** Production cloud is undecided (OCI
  or Azure); config is env vars only. `tests/test_config.py` enforces this.
- **The container assumes it owns one whole 24 GiB GPU.** Never a fraction — vLLM
  pre-allocates its KV cache, so a second pod on the card fails at boot.
- **A rented box is untrusted.** Synthetic prompts only, throwaway keys only,
  and never `logs/chat_interactions.log` — it holds 1188 plaintext staff queries.
  The runbook's `git archive` transfer enforces this mechanically; don't
  substitute `scp -r` or `rsync` of the working tree.
- **The audit log is metadata only.** Never prompt or completion text.
- **No push or merge without my approval.**
- Validate builds per the app repo's `CLAUDE.md` when touching it: `dotnet
  build`, `npm run build`, `npx tsc --noEmit` — 0 errors each.

Two things I'd rather you did than didn't: tell me when a PRD claim doesn't
survive contact with the code (three didn't last session, and building on them
would have produced no-op changes and a false "done"), and report failures as
failures — three pre-existing eval cases are red from a model downgrade and were
deliberately left that way rather than relaxed.

Circle back with clarifying questions before starting.
