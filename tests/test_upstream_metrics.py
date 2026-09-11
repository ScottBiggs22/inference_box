"""Parsing vLLM's exposition, and deriving the wedge signature from it.

The parser tests run against the REAL capture from the Phase 1 box rather than a
hand-written fixture. A hand-written one would be written from the same
understanding as the parser, so it would agree with it whether or not either was
right -- which is precisely how a scraper ends up matching nothing in production.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from gateway.upstream.metrics_scraper import WedgeDetector, metrics_url, parse

REAL_SCRAPE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "results" / "phase1-vastai-a10" / "vllm-metrics-final.txt"
)


@pytest.fixture(scope="module")
def real_text() -> str:
    return REAL_SCRAPE.read_text(encoding="utf-8")


class TestParserAgainstRealVllm:
    def test_reads_the_counter_the_wedge_signature_needs(self, real_text):
        """The trap this whole module is shaped around.

        prometheus_client's parser STRIPS `_total` from counter FAMILY names, so
        the family here is `vllm:generation_tokens` while only the sample keeps
        `vllm:generation_tokens_total` -- which is the name PROBE_CONTRACT.md §4
        and PRD §4.6 both use. Matching on family names finds nothing, silently,
        and the detector then sees a permanently flat counter.
        """
        families = {f.name for f in text_string_to_metric_families(real_text)}
        assert "vllm:generation_tokens_total" not in families, (
            "the premise of this test has changed; re-check the parser"
        )
        assert parse(real_text)["vllm:generation_tokens_total"] == 74322.0

    def test_reads_the_other_series(self, real_text):
        snap = parse(real_text)
        assert snap["vllm:prefix_cache_hits_total"] == 730928.0
        assert snap["vllm:prefix_cache_queries_total"] == 811227.0
        assert snap["vllm:num_requests_waiting"] == 0.0

    def test_ignores_created_siblings(self, real_text):
        """`vllm:generation_tokens_created` is a GAUGE holding a boot timestamp.

        It never changes, so a prefix-matching parser that picks it up sees a
        counter that is flat forever -- i.e. it wedge-alerts forever.
        """
        assert "vllm:generation_tokens_created" not in parse(real_text)
        assert parse(real_text)["vllm:generation_tokens_total"] < 1e6, (
            "a 1.78e9 value means a _created timestamp was read as the counter"
        )

    def test_histogram_with_a_total_suffix_is_not_mistaken_for_a_counter(self, real_text):
        """`vllm:iteration_tokens_total` is a histogram despite the suffix."""
        assert "vllm:iteration_tokens_total" not in parse(real_text)


class TestLabelAggregation:
    def _two_engines(self, text: str) -> str:
        extra = [
            '# TYPE vllm:num_requests_waiting gauge',
            'vllm:num_requests_waiting{engine="1",model_name="m"} 4.0',
            '# TYPE vllm:kv_cache_usage_perc gauge',
            'vllm:kv_cache_usage_perc{engine="1",model_name="m"} 0.9',
        ]
        return text + "\n".join(extra) + "\n"

    def test_counts_sum_across_engines(self, real_text):
        assert parse(self._two_engines(real_text))["vllm:num_requests_waiting"] == 4.0

    def test_a_ratio_is_maxed_not_summed(self, real_text):
        """kv_cache_usage_perc is a fraction. Summed over two engines it reads 1.8.

        The bug class a test against the old UNLABELLED stub could never catch:
        with one sample per name, every aggregation looks correct.
        """
        assert parse(self._two_engines(real_text))["vllm:kv_cache_usage_perc"] == 0.9


class TestMetricsUrl:
    @pytest.mark.parametrize("base,expected", [
        ("http://h:8000/v1", "http://h:8000/metrics"),
        ("http://h:8000/v1/", "http://h:8000/metrics"),
        ("http://h:8000", "http://h:8000/metrics"),
        ("http://h:8000/", "http://h:8000/metrics"),
        ("http://h:8000/proxy/v1", "http://h:8000/proxy/metrics"),
    ])
    def test_derivation(self, base, expected):
        """vLLM serves /metrics at the ROOT; UPSTREAM_URLS carry the /v1 prefix.

        Getting this wrong is a silent 404 that reads as a reachable upstream
        whose exposition will not parse.
        """
        assert metrics_url(base) == expected


class TestWedgeDetector:
    """Pure state machine, injected clock, no sleeping and no sockets."""

    def _snap(self, waiting: float, gen: float) -> dict[str, float]:
        return {"vllm:num_requests_waiting": waiting,
                "vllm:generation_tokens_total": gen}

    def test_queued_work_with_a_flat_counter_trips_after_the_window(self):
        d = WedgeDetector(window_sec=10.0)
        d.observe(self._snap(3, 100), now=0.0)
        d.observe(self._snap(3, 100), now=1.0)
        assert not d.suspected(now=5.0)
        assert d.suspected(now=11.5)
        assert d.stall_seconds(now=11.0) == pytest.approx(10.0)

    def test_idle_is_not_wedged(self):
        """The false positive that costs a 60s model reload.

        Nothing queued and no tokens generated is a quiet service, not a broken
        one. PROBE_CONTRACT.md §4 is explicit that a late alert beats this.
        """
        d = WedgeDetector(window_sec=10.0)
        for t in range(0, 100, 5):
            d.observe(self._snap(0, 100), now=float(t))
        assert not d.suspected(now=100.0)
        assert d.stall_seconds(now=100.0) == 0.0

    def test_an_advancing_counter_clears_the_stall(self):
        d = WedgeDetector(window_sec=10.0)
        d.observe(self._snap(3, 100), now=0.0)
        d.observe(self._snap(3, 100), now=1.0)
        assert d.stall_since is not None
        d.observe(self._snap(3, 101), now=2.0)
        assert d.stall_since is None

    def test_a_counter_reset_is_a_restart_not_a_stall(self):
        """vLLM restarting must not trip the breaker on every deploy."""
        d = WedgeDetector(window_sec=10.0)
        d.observe(self._snap(3, 500), now=0.0)
        d.observe(self._snap(3, 500), now=1.0)
        assert d.stall_since is not None
        d.observe(self._snap(3, 0), now=2.0)
        assert d.stall_since is None

    def test_a_failed_scrape_neither_advances_nor_clears(self):
        """No information is not evidence of recovery.

        Clearing here would make a wedged engine that has ALSO stopped serving
        /metrics look healthy -- the worst available reading.
        """
        d = WedgeDetector(window_sec=10.0)
        d.observe(self._snap(3, 100), now=0.0)
        d.observe(self._snap(3, 100), now=1.0)
        d.on_scrape_failure()
        assert d.stall_since == 1.0
        assert d.suspected(now=12.0)

    def test_stall_seconds_is_exported_below_the_window(self):
        """The series that replaces PROBE_CONTRACT.md §4's estimate with data."""
        d = WedgeDetector(window_sec=120.0)
        d.observe(self._snap(1, 5), now=0.0)
        d.observe(self._snap(1, 5), now=1.0)
        assert d.stall_seconds(now=31.0) == pytest.approx(30.0)
        assert not d.suspected(now=31.0)

    def test_a_missing_counter_is_never_read_as_a_stall(self):
        d = WedgeDetector(window_sec=1.0)
        d.observe({"vllm:num_requests_waiting": 5.0}, now=0.0)
        d.observe({"vllm:num_requests_waiting": 5.0}, now=10.0)
        assert not d.suspected(now=100.0)


class TestStubEmulatesRealVllm:
    """These fail against the pre-Phase-2 stub, and that is the point.

    It emitted unlabelled series under application/json, so a scraper written and
    tested against it parsed cleanly and then matched nothing on a real server.
    """

    def test_metrics_is_prometheus_text_not_json(self, live_stub):
        r = httpx.get(f"{live_stub()}/metrics")
        assert r.headers["content-type"].startswith("text/plain")
        assert "\\n" not in r.text

    def test_series_carry_the_real_vllm_labels(self, live_stub):
        r = httpx.get(f"{live_stub()}/metrics")
        fams = {f.name: f for f in text_string_to_metric_families(r.text)}
        sample = fams["vllm:num_requests_waiting"].samples[0]
        assert sample.labels["engine"] == "0"
        assert sample.labels["model_name"] == "Qwen/Qwen3-8B-AWQ"

    def test_the_parser_reads_the_stub_too(self, live_stub):
        snap = parse(httpx.get(f"{live_stub()}/metrics").text)
        assert "vllm:generation_tokens_total" in snap
        assert "vllm:num_requests_waiting" in snap

    def test_wedge_mode_produces_the_signature(self, live_stub):
        """A wedged engine keeps answering /health. That is why this needs metrics.

        Asserts the STUB can emit the signature; the detector's own behaviour is
        covered above with an injected clock, so nothing here has to sleep for a
        window.
        """
        base = live_stub("--stub-wedge")
        for _ in range(2):
            with pytest.raises(httpx.TimeoutException):
                httpx.post(f"{base}/v1/chat/completions", timeout=0.5,
                           json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert httpx.get(f"{base}/health").status_code == 200, (
            "a wedged engine still answers its health probe -- if this fails, the "
            "stub is simulating a crash rather than a wedge"
        )
        snap = parse(httpx.get(f"{base}/metrics").text)
        assert snap["vllm:num_requests_waiting"] >= 2
        assert snap["vllm:generation_tokens_total"] == 0.0
