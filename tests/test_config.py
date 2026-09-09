"""Configuration parsing.

These exist because of a real failure, not a hypothetical one. Declaring
`UPSTREAM_URLS: list[str]` on a pydantic BaseSettings made the process die at
import the first time the variable was actually set in the environment:
pydantic-settings treats a list-annotated field as complex and JSON-decodes the
env var before any default is consulted, so the plain comma-separated form
raised `SettingsError: error parsing value for field "UPSTREAM_URLS"`.

The unit suite missed it entirely, because nothing in it set the variable -- the
default path worked fine. Only starting the server with a real environment
caught it. That is the gap these tests close: every setting the platform is
expected to inject is exercised as an actual environment variable.

This matters more here than in most services. PRD §4.4(d) makes environment
variables the ONLY configuration surface, because production cloud is undecided
and no cloud SDK may enter the image. A config surface that crashes on the
documented input form is therefore a deployment blocker, not a papercut.
"""
from __future__ import annotations

import pytest

from gateway.config import Settings


class TestListSettingsFromEnvironment:
    """The regression. Plain comma-separated values must work."""

    def test_single_upstream_url(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_URLS", "http://vllm-a:8000/v1")
        assert Settings().UPSTREAM_URLS == ["http://vllm-a:8000/v1"]

    def test_multiple_upstream_urls(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_URLS", "http://vllm-a:8000/v1,http://vllm-b:8000/v1")
        assert Settings().UPSTREAM_URLS == [
            "http://vllm-a:8000/v1",
            "http://vllm-b:8000/v1",
        ]

    def test_whitespace_is_tolerated(self, monkeypatch):
        """A human editing a Helm values file will add spaces after commas."""
        monkeypatch.setenv("UPSTREAM_URLS", " http://a:8000/v1 , http://b:8000/v1 ")
        assert Settings().UPSTREAM_URLS == ["http://a:8000/v1", "http://b:8000/v1"]

    def test_allowed_models_from_env(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_MODELS", "Qwen/Qwen3-8B-AWQ,Qwen/Qwen3-8B")
        assert Settings().ALLOWED_MODELS == ["Qwen/Qwen3-8B-AWQ", "Qwen/Qwen3-8B"]

    def test_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("UPSTREAM_URLS", raising=False)
        monkeypatch.delenv("ALLOWED_MODELS", raising=False)
        s = Settings()
        assert s.UPSTREAM_URLS == ["http://127.0.0.1:8001/v1"]
        assert s.ALLOWED_MODELS == ["Qwen/Qwen3-8B-AWQ"]

    def test_empty_entries_are_dropped(self, monkeypatch):
        """A trailing comma must not produce an empty upstream URL."""
        monkeypatch.setenv("UPSTREAM_URLS", "http://a:8000/v1,,")
        assert Settings().UPSTREAM_URLS == ["http://a:8000/v1"]


class TestScalarSettingsFromEnvironment:
    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("yes", True), ("TRUE", True),
        ("0", False), ("false", False), ("no", False), ("", False),
    ])
    def test_flag_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("AUDIT_INCLUDE_PROMPT_TEXT", raw)
        assert Settings().AUDIT_INCLUDE_PROMPT_TEXT is expected

    def test_numeric_settings(self, monkeypatch):
        monkeypatch.setenv("MAX_COMPLETION_TOKENS", "512")
        monkeypatch.setenv("UPSTREAM_READ_TIMEOUT_SEC", "45.5")
        s = Settings()
        assert s.MAX_COMPLETION_TOKENS == 512
        assert s.UPSTREAM_READ_TIMEOUT_SEC == 45.5


class TestNoCloudSdk:
    """PRD §4.4(d) / R10: the image must carry no cloud-specific dependency.

    Production cloud is undecided (OCI or Azure), staging is planned for Azure,
    and OCI is the testing environment. A cloud SDK creeping into the dependency
    tree would tie the image to one of them, which is the portability
    requirement this repo exists to keep.
    """

    FORBIDDEN = ("oci", "azure", "boto3", "botocore", "google.cloud")

    def test_gateway_imports_no_cloud_sdk(self):
        import sys

        import gateway.audit  # noqa: F401
        import gateway.auth  # noqa: F401
        import gateway.config  # noqa: F401
        import gateway.main  # noqa: F401

        offenders = sorted(
            name for name in sys.modules
            if any(name == f or name.startswith(f + ".") for f in self.FORBIDDEN)
        )
        assert not offenders, f"cloud SDK imported: {offenders}"
