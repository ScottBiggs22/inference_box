"""The transfer to a rented box must not carry real data.

THIS IS THE F2 REGRESSION TEST.

The runbook used to claim the transfer was safe mechanically because
`git archive` ships tracked files only and the sensitive paths are gitignored.
The audit on 2026-09-09 found the hole: in the AI service, 1.2 MB of a 2.4 MB
tracked payload was real platform content, and `data/` was not gitignored at
all. Tracked is not the same as safe.

So `pack_for_box.sh` applies a deny-list on top of gitignore, and these tests
plant the exact decoys that were the real leak -- `data/pending_knowledge.json`,
`knowledge/.faiss_index.json`, a `.env`, a `.gguf` -- in a throwaway git repo
and assert the pack refuses. A test that only checked the happy path would have
passed against the broken runbook too.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pack_for_box.sh"


def _make_repo(tmp_path: Path, files: dict[str, str], gitignore: str = "") -> Path:
    """A throwaway git repo shaped enough like this one for the script to accept it.

    The script derives its repo root from its own location, so the script is
    COPIED IN rather than pointed at a path -- which means these tests exercise
    the real file, not a testing-only code path.
    """
    root = tmp_path / "fake-inference"
    (root / "scripts").mkdir(parents=True)
    (root / "gateway").mkdir()          # the script's sanity check
    (root / "gateway" / "__init__.py").write_text("")
    shutil.copy(SCRIPT, root / "scripts" / "pack_for_box.sh")
    if gitignore:
        (root / ".gitignore").write_text(gitignore)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root, check=True,
    )
    return root


def _pack(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "scripts/pack_for_box.sh", *args],
        cwd=root, capture_output=True, text=True,
    )


class TestDenyList:
    """Each case is a path that actually appeared in the audited payload."""

    @pytest.mark.parametrize("path", [
        "data/pending_knowledge.json",      # 692 KB of verbatim extractedText
        "knowledge/.faiss_index.json",      # 182 verbatim chunks
        "knowledge/.faiss_index.bin",       # the embeddings of that text
        "logs/chat_interactions.log",       # 1188 plaintext staff queries
        "logs/audit.jsonl",
        "models/qwen3-tokenizer/tokenizer.json",
        "models/qwen2.5-1.5b-instruct-q5_k_m.gguf",
        "token_stats.jsonl",
    ])
    def test_denied_path_refuses_the_pack(self, tmp_path, path):
        # Committed WITHOUT a gitignore, which is precisely the AI service's
        # situation for data/: tracked, therefore invisible to the old argument.
        root = _make_repo(tmp_path, {path: "x", "README.md": "hi"})
        result = _pack(root, "--dry-run")
        assert result.returncode == 1, f"{path} was allowed through:\n{result.stdout}"
        assert "REFUSING TO PACK" in result.stdout
        assert path in result.stdout

    def test_dotenv_is_refused_even_when_tracked(self, tmp_path):
        root = _make_repo(tmp_path, {".env": "API_KEY_PEPPER=real", "README.md": "hi"})
        result = _pack(root, "--dry-run")
        assert result.returncode == 1
        assert "REFUSING TO PACK" in result.stdout

    def test_env_example_is_allowed(self, tmp_path):
        """The reference file is documentation and must still travel."""
        root = _make_repo(tmp_path, {".env.example": "GATEWAY_PORT=8080"})
        result = _pack(root, "--dry-run")
        assert result.returncode == 0, result.stdout


class TestCredentialScan:
    """Shape-aware, not prefix-aware. The distinction is load-bearing."""

    def test_real_shaped_anthropic_key_is_caught(self, tmp_path):
        root = _make_repo(
            tmp_path,
            {"cfg.py": 'KEY = "sk-ant-api03-' + "A1b2C3d4E5f6G7h8I9j0" * 2 + '"'},
        )
        result = _pack(root, "--dry-run")
        assert result.returncode == 1
        assert "credential-shaped" in result.stdout

    def test_real_shaped_gateway_key_is_caught(self, tmp_path):
        root = _make_repo(
            tmp_path,
            {"note.txt": "bkn_live_0123456789ab_" + "x" * 43},
        )
        result = _pack(root, "--dry-run")
        assert result.returncode == 1

    def test_malformed_fixtures_are_not_flagged(self, tmp_path):
        """tests/test_auth.py really does contain "sk-ant-something".

        A bare `grep sk-ant-` fails on this repo's own suite -- the same
        self-reference bug verify_box.sh had, where it had to exclude itself by
        filename. Matching the shape removes the need for exclusions.
        """
        root = _make_repo(tmp_path, {
            "tests/test_auth.py": 'BAD = ["sk-ant-something", "bkn_live_abc", "bkn_live_"]',
            "README.md": "keys look like bkn_live_<keyid>_<secret>",
        })
        result = _pack(root, "--dry-run")
        assert result.returncode == 0, result.stdout
        assert "no credential-shaped material" in result.stdout


class TestUncommittedWorkTravels:
    """The F1 half: Phase 0 work sat uncommitted, and `git archive HEAD` lost it."""

    def test_uncommitted_new_file_is_in_the_manifest(self, tmp_path):
        root = _make_repo(tmp_path, {"README.md": "hi"})
        (root / "scripts" / "loadgen.py").write_text("# work in progress\n")
        result = _pack(root, "--dry-run")
        assert result.returncode == 0, result.stdout
        assert "scripts/loadgen.py" in result.stdout

    def test_gitignored_file_still_excluded(self, tmp_path):
        """The deny-list is additive: gitignore stays the base mechanism."""
        root = _make_repo(tmp_path, {"README.md": "hi"}, gitignore="secret.txt\n")
        (root / "secret.txt").write_text("nope")
        result = _pack(root, "--dry-run")
        assert result.returncode == 0, result.stdout
        assert "secret.txt" not in result.stdout

    def test_the_real_index_is_untouched(self, tmp_path):
        """A transfer must not stage anything in the caller's working tree."""
        root = _make_repo(tmp_path, {"README.md": "hi"})
        (root / "scratch.py").write_text("x")
        _pack(root, "--dry-run")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root,
            capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == "?? scratch.py", status.stdout


class TestRefusesTheAppRepo:
    def test_refuses_a_path_inside_baas_poc_templates(self, tmp_path):
        root = _make_repo(tmp_path, {"README.md": "hi"})
        moved = tmp_path / "baas-poc-templates"
        moved.mkdir()
        shutil.move(str(root), str(moved / "fake-inference"))
        result = _pack(moved / "fake-inference", "--dry-run")
        assert result.returncode == 2
        assert "does not go on a rented box" in result.stderr

    def test_refuses_a_tree_without_a_gateway_package(self, tmp_path):
        root = _make_repo(tmp_path, {"README.md": "hi"})
        shutil.rmtree(root / "gateway")
        result = _pack(root, "--dry-run")
        assert result.returncode == 2
        assert "does not look like bkn301-inference" in result.stderr
