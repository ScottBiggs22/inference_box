"""The breaker state machine. Pure, injected clock, no I/O and no sleeping.

Every threshold here is a judgement call the PRD does not make -- it asks for "a
circuit breaker per replica" and specifies no number -- so each test names the
measurement or the design principle the number came from. If a threshold is
changed, the test that fails should say why it was that value.
"""
from __future__ import annotations

from gateway.upstream.breaker import (
    INITIAL_OPEN_SEC,
    MAX_OPEN_SEC,
    PROBATION_SEC,
    PROBE_INTERVAL_SEC,
    BreakerState,
    FailureKind,
    ReplicaBreaker,
)


def _b() -> ReplicaBreaker:
    return ReplicaBreaker(url="http://replica/v1")


class TestTripThresholds:
    def test_five_server_errors_do_not_trip_but_six_do(self):
        """vLLM's 503 is often scheduler back-pressure, not brokenness.

        Weight 1 each, so six upstream failures -- roughly three client requests
        once one retry is in play.
        """
        b = _b()
        for i in range(5):
            b.record_failure(FailureKind.SERVER_ERROR, now=float(i))
            assert b.state is BreakerState.CLOSED, f"tripped early at {i + 1}"
        b.record_failure(FailureKind.SERVER_ERROR, now=5.0)
        assert b.state is BreakerState.OPEN

    def test_two_connect_failures_trip(self):
        """Connect budget is 5s; a healthy replica connects in under 5ms.

        Two consecutive failures have already cost a caller ten seconds, and
        nothing reached the GPU, so there is nothing to lose by acting.
        """
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        assert b.state is BreakerState.CLOSED
        b.record_failure(FailureKind.CONNECT, now=1.0)
        assert b.state is BreakerState.OPEN

    def test_two_read_timeouts_trip(self):
        """The 120s budget is ~1.5x the worst legitimate request.

        2048 tokens at 28.9 tok/s/user (cold-prefix p50 at 5 concurrent) plus a
        6.45s TTFT p95 is about 78s. Anomalous, but not by enough to act on one.
        """
        b = _b()
        b.record_failure(FailureKind.READ_TIMEOUT, now=0.0)
        b.record_failure(FailureKind.READ_TIMEOUT, now=1.0)
        assert b.state is BreakerState.OPEN

    def test_twenty_429s_never_trip(self):
        """PROBE_CONTRACT.md §3's mistake, transplanted into the router.

        A replica returning 429 is WORKING and asking for less load. Shedding it
        concentrates load on whatever remains, which is how "readiness goes red
        when busy" collapses a service at peak -- one layer up, same failure.
        """
        b = _b()
        for i in range(20):
            b.record_failure(FailureKind.THROTTLED, now=float(i))
        assert b.state is BreakerState.CLOSED
        assert b.score == 0

    def test_client_errors_never_trip(self):
        """A malformed request is the caller's fault, not a broken replica."""
        b = _b()
        for i in range(20):
            b.record_failure(FailureKind.CLIENT_ERROR, now=float(i))
        assert b.state is BreakerState.CLOSED

    def test_one_success_resets_the_score(self):
        """A full reset, not a decay -- the conservative direction."""
        b = _b()
        for i in range(5):
            b.record_failure(FailureKind.SERVER_ERROR, now=float(i))
        assert b.score == 5
        b.record_success(now=5.0)
        assert b.score == 0
        for i in range(5):
            b.record_failure(FailureKind.SERVER_ERROR, now=float(6 + i))
        assert b.state is BreakerState.CLOSED


class TestWedgeRequiresConfirmation:
    def test_one_observation_alerts_but_does_not_act(self):
        """The window it derives from was never measured.

        PROBE_CONTRACT.md §4's 120s is an estimate, and with one replica a false
        positive is a total outage, so the first observation is a warning only.
        """
        b = _b()
        b.record_wedge(suspected=True, now=0.0)
        assert b.state is BreakerState.CLOSED
        assert b.wedge_confirmations == 1

    def test_a_second_consecutive_observation_opens(self):
        b = _b()
        b.record_wedge(suspected=True, now=0.0)
        b.record_wedge(suspected=True, now=5.0)
        assert b.state is BreakerState.OPEN

    def test_the_signal_clearing_resets_the_confirmation(self):
        b = _b()
        b.record_wedge(suspected=True, now=0.0)
        b.record_wedge(suspected=False, now=5.0)
        b.record_wedge(suspected=True, now=10.0)
        assert b.state is BreakerState.CLOSED, "a non-consecutive pair acted"


class TestOpenAndReclose:
    def test_no_probe_before_the_open_window_elapses(self):
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        assert not b.due_for_probe(now=INITIAL_OPEN_SEC - 1)
        assert b.due_for_probe(now=INITIAL_OPEN_SEC + 0.1)

    def test_two_good_probes_close_it(self):
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_probe(True, now=INITIAL_OPEN_SEC)
        assert b.state is BreakerState.HALF_OPEN, "one probe must not be enough"
        b.record_probe(True, now=INITIAL_OPEN_SEC + PROBE_INTERVAL_SEC)
        assert b.state is BreakerState.CLOSED

    def test_a_live_wedge_signal_blocks_reclose(self):
        """/models answering 200 is a WEAK probe, and the stub proves it.

        --stub-fail-after leaves /models green while chat 503s, and a really
        wedged vLLM behaves identically, because the API server is not the engine
        loop.
        """
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_probe(True, now=20.0, wedge_suspected=True)
        b.record_probe(True, now=30.0, wedge_suspected=True)
        assert b.state is BreakerState.OPEN

    def test_the_open_window_doubles_then_caps(self):
        """Capped below the measured 153s cold start.

        A genuinely restarted pod is then found within a minute of being ready.
        """
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        seen = []
        t = INITIAL_OPEN_SEC
        for _ in range(8):
            b.record_probe(False, now=t)
            seen.append(b.open_sec)
            t += b.open_sec
        assert seen[0] == INITIAL_OPEN_SEC * 2
        assert max(seen) == MAX_OPEN_SEC
        assert seen[-1] == MAX_OPEN_SEC

    def test_reclosing_resets_the_window(self):
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_probe(False, now=20.0)
        assert b.open_sec > INITIAL_OPEN_SEC
        b.record_probe(True, now=60.0)
        b.record_probe(True, now=70.0)
        assert b.state is BreakerState.CLOSED
        assert b.open_sec == INITIAL_OPEN_SEC


class TestProbation:
    def test_threshold_is_halved_just_after_a_reclose(self):
        """Stops an open-close-open flap putting a full wave into a broken replica."""
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_probe(True, now=20.0)
        b.record_probe(True, now=30.0)
        assert b.state is BreakerState.CLOSED
        for i in range(3):
            b.record_failure(FailureKind.SERVER_ERROR, now=31.0 + i)
        assert b.state is BreakerState.OPEN, "probation threshold was not applied"

    def test_full_threshold_returns_after_the_probation_window(self):
        b = _b()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_probe(True, now=20.0)
        b.record_probe(True, now=30.0)
        later = 30.0 + PROBATION_SEC + 1
        for i in range(5):
            b.record_failure(FailureKind.SERVER_ERROR, now=later + i)
        assert b.state is BreakerState.CLOSED
        b.record_failure(FailureKind.SERVER_ERROR, now=later + 5)
        assert b.state is BreakerState.OPEN


class TestRoutingAdmission:
    def test_only_closed_replicas_are_routable(self):
        """Half-open is invisible to routing: no request is used as an experiment."""
        b = _b()
        assert b.allows()
        b.record_failure(FailureKind.CONNECT, now=0.0)
        b.record_failure(FailureKind.CONNECT, now=0.0)
        assert not b.allows()
        b.record_probe(True, now=20.0)
        assert b.state is BreakerState.HALF_OPEN
        assert not b.allows()
