"""The /metrics endpoint and the request instrumentation behind it.

Every count assertion is a DELTA. prometheus_client's registry is a process
global and counters accumulate across the whole session, so absolute values are
order-dependent. There is deliberately no registry-reset fixture: the metrics are
module-level singletons precisely so that re-registration cannot happen, and a
fixture that tore them down would be testing a shape the app never runs in.
"""
from __future__ import annotations

import pytest
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

from gateway import metrics as gw_metrics
from gateway.config import settings


def _count(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


class TestMetricsEndpoint:
    def test_is_unauthenticated(self, client):
        """Prometheus does not carry an API key, same as Kubernetes on /healthz."""
        assert client.get("/metrics").status_code == 200

    def test_content_type_is_prometheus_text(self, client):
        ct = client.get("/metrics").headers["content-type"]
        assert ct.startswith("text/plain")
        assert "version=0.0.4" in ct

    def test_body_is_not_json_encoded(self, client):
        r"""Returning a bare `str` from a FastAPI route yields "...\n..." as JSON.

        That is unparseable as Prometheus text, and is the defect the stub's own
        /metrics shipped with, so it is worth asserting rather than assuming.
        """
        assert "\\n" not in client.get("/metrics").text

    def test_exposition_reparses(self, client):
        families = list(text_string_to_metric_families(client.get("/metrics").text))
        assert families
        assert any(f.name.startswith("bkn301_gateway") for f in families)

    def test_disabled_returns_404_and_empty_body(self, client, monkeypatch):
        """404, not 403 or 503 -- see the handler docstring for why each is wrong."""
        monkeypatch.setattr(settings, "METRICS_ENABLED", False, raising=False)
        r = client.get("/metrics")
        assert r.status_code == 404
        assert r.content == b""

    def test_collection_continues_while_exposure_is_off(self, client, monkeypatch, live_key):
        """The flag gates the endpoint, not the middleware."""
        monkeypatch.setattr(settings, "METRICS_ENABLED", False, raising=False)
        before = _count("bkn301_gateway_requests_total", route="/v1/models",
                        method="GET", status="200", auth_method="apikey")
        client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        monkeypatch.setattr(settings, "METRICS_ENABLED", True, raising=False)
        after = _count("bkn301_gateway_requests_total", route="/v1/models",
                       method="GET", status="200", auth_method="apikey")
        assert after == before + 1


class TestRequestMetrics:
    def test_a_401_is_counted(self, client):
        """The reason this is middleware and not a call in each handler.

        `require_principal` raises from the dependency, so a rejected credential
        never reaches a handler body -- which is why the audit log has no record
        of one despite auth/keys.py claiming it does. Without middleware there is
        no way to count these at all.
        """
        before = _count("bkn301_gateway_requests_total", route="/v1/models",
                        method="GET", status="401", auth_method="none")
        client.get("/v1/models")
        after = _count("bkn301_gateway_requests_total", route="/v1/models",
                       method="GET", status="401", auth_method="none")
        assert after == before + 1

    def test_auth_method_is_labelled(self, client, live_key):
        before = _count("bkn301_gateway_requests_total", route="/v1/models",
                        method="GET", status="200", auth_method="apikey")
        client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        after = _count("bkn301_gateway_requests_total", route="/v1/models",
                       method="GET", status="200", auth_method="apikey")
        assert after == before + 1

    def test_route_label_is_the_template_not_the_path(self, client):
        client.get("/v1/models")
        assert _count("bkn301_gateway_requests_total", route="/v1/models",
                      method="GET", status="401", auth_method="none") > 0

    def test_unmatched_path_does_not_mint_a_series_per_url(self, client):
        for p in ("/nope/one", "/nope/two", "/nope/three"):
            client.get(p)
        assert _count("bkn301_gateway_requests_total", route="__unmatched__",
                      method="GET", status="404", auth_method="none") >= 3

    def test_rejection_reason_is_counted(self, client, live_key):
        before = _count("bkn301_gateway_rejections_total", reason="model_not_allowed")
        client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {live_key}"},
                    json={"model": "meta-llama/Llama-3-70B",
                          "messages": [{"role": "user", "content": "hi"}]})
        after = _count("bkn301_gateway_rejections_total", reason="model_not_allowed")
        assert after == before + 1

    def test_unknown_reason_folds_to_other(self):
        before = _count("bkn301_gateway_rejections_total", reason="other")
        gw_metrics.record_rejection("a_reason_nobody_registered")
        assert _count("bkn301_gateway_rejections_total", reason="other") == before + 1

    def test_scrape_of_self_is_not_counted(self, client):
        before = _count("bkn301_gateway_requests_total", route="/metrics",
                        method="GET", status="200", auth_method="none")
        client.get("/metrics")
        after = _count("bkn301_gateway_requests_total", route="/metrics",
                       method="GET", status="200", auth_method="none")
        assert after == before == 0


class TestCardinalityIsBounded:
    def test_no_keyid_label_anywhere(self, client, live_key):
        """/metrics is unauthenticated; keyid is unbounded AND tenant identity."""
        client.get("/v1/models", headers={"Authorization": f"Bearer {live_key}"})
        offenders = [
            f"{metric.name}{sample.labels}"
            for metric in REGISTRY.collect()
            if metric.name.startswith("bkn301_gateway")
            for sample in metric.samples
            if "keyid" in sample.labels or "key_id" in sample.labels
        ]
        assert not offenders, f"tenant identity on an unauthenticated surface: {offenders}"

    def test_no_upstream_url_is_published(self, client):
        """Replicas are labelled by index: the URLs are private-subnet addresses."""
        values = [
            v for metric in REGISTRY.collect()
            if metric.name.startswith("bkn301_gateway")
            for sample in metric.samples for v in sample.labels.values()
        ]
        assert not [v for v in values if v.startswith("http")], values

    @pytest.mark.parametrize("model", ["totally-made-up", "../../etc/passwd", ""])
    def test_caller_supplied_model_folds_to_other(self, model):
        """`chat.py` records the caller's string when rejecting an unknown model.

        Unbounded, that is a memory leak an authenticated tenant can drive, with
        the series published to an anonymous scraper.
        """
        assert gw_metrics.safe_model(model) == "other"

    def test_allowlisted_model_survives(self):
        assert gw_metrics.safe_model("Qwen/Qwen3-8B-AWQ") == "Qwen/Qwen3-8B-AWQ"


class TestRegistryInvariant:
    def test_create_app_twice_does_not_raise(self):
        """Pins the module-level-singleton rule.

        A metric built inside create_app() raises `Duplicated timeseries` on the
        second call -- and conftest's `client` fixture calls it once per test, so
        this breaks the whole suite rather than one case.
        """
        from gateway.main import create_app
        create_app()
        create_app()


class TestHistogramBuckets:
    def test_duration_buckets_reach_the_measured_working_range(self):
        """vLLM's own equivalent stops at 1.0s and put 168/168 requests in +Inf.

        Phase 1 measured a 10.15s p50 through this gateway.
        """
        assert 30.0 in gw_metrics.DURATION_BUCKETS
        finite = [b for b in gw_metrics.DURATION_BUCKETS if b != float("inf")]
        assert max(finite) >= 60.0

    def test_ttft_buckets_resolve_both_measured_regimes(self):
        """Prefix-cache hit: p50 92-132ms. Miss: p50 1.4-3.8s, p95 6.45s."""
        b = gw_metrics.TTFT_BUCKETS
        assert any(x <= 0.15 for x in b), "cannot resolve a prefix-cache hit"
        assert any(4.0 <= x <= 8.0 for x in b), "cannot resolve a prefix-cache miss"
