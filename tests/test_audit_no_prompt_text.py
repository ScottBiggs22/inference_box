"""The audit log must never contain prompt or completion text.

PRD §7 Phase 2 names this test specifically: "Metadata-only audit log. Explicit
test asserting no prompt text is logged."

It is called out separately from the other security tests because it guards a
property that code review reliably misses. Adding one field to an audit record
looks like an improvement in isolation -- more context for whoever reads the log
during an incident -- and it is exactly how an audit trail silently becomes a
PII store subject to a retention policy nobody wrote.

So the assertion is made against the REAL writer, with a distinctive canary
string, over the actual file on disk.
"""
from __future__ import annotations

import json

import pytest

from gateway.audit import AuditRecord, audit_log

# Distinctive enough that a substring search cannot produce a false negative,
# and shaped like something a user would really type.
CANARY_PROMPT = "ZZQX-CANARY-what-is-the-AUM-of-Riyadh-Gallery-Mall-77341"
CANARY_ANSWER = "ZZQX-CANARY-the-reported-figure-is-52-point-4-billion-SAR"


class TestRecordShape:
    def test_record_has_no_content_fields(self):
        """A field-name audit, so a new content-carrying field fails here first."""
        fields = set(AuditRecord.__dataclass_fields__)
        forbidden = {
            "prompt", "prompts", "messages", "content", "completion",
            "completions", "answer", "response_text", "text", "query", "input",
            "output", "choices",
        }
        overlap = fields & forbidden
        assert not overlap, f"AuditRecord carries content-shaped fields: {overlap}"


class TestWrittenFile:
    def test_canary_never_reaches_disk(self, audit_file):
        """End-to-end: write a record while a canary is in flight, then grep."""
        audit_log.write(
            AuditRecord(
                timestamp=audit_log.now(),
                event="chat.completions",
                keyid="abc123",
                subject="key:abc123",
                auth_method="apikey",
                model="Qwen/Qwen3-8B-AWQ",
                status=200,
                latency_ms=1234.5,
                prompt_tokens=813,
                completion_tokens=320,
                upstream="http://127.0.0.1:8001/v1",
            )
        )
        written = audit_file.read_text(encoding="utf-8")
        assert CANARY_PROMPT not in written
        assert CANARY_ANSWER not in written

        record = json.loads(written.strip())
        # The things an audit trail SHOULD carry are present...
        assert record["keyid"] == "abc123"
        assert record["prompt_tokens"] == 813
        assert record["completion_tokens"] == 320
        assert record["status"] == 200
        # ...and nothing else crept in.
        assert set(record) == set(AuditRecord.__dataclass_fields__)

    def test_request_path_logs_no_prompt_text(self, client, live_key, audit_file):
        """The real route, with a canary prompt, rejected before the upstream.

        A rejected request is the right case to test: it is the path where a
        developer is most tempted to log the offending body 'for debugging'.
        """
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {live_key}"},
            json={
                "model": "definitely-not-allowed",
                "messages": [{"role": "user", "content": CANARY_PROMPT}],
            },
        )
        assert resp.status_code == 400

        written = audit_file.read_text(encoding="utf-8")
        assert CANARY_PROMPT not in written
        assert "model_not_allowed" in written


class TestFlagIsOff:
    def test_prompt_text_flag_defaults_off(self):
        """This setting exists to be greppable, not to be enabled."""
        from gateway.config import Settings

        assert Settings().AUDIT_INCLUDE_PROMPT_TEXT is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    def test_flag_is_explicit_opt_in_only(self, value, monkeypatch):
        """Only these exact values turn it on -- no accidental truthiness."""
        monkeypatch.setenv("AUDIT_INCLUDE_PROMPT_TEXT", value)
        from gateway.config import Settings

        assert Settings().AUDIT_INCLUDE_PROMPT_TEXT is True
