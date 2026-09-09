#!/usr/bin/env bash
# Mint a throwaway API key for local development and write the key store that
# deploy/docker-compose.yml mounts.
#
# WHY THIS EXISTS
# ===============
# The compose file mounts `./local-keys.json` read-only into the gateway. A
# read-only bind of a file that does not exist does not fail helpfully: Docker
# creates a DIRECTORY at that path, the gateway's KeyStore then tries to read a
# directory as JSON, and the container comes up unable to authenticate anybody.
# The README nonetheless said `docker compose up --build` worked.
#
# The store is not committed and never will be: it holds argon2id hashes rather
# than plaintext, but it is still a per-environment credential registry, which
# is the reason `keys.json` is in .gitignore in the first place. So it is
# generated instead, and `deploy/local-keys.json` is gitignored too.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
store="$repo_root/deploy/local-keys.json"
python_bin="${PYTHON:-$repo_root/.venv/bin/python}"

if [ ! -x "$python_bin" ]; then
  python_bin=$(command -v python3.12 || command -v python3)
fi

# The pepper must match what the gateway runs with, or every key in the store
# fails to verify. compose sets this literal in its `environment:` block; if you
# change one, change both.
export API_KEY_PEPPER="${API_KEY_PEPPER:-local-dev-pepper-not-a-secret}"

plaintext=$("$python_bin" - "$store" <<'PY'
import sys
from gateway.auth.keys import KeyStore, mint_key

store = KeyStore(sys.argv[1])
plaintext, record = mint_key(scopes=["chat"], label="local-dev")
store.add(record)
store.save()
print(plaintext)
PY
)

echo "Wrote $store"
echo
echo "  export KEY=$plaintext"
echo
echo "Shown once -- only the argon2id hash is stored. Re-run to mint another."
echo "The pepper is '$API_KEY_PEPPER' and must match compose's environment."
