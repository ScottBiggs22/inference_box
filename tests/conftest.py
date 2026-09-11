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
def stub_upstream(live_stub):
    """Point the gateway's pool at a real stub subprocess, and restore after.

    There was no fixture for this: `client` always pulls in `dead_upstream`, so
    until now NO test ever streamed a real body through the gateway. That gap is
    why the streaming path could return 200 on an upstream 503 undetected.
    """
    from gateway.upstream.client import pool

    original = pool.urls

    def _point(*extra_args: str) -> str:
        base = live_stub(*extra_args)
        pool.urls = [f"{base}/v1"]
        return base

    yield _point
    pool.urls = original


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


@pytest.fixture
def live_gateway(live_stub, tmp_path):
    """The real gateway under uvicorn, on a real socket, in front of a real stub.

    A subprocess rather than a TestClient, for the same reason `live_stub` is one
    -- and it matters more here. `httpx.ASGITransport`, which TestClient uses,
    COLLECTS a streaming response before handing it back, so `iter_raw()` over a
    TestClient always yields one chunk with zero spread no matter what the app
    did. Any test of unbuffered passthrough written against TestClient is
    therefore testing httpx, not the gateway, and would pass just as happily
    against an implementation that buffered everything.

    Returns (base_url, api_key).
    """
    import os
    import socket
    import subprocess
    import sys
    import time as _time
    from pathlib import Path

    import httpx

    from gateway.auth.keys import KeyStore, mint_key

    procs: list[subprocess.Popen] = []

    def _start(*stub_args: str) -> tuple[str, str]:
        upstream = live_stub(*stub_args)

        pepper = "live-gateway-test-pepper"
        store_path = tmp_path / "gw-keys.json"
        # mint_key() hashes with settings.API_KEY_PEPPER from THIS process's
        # already-constructed settings object, so setting only the environment
        # variable for the subprocess mints a key the subprocess cannot verify.
        # Both sides have to agree.
        previous, settings.API_KEY_PEPPER = settings.API_KEY_PEPPER, pepper
        try:
            store = KeyStore(store_path)
            plaintext, record = mint_key(scopes=["chat"], label="live-gateway")
            store.add(record)
            store.save()
        finally:
            settings.API_KEY_PEPPER = previous

        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]

        env = {
            **os.environ,
            "API_KEY_STORE_PATH": str(store_path),
            "API_KEY_PEPPER": pepper,
            "UPSTREAM_URLS": f"{upstream}/v1",
            "AUDIT_LOG_PATH": str(tmp_path / "gw-audit.jsonl"),
            "UPSTREAM_METRICS_POLL_SEC": "1.0",
        }
        # S603: this interpreter plus literals from the test body.
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "uvicorn", "gateway.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        deadline = _time.time() + 30
        while _time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"gateway exited early with {proc.returncode}")
            try:
                if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                    return base, plaintext
            except Exception:  # noqa: BLE001 - still booting
                _time.sleep(0.1)
        raise RuntimeError("gateway did not become healthy within 30s")

    yield _start

    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
