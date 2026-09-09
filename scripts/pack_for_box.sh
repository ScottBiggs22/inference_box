#!/usr/bin/env bash
# Build the transfer tarball for a rented, UNTRUSTED GPU box.
# See docs/VAST_AI_RUNBOOK.md §3.
#
# WHY THIS SCRIPT REPLACED TWO `git archive` LINES
# ================================================
# The runbook used to argue that `git archive` was safe *mechanically*, because
# it ships tracked files only and the sensitive paths are gitignored. The first
# half is true. The conclusion does not follow, and the audit on 2026-09-09
# found the gap: TRACKED IS NOT THE SAME AS SAFE. In the AI service, 1.2 MB of
# a 2.4 MB tracked payload was real platform content --
# `data/pending_knowledge.json` (40 records of verbatim extracted document
# text), `knowledge/.faiss_index.json` (182 verbatim chunks) and its
# embeddings. `data/` was not gitignored at all, so `git archive HEAD` would
# have put all of it on someone else's machine.
#
# Two changes follow from that:
#
#   1. The AI service is NOT transferred at all any more. Its eval suite calls
#      the real `chat_rag`, which retrieves from the real knowledge base and
#      injects the chunks into the prompt -- so real content would reach the box
#      in the request body no matter how the code got there. Capacity, cold
#      start, throughput and B3 need none of it. Eval quality belongs on the OCI
#      card (PRD §4.8).
#
#   2. gitignore is kept as the base mechanism, but a DENY-LIST is applied on
#      top and the whole manifest is printed. Allowlist-with-manifest is the
#      same discipline the .dockerignore already uses: it fails closed as the
#      tree grows, rather than depending on whoever adds the next directory
#      remembering to gitignore it.
#
# The temp-index build is what lets uncommitted work ship without a commit:
# `git add -A` into a throwaway index still honours .gitignore, so `.env`,
# `keys.json` and `logs/` stay excluded, while a work-in-progress loadgen.py
# travels. The real index and the working tree are never touched.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out="${1:-/tmp/inference.tgz}"
dry_run=0
[ "${1:-}" = "--dry-run" ] && { dry_run=1; out=""; }

fail=0
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

# ── This must never be pointed at the app repo ───────────────────────────────
# Belt as well as braces: the app repo is the one that holds real data, and the
# whole point of the rewrite is that it does not travel.
case "$repo_root" in
  *baas-poc-templates*)
    echo "REFUSING: $repo_root is inside baas-poc-templates." >&2
    echo "The AI service does not go on a rented box -- see the header." >&2
    exit 2 ;;
esac
if [ ! -d "$repo_root/gateway" ]; then
  echo "REFUSING: $repo_root does not look like bkn301-inference." >&2
  exit 2
fi

cd "$repo_root"

# ── Stage the working tree into a throwaway index ────────────────────────────
tmpidx=$(mktemp -u "${TMPDIR:-/tmp}/packidx.XXXXXX")
cleanup() { rm -f "$tmpidx"; }
trap cleanup EXIT

GIT_INDEX_FILE="$tmpidx" git read-tree HEAD
GIT_INDEX_FILE="$tmpidx" git add -A .
manifest=$(GIT_INDEX_FILE="$tmpidx" git ls-files --cached)

echo "=== Manifest: $(printf '%s\n' "$manifest" | grep -c . ) files ==="
printf '%s\n' "$manifest" | sed 's/^/  /'
echo

# ── Deny-list ────────────────────────────────────────────────────────────────
# Every entry earns its place from something real. `data/` and `knowledge/`
# are here because they were the actual leak in the AI service; `models/` and
# `*.gguf` because weights and the 15 MB vendored tokenizer have no business
# in a transfer; `*.jsonl` because that is the shape of both the audit log and
# the interaction telemetry.
echo "=== Deny-list (PRD §4.8) ==="
deny=$(printf '%s\n' "$manifest" | grep -E \
  -e '(^|/)\.env$' \
  -e '(^|/)\.env\.[^/]*$' \
  -e '(^|/)[a-z-]*keys\.json$' \
  -e '(^|/)logs/' \
  -e '^data/' -e '(^|/)data/' \
  -e '^knowledge/' -e '(^|/)knowledge/' \
  -e '^models/' -e '(^|/)models/' \
  -e '\.gguf$' \
  -e '\.faiss[^/]*$' \
  -e '(^|/)chat_interactions\.log$' \
  -e '(^|/)token_stats\.jsonl$' \
  -e '\.jsonl$' \
  | grep -v -e '^\.env\.example$' -e '^tests/fixtures/.*\.jsonl$' || true)

if [ -n "$deny" ]; then
  bad "denied paths staged for transfer:"
  printf '%s\n' "$deny" | sed 's/^/        /'
else
  pass "no denied paths in the manifest"
fi

# ── Content scan ─────────────────────────────────────────────────────────────
# SHAPE-AWARE, not prefix-aware, and that distinction is load-bearing:
# tests/test_auth.py deliberately contains the string "sk-ant-something" as a
# malformed-credential fixture, so a bare `grep sk-ant-` fails on this repo's
# own test suite. A real Anthropic key has 20+ chars of key material after the
# prefix; a real gateway key is bkn_live_<12 hex>_<43 urlsafe>. Matching the
# shape finds credentials and ignores documentation and fixtures.
echo
echo "=== Credential scan ==="
scan_hits=""
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if LC_ALL=C grep -lE 'sk-ant-[A-Za-z0-9_-]{20,}|bkn_live_[0-9a-f]{12}_[A-Za-z0-9_-]{30,}' "$f" >/dev/null 2>&1; then
    scan_hits="${scan_hits}${f}"$'\n'
  fi
done <<< "$manifest"

if [ -n "$scan_hits" ]; then
  bad "credential-shaped material found:"
  printf '%s' "$scan_hits" | sed 's/^/        /'
else
  pass "no credential-shaped material in the payload"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "REFUSING TO PACK. Fix the above -- this tarball was going to an untrusted host."
  exit 1
fi

if [ "$dry_run" -eq 1 ]; then
  echo "Dry run: checks passed, nothing written."
  exit 0
fi

tree=$(GIT_INDEX_FILE="$tmpidx" git write-tree)
git archive --format=tar.gz -o "$out" "$tree"
echo "Wrote $out ($(du -h "$out" | cut -f1)) from tree $tree"
echo
echo "Next: scp it to the box, extract, and run scripts/verify_box.sh BEFORE"
echo "pulling any weights. See docs/VAST_AI_RUNBOOK.md §2-3."
