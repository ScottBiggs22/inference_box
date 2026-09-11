"""Scrape vLLM's own /metrics, and derive the wedge signal from it.

WHAT THIS IS FOR
================
A wedged engine does not fail an HTTP probe: the process is alive and the socket
answers. The signature is a relationship between two series over time
(PROBE_CONTRACT.md §4):

    vllm:num_requests_waiting > 0  AND  vllm:generation_tokens_total flat for N s

No endpoint can express that, so something has to hold state across scrapes.
This does, and the circuit breaker consumes it.

WHAT IS DELIBERATELY NOT EXPORTED
=================================
vLLM's own series are not re-exported. Prometheus should scrape vLLM directly
for those: duplicating them doubles cardinality and, worse, makes this process
serve stale values that look live whenever its own scrape fails. Only the
gateway's derived view is published -- reachability, scrape age, stall duration,
verdict.

THREE PARSER TRAPS, ALL CLOSED BY ONE RULE
==========================================
Match exact `sample.name` against an explicit allowlist. Never `family.name`,
never `startswith`. Each of these was verified against the real capture in
`results/phase1-vastai-a10/vllm-metrics-final.txt`:

 1. `text_string_to_metric_families` STRIPS `_total` from counter FAMILY names.
    The family is `vllm:generation_tokens`; only the sample keeps
    `vllm:generation_tokens_total`, which is the name PROBE_CONTRACT.md and the
    PRD both use. Looking it up by family name finds nothing, silently, and the
    detector then sees a permanently flat counter.
 2. Every counter has a `<name>_created` GAUGE sibling whose value is the
    process's boot timestamp. It never changes, so a `startswith` match makes the
    counter look permanently flat -- i.e. wedge-alerting forever.
 3. `vllm:iteration_tokens_total` is a HISTOGRAM despite the `_total` suffix, so
    anything keying on that suffix will eat a bucket value.

AGGREGATION IS PER-SERIES, NOT BLANKET
======================================
Every vLLM series carries `engine` and `model_name`. Counts sum across engines;
`kv_cache_usage_perc` is a RATIO and must be maxed -- summed across two engines
it would read 1.8. This is exactly the bug a test against the old unlabelled stub
could never catch, because one sample per name makes any aggregation look right.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from prometheus_client.parser import text_string_to_metric_families

from gateway import metrics as gw_metrics
from gateway.config import settings

logger = logging.getLogger(__name__)

# sample name -> how to combine samples that differ only by engine/model.
SERIES: dict[str, str] = {
    "vllm:num_requests_waiting": "sum",
    "vllm:num_requests_running": "sum",
    "vllm:generation_tokens_total": "sum",
    "vllm:prompt_tokens_total": "sum",
    "vllm:num_preemptions_total": "sum",
    "vllm:prefix_cache_hits_total": "sum",
    "vllm:prefix_cache_queries_total": "sum",
    # A ratio in [0,1]. Summing across engines is meaningless.
    "vllm:kv_cache_usage_perc": "max",
}


def metrics_url(base: str) -> str:
    """Derive the /metrics URL from an UPSTREAM_URLS entry.

    vLLM serves /metrics at the ROOT, while UPSTREAM_URLS entries carry the
    OpenAI `/v1` prefix. Getting this wrong yields a silent 404 that reads as a
    reachable-but-unparseable upstream, so it is a named function with tests
    rather than an inline f-string.
    """
    trimmed = base.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return f"{trimmed}/metrics"


def parse(text: str) -> dict[str, float]:
    """Reduce a vLLM exposition to the handful of numbers this module uses."""
    out: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            how = SERIES.get(s.name)          # exact sample name; see trap 1-3
            if how is None:
                continue
            if s.name not in out:
                out[s.name] = s.value
            elif how == "sum":
                out[s.name] += s.value
            else:
                out[s.name] = max(out[s.name], s.value)
    return out


@dataclass
class WedgeDetector:
    """Tracks the wedge signature for one replica.

    Pure and clock-injected so the state machine is testable without sleeping.
    """

    window_sec: float
    _last_gen_tokens: float | None = field(default=None, repr=False)
    stall_since: float | None = None

    def observe(self, snapshot: dict[str, float], now: float) -> None:
        waiting = snapshot.get("vllm:num_requests_waiting", 0.0)
        gen = snapshot.get("vllm:generation_tokens_total")
        if gen is None:
            # The series we key on is absent -- a version change, or a parser
            # regression. Treated as no information, never as a stall.
            logger.warning(
                "vllm:generation_tokens_total absent from the upstream scrape; "
                "wedge detection is inactive for this replica"
            )
            return

        previous, self._last_gen_tokens = self._last_gen_tokens, gen
        if previous is None:
            return

        if gen < previous:
            # A counter going backwards means vLLM restarted. That is not a
            # stall, and treating it as one would trip the breaker on every
            # deploy.
            self.stall_since = None
            return

        if waiting > 0 and gen == previous:
            if self.stall_since is None:
                self.stall_since = now
        else:
            # Either nothing is queued (idle is not wedged -- this is the false
            # positive that would cost a 60s model reload) or tokens moved.
            self.stall_since = None

    def on_scrape_failure(self) -> None:
        """A failed scrape is NO INFORMATION: neither advance nor clear.

        Clearing would make a wedged engine that has also stopped serving
        /metrics look recovered, which is the worst possible reading. The failure
        is reported separately, and more loudly, as upstream_up = 0.
        """

    def stall_seconds(self, now: float) -> float:
        return 0.0 if self.stall_since is None else max(0.0, now - self.stall_since)

    def suspected(self, now: float) -> bool:
        return self.stall_seconds(now) >= self.window_sec


class UpstreamMetricsPoller:
    """Polls every replica's /metrics and publishes the derived view."""

    def __init__(self) -> None:
        self._detectors: dict[str, WedgeDetector] = {}
        self._last_ok: dict[str, float] = {}
        self._failing: set[str] = set()

    def _detector(self, url: str) -> WedgeDetector:
        if url not in self._detectors:
            self._detectors[url] = WedgeDetector(
                window_sec=settings.UPSTREAM_WEDGE_WINDOW_SEC
            )
        return self._detectors[url]

    def wedge_suspected(self, url: str) -> bool:
        """The breaker's read side."""
        d = self._detectors.get(url)
        return bool(d and d.suspected(time.monotonic()))

    async def poll_once(self, client, urls: list[str]) -> None:
        for index, url in enumerate(urls):
            await self._poll_one(client, str(index), url)

    async def _poll_one(self, client, label: str, url: str) -> None:
        now = time.monotonic()
        detector = self._detector(url)
        try:
            # An explicit short timeout, NOT the pool's 120s read timeout: a
            # /metrics scrape that takes two minutes IS the wedge, and blocking
            # the poll loop on it stops detection at the moment it matters.
            resp = await client.get(
                metrics_url(url), timeout=settings.UPSTREAM_METRICS_TIMEOUT_SEC
            )
            if resp.status_code >= 400:
                self._fail(label, url, "http_status", detector)
                return
            snapshot = parse(resp.text)
        except Exception as e:  # noqa: BLE001 - every failure is the same verdict
            kind = "timeout" if "Timeout" in type(e).__name__ else "connect"
            self._fail(label, url, kind, detector)
            return

        if not snapshot:
            self._fail(label, url, "parse", detector)
            return

        detector.observe(snapshot, now)
        self._last_ok[url] = now
        if url in self._failing:
            self._failing.discard(url)
            logger.info("upstream %s /metrics is reachable again", label)

        gw_metrics.upstream_up.labels(upstream=label).set(1)
        gw_metrics.upstream_scrape_age_seconds.labels(upstream=label).set(0.0)
        gw_metrics.upstream_wedge_stall_seconds.labels(upstream=label).set(
            detector.stall_seconds(now)
        )
        gw_metrics.upstream_wedge_suspected.labels(upstream=label).set(
            1 if detector.suspected(now) else 0
        )

    def _fail(self, label: str, url: str, kind: str, detector: WedgeDetector) -> None:
        detector.on_scrape_failure()
        gw_metrics.upstream_scrape_failures_total.labels(
            upstream=label, kind=kind).inc()
        gw_metrics.upstream_up.labels(upstream=label).set(0)
        last = self._last_ok.get(url)
        if last is not None:
            gw_metrics.upstream_scrape_age_seconds.labels(upstream=label).set(
                time.monotonic() - last
            )
        # Log the TRANSITION, then stay quiet. Every unit test points the pool at
        # a closed port, so logging each failure would put a line in the output
        # every few seconds and end "a green run is one line". It is also the
        # right production behaviour: a week-long outage should not write 120,000
        # identical lines.
        if url not in self._failing:
            self._failing.add(url)
            logger.warning("upstream %s /metrics scrape failing (%s)", label, kind)
        else:
            logger.debug("upstream %s /metrics still failing (%s)", label, kind)


upstream_metrics = UpstreamMetricsPoller()
