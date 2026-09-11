"""Per-replica circuit breaker.

PRD §5.3 asks for "circuit breaker per replica, bounded retries with jitter on
429/503, health-check-based routing so a wedged replica is removed rather than
retried into", and PRD §4.6(1) makes this the PRIMARY wedge response, with a
sidecar failing Kubernetes liveness as the backstop. The PRD specifies no
threshold, no timings and no reclose policy, so they are chosen here and each one
is justified against a Phase 1 measurement rather than against a default.

WHY A WEIGHTED CONSECUTIVE-FAILURE SCORE AND NOT AN ERROR RATE
==============================================================
At three to five concurrent users a rate window has no denominator. "50% of at
least 20 requests in 60s" either never fires at this traffic or fires on two out
of three. Consecutive counting also fails in the conservative direction, because
any success resets it to zero -- and PRD §4.6 is explicit that a late alert beats
a false positive, since the false positive costs a 153s cold reload and can loop.

One score with per-class weights keeps a single tunable while still saying, in
the type system, that these are not the same failure.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class FailureKind(str, enum.Enum):
    CONNECT = "connect"
    READ_TIMEOUT = "read_timeout"
    SERVER_ERROR = "server_error"
    THROTTLED = "throttled"
    CLIENT_ERROR = "client_error"
    WEDGE = "wedge"


# Trip at 6. The weights below are what make that one number mean different
# things for different failures.
#
#   CONNECT (3, trips on 2)
#       The connect budget is 5s and a healthy replica connects in under 5ms, so
#       two consecutive connect failures are unambiguous -- and have already cost
#       a caller ten seconds. Nothing reached the GPU, so nothing is lost.
#
#   READ_TIMEOUT (3, trips on 2)
#       The worst legitimate request measurable from Phase 1 is 2048 tokens at
#       28.9 tok/s per user (the cold-prefix p50 at 5 concurrent) plus a 6.45s
#       TTFT p95 -- about 78s. The 120s read budget is 1.5x that, so a timeout is
#       genuinely anomalous, but 1.5x is not enough margin to act on one.
#
#   SERVER_ERROR (1, trips on 6)
#       vLLM's 503 is frequently scheduler back-pressure rather than brokenness.
#       With one retry, six upstream failures is about three client requests.
#
#   THROTTLED (0, never trips)
#       A replica returning 429 is WORKING and asking for less load. Tripping on
#       it is PROBE_CONTRACT.md §3's "readiness goes red when the instance is
#       busy" mistake moved into the router, where it fails the same way: load
#       sheds capacity, which concentrates load. Retried instead.
#
#   CLIENT_ERROR (0, never trips)
#       The caller's fault. A malformed request is not a broken replica.
#
#   WEDGE (6, trips on the 2nd consecutive confirmation)
#       Enough to trip alone, but only on confirmation -- see record_wedge.
WEIGHTS: dict[FailureKind, int] = {
    FailureKind.CONNECT: 3,
    FailureKind.READ_TIMEOUT: 3,
    FailureKind.SERVER_ERROR: 1,
    FailureKind.THROTTLED: 0,
    FailureKind.CLIENT_ERROR: 0,
    FailureKind.WEDGE: 6,
}

TRIP_SCORE = 6
# 15s: the reclose probe is a ~5ms GET, so probing this often costs nothing, and
# 15s is far longer than any transient worth riding out.
INITIAL_OPEN_SEC = 15.0
# Doubles per failed reclose, capped at 60s -- which keeps probing at once a
# minute during a long outage while staying well under the measured 153s cold
# start, so a genuinely restarted pod is found within a minute of being ready.
MAX_OPEN_SEC = 60.0
PROBE_INTERVAL_SEC = 5.0
PROBES_TO_CLOSE = 2
# After a reclose the replica is on probation at half the threshold, so an
# open -> close -> open flap does not put a full traffic wave into a still-broken
# replica on every cycle.
PROBATION_SEC = 60.0


@dataclass
class ReplicaBreaker:
    """One replica's state. Pure: the clock is passed in, never read."""

    url: str
    trip_score: int = TRIP_SCORE
    state: BreakerState = BreakerState.CLOSED
    score: int = 0
    opened_at: float | None = None
    open_sec: float = INITIAL_OPEN_SEC
    good_probes: int = 0
    last_probe_at: float | None = None
    probation_until: float = 0.0
    wedge_confirmations: int = 0
    trips: int = 0
    _label: str = field(default="?", repr=False)

    def threshold(self, now: float) -> int:
        return max(1, self.trip_score // 2) if now < self.probation_until else self.trip_score

    def allows(self) -> bool:
        """Routing admits CLOSED only.

        Half-open is invisible to routing: reclose is decided entirely by the
        prober, so there is exactly one code path and no request is ever used as
        an experiment.
        """
        return self.state is BreakerState.CLOSED

    def record_success(self, now: float) -> None:
        # A full reset, not a decay. One success is strong evidence at this
        # traffic level, and erring toward staying closed is the conservative
        # direction.
        self.score = 0
        self.wedge_confirmations = 0

    def record_failure(self, kind: FailureKind, now: float) -> None:
        weight = WEIGHTS[kind]
        if weight == 0:
            return
        self.score += weight
        if self.state is BreakerState.CLOSED and self.score >= self.threshold(now):
            self._open(now, str(kind.value))

    def record_wedge(self, suspected: bool, now: float) -> None:
        """Act on the second consecutive confirmation, never the first.

        The window this derives from is an ESTIMATE (PROBE_CONTRACT.md §4, never
        measured because Phase 1 did not wedge an engine), and with one replica a
        false positive is a total outage. A second confirmation costs one scrape
        interval and still alerts a human at the original deadline, which is what
        PRD §4.6(1) actually asks the breaker to do.

        Note too that a real wedge WITH traffic on it produces read timeouts
        anyway. This signal's unique value is catching a wedge when there is no
        in-flight request to time out -- which is exactly why it must not also be
        the fastest path to tripping.
        """
        if not suspected:
            self.wedge_confirmations = 0
            return
        self.wedge_confirmations += 1
        if self.wedge_confirmations == 1:
            logger.warning(
                "upstream %s shows the wedge signature; not acting until it is "
                "confirmed on the next scrape", self._label,
            )
            return
        if self.state is BreakerState.CLOSED:
            self.record_failure(FailureKind.WEDGE, now)

    def _open(self, now: float, why: str) -> None:
        self.state = BreakerState.OPEN
        self.opened_at = now
        self.good_probes = 0
        self.last_probe_at = None
        self.trips += 1
        logger.error(
            "circuit breaker OPEN for upstream %s after %s (score %d/%d)",
            self._label, why, self.score, self.threshold(now),
        )

    def due_for_probe(self, now: float) -> bool:
        if self.state is BreakerState.CLOSED:
            return False
        if self.state is BreakerState.OPEN:
            return self.opened_at is not None and now - self.opened_at >= self.open_sec
        return self.last_probe_at is None or now - self.last_probe_at >= PROBE_INTERVAL_SEC

    def record_probe(self, ok: bool, now: float, *, wedge_suspected: bool = False) -> None:
        """Fold one reclose probe in.

        A good probe is NOT sufficient on its own, and the stub proves why:
        `--stub-fail-after` makes chat 503 while /models keeps answering 200, and
        a really wedged vLLM behaves identically because the API server is not
        the engine loop. Hence two consecutive good probes AND no live wedge
        signal.
        """
        self.last_probe_at = now
        if not ok or wedge_suspected:
            self.good_probes = 0
            self.state = BreakerState.OPEN
            self.opened_at = now
            self.open_sec = min(self.open_sec * 2, MAX_OPEN_SEC)
            return

        self.state = BreakerState.HALF_OPEN
        self.good_probes += 1
        if self.good_probes >= PROBES_TO_CLOSE:
            self._close(now)

    def _close(self, now: float) -> None:
        self.state = BreakerState.CLOSED
        self.score = 0
        self.good_probes = 0
        self.opened_at = None
        self.open_sec = INITIAL_OPEN_SEC
        self.wedge_confirmations = 0
        self.probation_until = now + PROBATION_SEC
        logger.warning(
            "circuit breaker CLOSED for upstream %s; on probation at half "
            "threshold for %.0fs", self._label, PROBATION_SEC,
        )

    def seconds_until_probe(self, now: float) -> float:
        if self.state is BreakerState.CLOSED:
            return 0.0
        if self.state is BreakerState.OPEN and self.opened_at is not None:
            return max(0.0, self.open_sec - (now - self.opened_at))
        if self.last_probe_at is not None:
            return max(0.0, PROBE_INTERVAL_SEC - (now - self.last_probe_at))
        return 0.0


class BreakerRegistry:
    """url -> ReplicaBreaker, rebuilt whenever the replica list changes."""

    def __init__(self) -> None:
        self._breakers: dict[str, ReplicaBreaker] = {}

    def reset(self, urls: list[str]) -> None:
        self._breakers = {
            url: ReplicaBreaker(url=url, _label=str(i)) for i, url in enumerate(urls)
        }

    def get(self, url: str) -> ReplicaBreaker:
        if url not in self._breakers:
            self._breakers[url] = ReplicaBreaker(url=url, _label="?")
        return self._breakers[url]

    def __iter__(self):
        return iter(self._breakers.values())

    def snapshot(self) -> dict[str, str]:
        return {url: b.state.value for url, b in self._breakers.items()}
