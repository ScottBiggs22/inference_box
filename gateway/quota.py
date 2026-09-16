"""Per-key quota enforcement: token budgets and concurrency caps (PRD §5.3).

Two SEPARATE limits, checked and released independently:

  * TOKEN BUDGET is cumulative and coarse. It is checked before a request is
    forwarded, against tokens consumed by PRIOR requests on this key -- it
    cannot know what the request in flight will cost, so a key sitting exactly
    at budget still finishes the request that pushes it over, and the NEXT one
    is rejected. That is the honest reading of PRD §5.3's "a token budget (not
    just a request count)": it prices what already happened, not what is about
    to.
  * CONCURRENCY CAP is a live in-flight counter, acquired before forwarding and
    released once the response -- or the stream -- is fully done. This is the
    gateway's answer to PRD §2.1 T6: a key that would otherwise hold every
    replica with sequential document-extraction calls is capped independently
    of GPU capacity, cheaper than a separate extraction replica pool.

Both are process-local and in-memory. A restart resets them -- acceptable for
Phase 2's single-gateway-process deployment (PRD §6.4 is about AI-service
replica count, not gateway replica count), and far cheaper than a per-request
disk write to the JSON key store on the hot path. If the gateway ever runs as
more than one process, this needs to move to shared state (Redis, already a
platform dependency) -- noted here rather than discovered under load.

Only API-key principals carry a keyid (gateway/auth/__init__.py), so only
API-key principals are subject to either limit. A JWT-authenticated
service-to-service call has no per-key budget by design (PRD §5.3 treats JWT
as the trusted path).
"""
from __future__ import annotations

from dataclasses import dataclass, field


class QuotaExceeded(Exception):
    """Either limit was hit. `reason` is a gateway.metrics.REJECT_REASONS value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class QuotaTracker:
    _inflight: dict[str, int] = field(default_factory=dict)
    _tokens_used: dict[str, int] = field(default_factory=dict)

    def tokens_used(self, keyid: str) -> int:
        return self._tokens_used.get(keyid, 0)

    def check_budget(self, keyid: str, token_budget: int) -> None:
        """Raise if this key has already consumed its budget.

        Deliberately checked before `acquire`: it is stateless (a comparison,
        not a mutation), so the cheaper check runs first -- the same ordering
        principle chat.py already applies to request validation.
        """
        if self.tokens_used(keyid) >= token_budget:
            raise QuotaExceeded("token_budget_exceeded")

    def record_usage(self, keyid: str, total_tokens: int) -> None:
        if total_tokens > 0:
            self._tokens_used[keyid] = self.tokens_used(keyid) + total_tokens

    def acquire(self, keyid: str, max_concurrency: int) -> None:
        """Raise if this key is already at its concurrency cap; else hold a slot.

        Every successful `acquire` MUST be matched by exactly one `release`, on
        every exit path including an exception -- see chat.py's `_stream`,
        where the slot has to survive past this function returning because the
        request is not done until the stream is.
        """
        current = self._inflight.get(keyid, 0)
        if current >= max_concurrency:
            raise QuotaExceeded("concurrency_cap_exceeded")
        self._inflight[keyid] = current + 1

    def release(self, keyid: str) -> None:
        current = self._inflight.get(keyid, 0)
        self._inflight[keyid] = max(0, current - 1)

    def reset(self) -> None:
        """Test seam. Same pattern as gateway.auth.reset_auth_singletons."""
        self._inflight.clear()
        self._tokens_used.clear()


quota = QuotaTracker()
