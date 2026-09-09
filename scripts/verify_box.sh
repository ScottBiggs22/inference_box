#!/usr/bin/env bash
# First-boot verification for a rented GPU box. See docs/VAST_AI_RUNBOOK.md §2.
#
# Run this BEFORE anything else. Listings are frequently wrong, and every
# capacity figure in INFERENCE_SERVICE_PRD.md §3.3 assumes a whole 24 GiB A10.
# A partitioned card, an A10G, or an L4 will all boot and serve happily while
# silently invalidating the memory budget and the throughput targets.
#
# Exits non-zero if any hard requirement fails. Paste the output into the
# Phase 1 notes -- DevOps sizes per-environment GPU counts off these numbers,
# so their provenance matters.

set -uo pipefail

fail=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }

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
# The transfer method in the runbook (git archive) should make this impossible.
# Checked anyway, because the cost of being wrong is real user data on a
# third-party host.
echo
echo "=== Data hygiene (PRD §4.8) ==="
leaks=$(find /workspace -maxdepth 4 \( -name '.env' -o -name 'chat_interactions.log' -o -name 'token_stats.jsonl' \) 2>/dev/null)
if [ -z "$leaks" ]; then pass "no .env or interaction logs present"
else                     bad "REAL DATA ON AN UNTRUSTED HOST -- delete this box:"; echo "$leaks" | sed 's/^/        /'
fi

if grep -rl 'sk-ant-' /workspace 2>/dev/null | grep -v verify_box.sh | head -1 >/dev/null; then
  bad "something matching an Anthropic key is present"
else
  pass "no Anthropic key material found"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "All hard requirements met. Record the driver/CUDA lines above."
else
  echo "FAILED. Do not benchmark this box -- the numbers would not mean anything."
fi
exit "$fail"
