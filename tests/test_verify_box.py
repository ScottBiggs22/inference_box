"""`scripts/verify_box.sh` — the first-boot gate for a rented host.

The GPU checks cannot run on a laptop, so what is tested here is the part that
can be: the data-hygiene scan, via the script's own `--self-test` (which plants
each decoy and asserts it is caught) and `--scan-only` (which points the scan at
a directory).

There is a specific reason this is in CI rather than left to be run by hand on
the box. The scan's previous version was `grep -rl 'sk-ant-'` with the script
excluded by filename, and `tests/test_auth.py` deliberately contains the string
"sk-ant-something" as a malformed-credential fixture. So the moment this repo
was transferred, the scan matched the test suite and the script exited 1 with
"something matching an Anthropic key is present" -- the very first step of a
paid box session, failing on a false positive, with the runbook instructing
"do not benchmark this box". A gate that always fails is a gate people learn to
ignore.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "verify_box.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - resolved bash, literal script path
        [BASH, str(SCRIPT), *args],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )


def test_self_test_passes():
    """Every planted decoy is caught, and the repo's own fixtures are not."""
    result = _run("--self-test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Self-test passed" in result.stdout
    assert "MISSED" not in result.stdout


def test_scan_is_clean_against_this_repo():
    """The regression: this repo, on the box, must not trip its own scan.

    `tests/test_auth.py` holds "sk-ant-something"; `test_pack_for_box.py` builds
    a real-shaped decoy from parts; three docs mention the `sk-ant-` prefix by
    name. All are legitimate, all travel to the box, and none may fail the gate.
    """
    result = _run("--scan-only", str(REPO))
    assert result.returncode == 0, (
        "the hygiene scan fails against this repo's own tree:\n" + result.stdout
    )
    assert "Hygiene scan clean" in result.stdout


@pytest.mark.parametrize("decoy", [
    "data/pending_knowledge.json",
    "knowledge/.faiss_index.json",
    "logs/chat_interactions.log",
    ".env",
    "models/weights.gguf",
])
def test_scan_only_catches_planted_data(tmp_path, decoy):
    """`--scan-only` is the mode used by hand on the box, so it must bite too."""
    target = tmp_path / decoy
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("real content")
    result = _run("--scan-only", str(tmp_path))
    assert result.returncode == 1, result.stdout
    assert "HYGIENE SCAN FAILED" in result.stdout


def test_missing_scan_target_is_a_failure_not_a_pass(tmp_path):
    """A scan of a directory that does not exist examined nothing.

    Found on the first real box: `vllm/vllm-openai` has no `/workspace` (its
    home is `/root`), and that was the scan's default target. `find` on a
    missing path returns nothing, so the gate printed three PASS lines having
    looked at no files at all -- the exact always-passes failure this file's
    docstring criticises the old scan for.
    """
    result = _run("--scan-only", str(tmp_path / "does-not-exist"))
    assert result.returncode == 1, result.stdout
    assert "does not exist" in result.stdout
    assert "nothing was checked" in result.stdout
