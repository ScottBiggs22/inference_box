"""Shared fixtures.

Every fixture here points the gateway at temp files and a stub upstream, so the
suite never touches a real key store, a real audit log, or a GPU.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway import audit as audit_module
from gateway.auth import keys as keys_module
from gateway.auth import reset_auth_singletons
from gateway.config import settings


@pytest.fixture
def key_store(tmp_path, monkeypatch):
    """A KeyStore backed by a temp file, with a pepper set.

    Note what this deliberately does NOT do: monkeypatch
    `gateway.auth.get_key_store`. Rebinding that module attribute has no effect
    on the route, because `Depends(get_key_store)` captured the original
    function object at import time -- and worse, it makes a later
    `from gateway.auth import get_key_store` return the replacement, so a
    dependency_overrides entry keyed on it silently never matches. The override
    in `client` is the only mechanism that works, and it needs the real
    function.
    """
    monkeypatch.setattr(settings, "API_KEY_PEPPER", "test-pepper", raising=False)
    store = keys_module.KeyStore(tmp_path / "keys.json")
    reset_auth_singletons()
    yield store
    reset_auth_singletons()


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    """Redirect the audit log to a temp file and hand back its path."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_module.audit_log, "path", path)
    return path


@pytest.fixture
def live_key(key_store):
    """A minted, stored, currently-valid key. Returns the plaintext."""
    plaintext, record = keys_module.mint_key(scopes=["chat"], label="test")
    key_store.add(record)
    return plaintext


@pytest.fixture
def dead_upstream(monkeypatch):
    """Point the upstream pool at an address nothing is listening on.

    The unit suite must not depend on what happens to be running on the
    developer's machine. Without this the readiness test passed or failed
    depending on whether a stub server was left running on the default port
    8001 -- which is exactly the kind of hidden environmental coupling that
    makes a suite untrustworthy.

    Port 9 is `discard`; nothing serves HTTP there.
    """
    from gateway.upstream.client import pool

    original = pool.urls
    pool.urls = ["http://127.0.0.1:9/v1"]
    yield
    pool.urls = original


@pytest.fixture
def client(key_store, audit_file, dead_upstream):
    """TestClient over the real app, with auth wired to the temp store.

    The dependency is overridden rather than the module global monkeypatched --
    see the note in `key_store` for why the latter cannot work.
    """
    from gateway.auth import get_key_store
    from gateway.main import create_app

    app = create_app()
    app.dependency_overrides[get_key_store] = lambda: key_store
    with TestClient(app) as c:
        yield c


@pytest.fixture
def live_stub():
    """A real `stub.server` subprocess on a free port. Returns a factory.

    A subprocess rather than a TestClient, because what is under test in
    `test_loadgen.py` is CONCURRENCY: an in-process ASGI transport would
    serialise differently from a socket and could make a sequential load
    generator look parallel. The one thing these tests must be able to prove is
    that the workers really do overlap, so the transport has to be real.
    """
    import socket
    import subprocess
    import sys
    import time as _time
    from pathlib import Path

    import httpx

    procs: list[subprocess.Popen] = []

    def _start(*extra_args: str) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # S603: the argv is this interpreter plus literals from the test body.
        # There is no untrusted input anywhere near it.
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "stub.server", "--port", str(port), *extra_args],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        deadline = _time.time() + 30
        while _time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"stub exited early with {proc.returncode}")
            try:
                if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                    return base
            except Exception:  # noqa: BLE001 - still booting
                _time.sleep(0.1)
        raise RuntimeError("stub did not become healthy within 30s")

    yield _start

    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
