"""Metadata-only audit log.

THE DISTINCTION THIS MODULE EXISTS TO ENFORCE
=============================================
An audit trail records that something happened, to whom, and at what cost. A PII
store records what was said. On a platform handling investor and KYC data those
are different artefacts with different retention obligations, different access
controls, and different consequences when leaked -- and the difference between
them is one careless field.

So this module writes: timestamp, key id, subject, model, token counts, latency,
status, upstream. It does not write prompts, completions, or any part of either,
and `tests/test_audit_no_prompt_text.py` asserts that against the real writer
rather than trusting review to catch it.

The AUDIT_INCLUDE_PROMPT_TEXT setting exists to make the decision explicit and
greppable, not to be enabled. If a debugging session ever needs prompt text, it
needs it somewhere with a retention policy attached -- not here.

Note also the corresponding upstream requirement: vLLM must run with
`--no-enable-log-requests --no-enable-log-outputs`, or it keeps its own copy of
every prompt regardless of what this file does (PRD §5.4).

Not `--disable-log-requests`, which is what this said until Phase 1 measured it:
that flag does not exist in vLLM 0.28.0 and `vllm serve` refuses to start with
it. The replacement has inverted polarity, so no-retention is now the DEFAULT --
both flags are still passed explicitly so the intent survives a future change of
default.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from gateway.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AuditRecord:
    """One request. Every field here is metadata by construction.

    If you are about to add a field, the test to apply is: could its value
    contain any part of what the user typed or the model said? If yes, it does
    not belong in this dataclass.
    """

    timestamp: str
    event: str
    keyid: str | None
    subject: str | None
    auth_method: str | None
    model: str | None
    status: int
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    upstream: str | None
    # Short machine-readable reason, e.g. "invalid_key", "model_not_allowed".
    # A fixed vocabulary, never free text derived from the request.
    reason: str | None = None
    streamed: bool = False


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.AUDIT_LOG_PATH)
        # One process may serve many concurrent requests, and interleaved
        # partial writes would corrupt the JSONL into unparseable lines --
        # which is indistinguishable from tampering when someone later reads
        # the log during an incident.
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        payload = asdict(record)

        if settings.AUDIT_INCLUDE_PROMPT_TEXT:
            # Loud, every single time, because this must never be on quietly.
            logger.error(
                "AUDIT_INCLUDE_PROMPT_TEXT is enabled. The audit log is now a "
                "PII store and falls under the data retention register."
            )

        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as e:
            # An audit write failure must not fail the request -- but it must be
            # visible, because a silently non-writing audit log is worse than
            # none: it looks like a complete record and is not.
            logger.error("audit write failed: %s: %s", type(e).__name__, e)

    @staticmethod
    def now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


audit_log = AuditLog()
