"""Authentication and request validation.

These are the abuse cases from PRD §7 Phase 2's exit gate: revoked key, expired
credential, oversized prompt, disallowed model. They are written now, against
the first slice, so the gate is something the code grows into rather than
something bolted on at the end.
"""
from __future__ import annotations

import time

import pytest

from gateway.auth.jwt import JwtVerifier
from gateway.auth.keys import AuthError, KeyStore, mint_key, split_key


class TestKeyFormat:
    def test_roundtrip(self):
        plaintext, record = mint_key()
        keyid, secret = split_key(plaintext)
        assert keyid == record.keyid
        assert secret

    def test_keyid_is_indexable_without_the_secret(self):
        """The reason for the format: verification is a dict lookup, not a scan."""
        plaintext, record = mint_key()
        keyid, _ = split_key(plaintext)
        assert plaintext.startswith(f"bkn_live_{keyid}_")

    @pytest.mark.parametrize("bad", [
        "", "nope", "bkn_live_", "bkn_live_abc", "Bearer bkn_live_a_b",
        "sk-ant-something", "bkn_live__secret",
    ])
    def test_malformed_rejected(self, bad):
        with pytest.raises(AuthError):
            split_key(bad)

    def test_secret_is_not_recoverable_from_the_record(self):
        plaintext, record = mint_key()
        _, secret = split_key(plaintext)
        assert secret not in record.hash
        assert plaintext not in record.hash


class TestKeyStore:
    def test_valid_key_verifies(self, key_store):
        plaintext, record = mint_key()
        key_store.add(record)
        assert key_store.verify(plaintext).keyid == record.keyid

    def test_revoked_key_rejected(self, key_store):
        plaintext, record = mint_key()
        key_store.add(record)
        key_store.revoke(record.keyid)
        with pytest.raises(AuthError):
            key_store.verify(plaintext)

    def test_expired_key_rejected(self, key_store):
        plaintext, record = mint_key(ttl_sec=1)
        record.expires_at = time.time() - 1
        key_store.add(record)
        with pytest.raises(AuthError):
            key_store.verify(plaintext)

    def test_unknown_keyid_rejected(self, key_store):
        plaintext, _ = mint_key()
        with pytest.raises(AuthError):
            key_store.verify(plaintext)

    def test_wrong_secret_rejected(self, key_store):
        _, record = mint_key()
        key_store.add(record)
        with pytest.raises(AuthError):
            key_store.verify(f"bkn_live_{record.keyid}_wrongsecret")

    def test_persists_across_reload(self, key_store, tmp_path):
        plaintext, record = mint_key(label="persisted")
        key_store.add(record)
        key_store.save()
        reopened = KeyStore(key_store.path)
        assert reopened.verify(plaintext).label == "persisted"


class TestJwtAlgorithmPinning:
    """The gateway must never be able to MINT a token it can verify."""

    @pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512", "none"])
    def test_symmetric_algorithms_refused_at_construction(self, alg):
        with pytest.raises(ValueError, match="not asymmetric"):
            JwtVerifier(public_key="irrelevant", algorithm=alg)

    def test_asymmetric_accepted(self):
        assert JwtVerifier(public_key="x", algorithm="RS256").enabled

    def test_disabled_without_a_key(self):
        v = JwtVerifier(public_key="", algorithm="RS256")
        assert not v.enabled
        with pytest.raises(AuthError):
            v.verify("a.b.c")


class TestRouteAuth:
    def test_no_credential_is_401(self, client):
        assert client.get("/v1/models").status_code == 401

    def test_garbage_credential_is_401(self, client):
        r = client.get("/v1/models", headers={"Authorization": "Bearer nonsense"})
        assert r.status_code == 401

    def test_www_authenticate_header_present(self, client):
        r = client.get("/v1/models")
        assert r.headers.get("WWW-Authenticate") == "Bearer"

    def test_valid_key_reaches_the_route(self, client, live_key):
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        assert r.status_code == 200
        assert r.json()["data"][0]["id"] == "Qwen/Qwen3-8B-AWQ"

    def test_revoked_key_is_401(self, client, live_key, key_store):
        keyid, _ = split_key(live_key)
        key_store.revoke(keyid)
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        assert r.status_code == 401

    def test_rejection_reason_is_not_disclosed(self, client, live_key, key_store):
        """Revoked and unknown must be indistinguishable to the caller."""
        keyid, _ = split_key(live_key)
        key_store.revoke(keyid)
        revoked = client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        unknown_key, _ = mint_key()
        unknown = client.get("/v1/models", headers={"Authorization": f"Bearer {unknown_key}"})
        assert revoked.status_code == unknown.status_code == 401
        assert revoked.json() == unknown.json()


class TestRequestValidation:
    """Every one of these must be rejected BEFORE the GPU sees it."""

    def _post(self, client, key, **overrides):
        body = {
            "model": "Qwen/Qwen3-8B-AWQ",
            "messages": [{"role": "user", "content": "hello"}],
        }
        body.update(overrides)
        return client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )

    def test_disallowed_model_rejected(self, client, live_key):
        r = self._post(client, live_key, model="meta-llama/Llama-3-70B")
        assert r.status_code == 400
        assert r.json()["detail"] == "model_not_allowed"

    def test_missing_messages_rejected(self, client, live_key):
        r = self._post(client, live_key, messages=[])
        assert r.status_code == 400
        assert r.json()["detail"] == "messages_required"

    def test_oversized_prompt_rejected(self, client, live_key):
        huge = " ".join(["token"] * 20000)
        r = self._post(client, live_key, messages=[{"role": "user", "content": huge}])
        assert r.status_code in (400, 413)

    def test_unauthenticated_never_reaches_validation(self, client):
        """Auth runs first: an unauthenticated bad request is 401, not 400."""
        r = client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
        assert r.status_code == 401


class TestProbes:
    def test_healthz_needs_no_credential(self, client):
        """Kubernetes does not carry an API key."""
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_readyz_reports_unavailable_with_no_upstream(self, client):
        """No stub running in the unit suite, so every replica is unreachable."""
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] in ("unavailable", "error")
