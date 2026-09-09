"""Request validation — the checks that stand in front of the GPU.

`gateway/routes/chat.py`'s whole purpose is to reject cheaply what would
otherwise be expensive. So a malformed request must produce a 4xx from this
layer, never a 500: a 500 means the validator itself fell over, which is both
the wrong status for the caller and an error the operator has to triage as if
the gateway were broken.

The `max_tokens` cases exist because the original clamp was
`if requested is None or int(requested) > MAX`, and the bare `int()` raised on
any non-numeric value.
"""
from __future__ import annotations

import json

import pytest

MODEL = "Qwen/Qwen3-8B-AWQ"


def _post(client, key: str, **overrides):
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {key}"})


class TestMaxTokens:
    @pytest.mark.parametrize("value", ["abc", {}, [], "12.5.6", "", "1e5x"])
    def test_non_numeric_is_400_not_500(self, client, live_key, value):
        resp = _post(client, live_key, max_tokens=value)
        assert resp.status_code == 400, (
            f"max_tokens={value!r} produced {resp.status_code}; the validation "
            "layer must not raise on input it exists to validate"
        )
        assert resp.json()["detail"] == "max_tokens_invalid"

    @pytest.mark.parametrize("value", [0, -1, -2048])
    def test_non_positive_is_rejected(self, client, live_key, value):
        """Not clamped UP to the ceiling, which would invert the caller's ask."""
        resp = _post(client, live_key, max_tokens=value)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "max_tokens_invalid"

    def test_rejection_is_audited_with_a_fixed_reason(self, client, live_key, audit_file):
        _post(client, live_key, max_tokens="abc")
        records = [json.loads(x) for x in audit_file.read_text().splitlines()]
        rejected = [r for r in records if r["reason"] == "max_tokens_invalid"]
        assert len(rejected) == 1
        row = rejected[0]
        assert row["status"] == 400
        assert row["model"] == MODEL
        # Metadata only. The reason vocabulary is fixed and never derived from
        # the request, or the audit log becomes a place user input lands.
        assert "abc" not in json.dumps(row)
        assert "hi" not in json.dumps(row)


class TestOtherValidation:
    """Guard rails around the ones above, so a refactor cannot quietly drop them."""

    def test_model_not_in_allowlist_is_rejected(self, client, live_key):
        resp = _post(client, live_key, model="mistral-7b")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "model_not_allowed"

    def test_missing_messages_is_rejected(self, client, live_key):
        resp = client.post("/v1/chat/completions", json={"model": MODEL},
                           headers={"Authorization": f"Bearer {live_key}"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "messages_required"

    def test_oversized_prompt_is_rejected_before_the_gpu(self, client, live_key):
        huge = " ".join(["word"] * 10_000)
        resp = _post(client, live_key, messages=[{"role": "user", "content": huge}])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "prompt_too_long"
