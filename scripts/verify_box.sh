#!/usr/bin/env bash
# First-boot verification for a rented GPU box. See docs/VAST_AI_RUNBOOK.md §2.
#
# Run this BEFORE pulling any weights. Listings are frequently wrong, and every
# capacity figure in INFERENCE_SERVICE_PRD.md §3.3 assumes a whole 24 GiB A10.
# A partitioned card, an A10G, or an L4 will all boot and serve happily while
# silently invalidating the memory budget and the throughput targets.
#
# Exits non-zero if any hard requirement fails. Paste the output into
# docs/PHASE1_RESULTS.md -- DevOps sizes per-environment GPU counts off these
# numbers, so their provenance matters.
#
#   bash scripts/verify_box.sh                full check, on the box
#   bash scripts/verify_box.sh --self-test    plant decoys and assert the
#                                             hygiene scan catches each one;
#                                             runs on a laptop, no GPU
#   bash scripts/verify_box.sh --scan-only DIR
#                                             hygiene scan against DIR only.
#                                             Re-run this after copying
#                                             anything onto the box by hand.
#
# WHAT CHANGED ON 2026-09-09, AND WHY
# ===================================
# Two defects, both found by auditing this file against what actually ends up
# on the box:
#
#   1. The credential scan was `grep -rl 'sk-ant-'` with the script itself
#      excluded by filename. But `tests/test_auth.py` deliberately contains the
#      string "sk-ant-something" as a malformed-credential fixture -- so the
#      moment this repo is on the box, the scan matched its own test suite and
#      the script exited 1 with "something matching an Anthropic key is
#      present". Step one of the session hard-failed on a false positive. The
#      scan is now SHAPE-aware: a real key has 20+ characters of key material
#      after the prefix, so matching the shape finds credentials and ignores
#      documentation and fixtures, with no filename exclusions to maintain.
#
#   2. The hygiene scan looked only for `.env`, `chat_interactions.log` and
#      `token_stats.jsonl`. The audit found the real leak was elsewhere:
#      `data/pending_knowledge.json` (40 records of verbatim extracted document
#      text) and `knowledge/.faiss_index.json` (182 verbatim chunks) are
#      git-TRACKED, so the old `git archive` transfer shipped them and this
#      check waved them through. Tracked is not the same as safe.

set -uo pipefail

fail=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }

# ─────────────────────────────────────────────────────────────────────────────
# Data hygiene, factored out so --self-test can exercise it without a GPU.
# A check that has never been seen to fail is not known to work.
# ─────────────────────────────────────────────────────────────────────────────
hygiene_scan() {
  local root="$1"

  # A scan of a directory that does not exist finds nothing and reports clean.
  # That is not a pass, it is a vacuous check -- and it very nearly shipped:
  # the default target was /workspace, which the vllm/vllm-openai image does
  # not have (its home is /root), so on the first real box this function would
  # have printed three PASS lines having examined no files at all.
  if [ ! -d "$root" ]; then
    bad "scan target '$root' does not exist -- nothing was checked. Set WORKSPACE to the directory the payload was extracted into."
    return
  fi

  # Paths that must never exist on an untrusted host. Each entry earns its
  # place from something that was actually found in the payload.
  local leaks
  leaks=$(find "$root" -maxdepth 6 \( \
        -name '.env' \
     -o -name 'chat_interactions.log' \
     -o -name 'token_stats.jsonl' \
     -o -name 'vast_api_key' \
     -o -name '*api_key*' \
     -o -name 'pending_knowledge.json' \
     -o -name 'pending_sessions.json' \
     -o -name '.faiss_index.*' \
     -o -name '*.gguf' \
     -o -name 'qwen3-tokenizer' \
     -o -name 'qwen2.5-tokenizer' \
     \) 2>/dev/null)
  # Separate the host platform's own injected credentials from anything WE
  # put here. vast.ai writes /root/.vast_api_key into every instance so the
  # container can call its API (self-destruct, and so on). Failing the gate on
  # it would make the gate un-passable on vast.ai, and a gate that always fails
  # is one people learn to skip -- the same defect as a gate that always passes.
  #
  # It is reported rather than ignored, because it IS a credential on an
  # untrusted host and the reader should know it is there. What makes it
  # tolerable is that it is instance-scoped: verified on 2026-09-10 that its
  # hash differs from the account key in ~/.config/vastai/vast_api_key, so a
  # host operator reading it gets control of this contract, not the account.
  # If that ever stops being true, this comment is the thing to re-check.
  local platform_re='(^|/)\.vast_api_key$|(^|/)\.vast_containerlabel$'
  local injected our_leaks
  injected=$(printf '%s' "$leaks" | grep -E "$platform_re" || true)
  our_leaks=$(printf '%s' "$leaks" | grep -vE "$platform_re" || true)

  if [ -n "$injected" ]; then
    warn "host-injected credentials present (expected on vast.ai, instance-scoped):"
    printf '%s\n' "$injected" | sed 's/^/        /'
    echo "        ^ dies with the instance. Confirm it is NOT your account key:"
    echo "          laptop: shasum -a 256 < ~/.config/vastai/vast_api_key"
    echo "          box:    sha256sum < /root/.vast_api_key"
  fi

  if [ -z "$our_leaks" ]; then
    pass "no real-data artefacts present"
  else
    bad "REAL DATA ON AN UNTRUSTED HOST -- delete this box:"
    printf '%s\n' "$our_leaks" | sed 's/^/        /'
  fi

  # A whole knowledge/ directory is the AI service's retrieval corpus. Its
  # presence means the app repo was transferred, which the runbook no longer
  # does: the eval suite injects retrieved chunks into the prompt, so real
  # content travels in the request body regardless of how the code arrived.
  local corpus
  corpus=$(find "$root" -maxdepth 6 -type d -name 'knowledge' 2>/dev/null)
  if [ -n "$corpus" ]; then
    bad "a knowledge/ corpus directory is present -- the AI service must not be here:"
    printf '%s\n' "$corpus" | sed 's/^/        /'
  else
    pass "no retrieval corpus present"
  fi

  # SHAPE-aware credential scan. See the header for why the old prefix match
  # failed on this repo's own test fixtures.
  #
  # Build artefacts are excluded, and not merely as noise reduction. A test
  # fixture written as `"sk-ant-api03-" + "A"*40` is safe in SOURCE -- the
  # prefix is followed by a quote, not key material -- but the compiler folds
  # the concatenation, so the .pyc contains the assembled shape and matches.
  # Scanning bytecode therefore reports fixtures as credentials while telling
  # you nothing a source scan does not.
  local creds
  creds=$(LC_ALL=C grep -rlE \
      --exclude-dir=__pycache__ --exclude-dir=.git --exclude-dir='.venv*' \
      --exclude-dir=node_modules --exclude='*.pyc' \
      'sk-ant-[A-Za-z0-9_-]{20,}|bkn_live_[0-9a-f]{12}_[A-Za-z0-9_-]{30,}' \
      "$root" 2>/dev/null)
  if [ -z "$creds" ]; then
    pass "no credential-shaped material found"
  else
    bad "credential-shaped material present:"
    printf '%s\n' "$creds" | sed 's/^/        /'
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# --self-test: plant each decoy and assert the scan catches it.
# ─────────────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--scan-only" ]; then
  target="${2:-${WORKSPACE:-/workspace}}"
  echo "=== Data hygiene (PRD §4.8) — $target ==="
  hygiene_scan "$target"
  echo
  if [ "$fail" -eq 0 ]; then
    echo "Hygiene scan clean."
  else
    echo "HYGIENE SCAN FAILED. Remove the above before continuing."
  fi
  exit "$fail"
fi

if [ "${1:-}" = "--self-test" ]; then
  echo "=== verify_box.sh self-test (no GPU needed) ==="
  echo
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT

  echo "-- a clean tree must PASS --"
  mkdir -p "$tmp/clean/tests"
  echo 'BAD = ["sk-ant-something", "bkn_live_abc", "bkn_live_"]' > "$tmp/clean/tests/test_auth.py"
  echo 'keys look like bkn_live_<keyid>_<secret>' > "$tmp/clean/README.md"
  echo 'GATEWAY_PORT=8080' > "$tmp/clean/.env.example"
  fail=0; hygiene_scan "$tmp/clean"
  if [ "$fail" -eq 0 ]; then
    printf '  \033[32mOK\033[0m    clean tree passed (fixtures did not false-positive)\n'
    self_fail=0
  else
    printf '  \033[31mBROKEN\033[0m self-test: a clean tree failed the scan\n'
    self_fail=1
  fi
  echo

  echo "-- each decoy must FAIL --"
  self_fail=${self_fail:-0}
  for decoy in \
      'data/pending_knowledge.json' \
      'knowledge/.faiss_index.json' \
      'logs/chat_interactions.log' \
      'logs/token_stats.jsonl' \
      '.env' \
      'models/model-q4.gguf' \
      'models/qwen3-tokenizer/tokenizer.json' \
      'config/service_api_key' ; do
    d="$tmp/decoy-$(echo "$decoy" | tr '/.' '__')"
    mkdir -p "$d/$(dirname "$decoy")"
    echo 'real content' > "$d/$decoy"
    fail=0
    # NOT `out=$(hygiene_scan ...)`: command substitution runs the function in
    # a SUBSHELL, so the `fail=1` it sets is discarded and every decoy reads as
    # missed. The self-test caught this on its first run, which is the argument
    # for having it.
    hygiene_scan "$d" >/dev/null
    if [ "$fail" -ne 0 ]; then
      printf '  \033[32mOK\033[0m    caught %s\n' "$decoy"
    else
      printf '  \033[31mMISSED\033[0m %s -- the scan let it through\n' "$decoy"
      self_fail=1
    fi
  done

  d="$tmp/decoy-cred"
  mkdir -p "$d"
  printf 'KEY = "sk-ant-api03-%s"\n' "$(printf 'A1b2C3d4E5f6G7h8I9j0%.0s' 1 2)" > "$d/cfg.py"
  fail=0
  hygiene_scan "$d" >/dev/null
  if [ "$fail" -ne 0 ]; then
    printf '  \033[32mOK\033[0m    caught a real-shaped Anthropic key\n'
  else
    printf '  \033[31mMISSED\033[0m a real-shaped Anthropic key\n'
    self_fail=1
  fi

  echo
  if [ "$self_fail" -eq 0 ]; then
    echo "Self-test passed: the hygiene scan catches every planted decoy and"
    echo "does not trip on the repo's own credential fixtures."
    exit 0
  fi
  echo "SELF-TEST FAILED. Do not rely on this script's hygiene verdict."
  exit 1
fi

# ─────────────────────────────────────────────────────────────────────────────
# Full check, on the box.
# ─────────────────────────────────────────────────────────────────────────────
echo "=== Rented box verification ==="
echo

if ! command -v nvidia-smi >/dev/null 2>&1; then
  bad "nvidia-smi not found -- this box has no usable NVIDIA driver"
  exit 1
fi

# ── GPU count ────────────────────────────────────────────────────────────────
count=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')
echo "GPUs visible: $count"
nvidia-smi --query-gpu=index,name,memory.total,driver_version \
           --format=csv,noheader | sed 's/^/  /'
echo

# PRD §4.5 item 1: one whole GPU per pod, never a fraction -- and equally, the
# container assumes it owns the WHOLE card and nothing else. More than one
# visible device means the benchmark's provenance is ambiguous: which card
# served, and was the other one busy? Pin with CUDA_VISIBLE_DEVICES and re-run.
if [ "$count" -eq 1 ]; then
  pass "exactly one GPU visible"
else
  bad "$count GPUs visible -- the memory budget and every throughput figure assume one whole card. Set CUDA_VISIBLE_DEVICES=0 and re-run, or pick a 1x listing."
fi

# ── Card model ───────────────────────────────────────────────────────────────
# A10G has different bandwidth and clocks; usable, but its numbers do not
# transfer to VM.GPU.A10.1 and must not be handed to procurement as if they did.
name=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
case "$name" in
  *A10G*) warn "card is '$name' (A10G, not A10) -- usable, but throughput will NOT transfer to VM.GPU.A10.1" ;;
  *L4*)   bad  "card is '$name' (L4, not A10) -- Ada generation, different memory budget entirely" ;;
  *A10*)  pass "card is '$name'" ;;
  *)      bad  "card is '$name' -- not an A10; PRD §3.3 does not apply" ;;
esac

# ── VRAM ─────────────────────────────────────────────────────────────────────
# 24 GB reports as ~23028 MiB. Anything materially below that is a partition,
# and on a half card the AWQ config has ~4 GiB of KV cache: 5x4k works, 5x8k
# does not. See PRD §4.4(a).
mib=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
if   [ "$mib" -ge 22000 ]; then pass "VRAM ${mib} MiB (full 24 GB card)"
elif [ "$mib" -ge 11000 ]; then bad "VRAM ${mib} MiB -- looks like a HALF card. 5x8k context is unreachable (PRD §4.4a)"
else                            bad "VRAM ${mib} MiB -- partitioned card, weights may not even fit"
fi

# ── Nothing else on the card ─────────────────────────────────────────────────
# vLLM pre-allocates its KV cache at startup, so another tenant does not degrade
# it -- it makes it fail at boot.
used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
if [ "$used" -lt 500 ]; then pass "card is idle (${used} MiB used)"
else                         warn "${used} MiB already in use -- something else is on this card"
fi

# ── CUDA / driver ────────────────────────────────────────────────────────────
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
cuda=$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' | head -1)
echo "  driver: $driver"
echo "  ${cuda:-CUDA Version: unknown}"
echo "  ^ record both: this is the minimum we hand DevOps (Q17)"

# ── Disk ─────────────────────────────────────────────────────────────────────
avail_gb=$(df -Pk /workspace 2>/dev/null || df -Pk /; )
avail_gb=$(echo "$avail_gb" | awk 'NR==2 {print int($4/1024/1024)}')
if [ "${avail_gb:-0}" -ge 30 ]; then pass "disk free ${avail_gb} GB"
else                                 bad "disk free ${avail_gb} GB -- need >=30 GB for weights, image layers and HF cache"
fi

# ── Data hygiene ─────────────────────────────────────────────────────────────
# scripts/pack_for_box.sh should make this impossible. Checked anyway, because
# the cost of being wrong is real user data on a third-party host.
echo
echo "=== Data hygiene (PRD §4.8) ==="
hygiene_scan "${WORKSPACE:-/workspace}"

echo
if [ "$fail" -eq 0 ]; then
  echo "All hard requirements met. Record the driver/CUDA lines above in"
  echo "docs/PHASE1_RESULTS.md before pulling any weights."
else
  echo "FAILED. Do not benchmark this box -- the numbers would not mean anything."
fi
exit "$fail"
