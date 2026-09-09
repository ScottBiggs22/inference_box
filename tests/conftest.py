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
