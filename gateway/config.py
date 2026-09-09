"""Gateway configuration. Environment variables only.

THIS IS A HARD CONSTRAINT, NOT A PREFERENCE (PRD §4.4d, R10).

Staging is planned for Azure, production cloud is undecided, and OCI is
available as a testing environment. So the image must contain no cloud-specific
dependency at all: no OCI SDK, no instance-principal call, no Azure
managed-identity call, no Key Vault client. The platform injects secrets as
environment variables; we read them. That is the one mechanism every candidate
platform supports identically.

The practical test to apply to any change here: could this container start,
unmodified, on OCI and on Azure and on a laptop? If a setting can only be
resolved by asking a specific cloud's metadata service, it does not belong.

Style follows the consuming repo's config.py -- pydantic BaseSettings, tuning
constants as plain literals, comments that justify a default rather than restate
it -- with two deliberate departures, each explained at the point it is made
below: values are read at CONSTRUCTION rather than at import, and pydantic's own
environment source is switched off so there is exactly one parser.
"""
from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


# WHY THE LIST SETTINGS ARE DECLARED AS `str` AND EXPOSED VIA @property
#
# A field annotated `list[str]` is a COMPLEX type to pydantic-settings, and its
# environment source insists on JSON-decoding such a variable before any default
# here is consulted. So `UPSTREAM_URLS=http://host/v1` -- the obvious, documented,
# comma-separated form -- makes the process die at import with
# `SettingsError: error parsing value for field "UPSTREAM_URLS"`, and the only
# accepted spelling becomes a JSON array.
#
# That failure mode is unacceptable for this service specifically: PRD §4.4(d)
# makes environment variables the ONLY config surface precisely because the
# platform injects them, and a platform injecting a plain URL string would crash
# the container on boot with an error that names pydantic rather than the
# mistake.
#
# Storing the raw string and parsing it in a property keeps pydantic out of the
# decision entirely. The field names carry a _CSV suffix so they do not shadow
# the env var names the properties read.


class Settings(BaseSettings):
    """Every field reads its environment variable through `default_factory`.

    Not decoration. The consuming repo's config.py calls `os.getenv(...)` in the
    class body, which runs once at IMPORT and freezes the value for the life of
    the process. In production that is invisible, because the platform sets the
    environment before the process starts -- but it means the settings object
    cannot be constructed twice with different inputs, so config parsing is
    untestable and a parsing bug can only be found by starting the server. That
    is exactly how the UPSTREAM_URLS failure described above reached a running
    process instead of a red test.

    `default_factory` defers the read to construction, so `Settings()` in a test
    sees the environment that test set. Production behaviour is unchanged.
    """

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_kwargs):
        """Drop pydantic's own environment source. There must be exactly one parser.

        Left enabled, pydantic-settings ALSO binds every one of these variables,
        and its coercion rules are not the ones documented here:

          * `AUDIT_INCLUDE_PROMPT_TEXT=""` -- a perfectly ordinary way for a
            platform to express "unset" -- fails pydantic's bool validation and
            kills the process at import, rather than reading as false.
          * pydantic accepts "on"/"off"/"y"/"n" for booleans; `_flag` accepts only
            "1"/"true"/"yes". So the SAME variable means different things
            depending on which parser wins, and which one wins depends on whether
            the variable is set at all.

        Two parsers for one security-relevant flag is not a subtlety worth
        keeping. The `default_factory` on each field reads os.getenv explicitly,
        so removing this source leaves one code path with the documented
        semantics.
        """
        return (init_settings,)

    # ── Server ───────────────────────────────────────────────────────────────
    # 0.0.0.0 is correct HERE, unlike in the AI service. This process is an
    # authenticating edge: it is meant to receive forwarded traffic, and inside
    # a container the network namespace is the isolation boundary. What governs
    # exposure is the publish/Service in front of it.
    HOST: str = Field(
        # noqa on the next line, not this one: an authenticating edge is meant
        # to bind wide, and inside a container the network namespace is the
        # boundary. The publish/Service in front governs exposure.
        default_factory=lambda: os.getenv("GATEWAY_HOST", "0.0.0.0"))  # noqa: S104
    PORT: int = Field(default_factory=lambda: int(os.getenv("GATEWAY_PORT", "8080")))

    # ── Upstream vLLM ────────────────────────────────────────────────────────
    # Comma-separated so the router interface has something to route over from
    # day one, even though Phase 1 runs a single replica. Adding the plural later
    # would change the config surface after DevOps had built against it.
    UPSTREAM_URLS_CSV: str = Field(
        default_factory=lambda: os.getenv("UPSTREAM_URLS", "http://127.0.0.1:8001/v1"))
    # vLLM's single static --api-key. This is why the gateway exists: that key is
    # all the auth vLLM has, with no per-tenant identity, quota or audit trail
    # (PRD §5.1). It never leaves the private network.
    UPSTREAM_API_KEY: str = Field(default_factory=lambda: os.getenv("UPSTREAM_API_KEY", ""))
    # Split, for the reason given in the AI service's vllm backend: a decode
    # legitimately takes 10s+ under load, a CONNECT that takes 10s means the
    # replica is gone. One number cannot be right for both.
    UPSTREAM_CONNECT_TIMEOUT_SEC: float = Field(
        default_factory=lambda: float(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SEC", "5.0")))
    UPSTREAM_READ_TIMEOUT_SEC: float = Field(
        default_factory=lambda: float(os.getenv("UPSTREAM_READ_TIMEOUT_SEC", "120.0")))

    # ── Authentication ───────────────────────────────────────────────────────
    # Where the API key store lives. A JSON file is the Phase 1 answer because it
    # is inspectable and needs no extra infrastructure; the loader is behind an
    # interface so a database or secret manager can replace it without touching
    # the request path.
    API_KEY_STORE_PATH: str = Field(
        default_factory=lambda: os.getenv("API_KEY_STORE_PATH", "./keys.json"))
    # Pepper, applied on top of argon2id's per-key salt. Kept OUT of the key
    # store deliberately: a stolen store is then not enough to mount an offline
    # attack, because the pepper lives in the platform's secret injection and not
    # on the same disk.
    API_KEY_PEPPER: str = Field(default_factory=lambda: os.getenv("API_KEY_PEPPER", ""))

    # PEM public key (or JWKS-derived) for the service-to-service path. ASYMMETRIC
    # ONLY, and the asymmetry is the point: the C# API signs with a private key,
    # the gateway verifies with the public one, so a gateway compromise yields no
    # ability to mint tokens. A shared HMAC secret would.
    JWT_PUBLIC_KEY: str = Field(default_factory=lambda: os.getenv("JWT_PUBLIC_KEY", ""))
    JWT_ALGORITHM: str = Field(default_factory=lambda: os.getenv("JWT_ALGORITHM", "RS256"))
    JWT_ISSUER: str = Field(default_factory=lambda: os.getenv("JWT_ISSUER", "firstdata-api"))
    JWT_AUDIENCE: str = Field(
        default_factory=lambda: os.getenv("JWT_AUDIENCE", "bkn301-inference"))
    # Tokens should be minted with a <=5 minute expiry (PRD §5.3). This bounds
    # how far a clock skew may stretch that, and is not itself the lifetime.
    JWT_LEEWAY_SEC: int = Field(default_factory=lambda: int(os.getenv("JWT_LEEWAY_SEC", "30")))

    # ── Request validation ───────────────────────────────────────────────────
    # Rejected before the GPU sees it. Every one of these is a cheap check
    # standing in front of an expensive resource.
    #
    # The model allowlist is not decoration: without it a caller can name any
    # model string, and a gateway that forwards it blindly lets a tenant reach
    # whatever else that vLLM process happens to serve.
    ALLOWED_MODELS_CSV: str = Field(
        default_factory=lambda: os.getenv("ALLOWED_MODELS", "Qwen/Qwen3-8B-AWQ"))
    MAX_BODY_BYTES: int = Field(
        default_factory=lambda: int(os.getenv("MAX_BODY_BYTES", str(1 << 20))))
    # Ceiling on caller-supplied max_tokens. A caller asking for 32k output
    # occupies a decode slot for minutes and starves the other four users.
    MAX_COMPLETION_TOKENS: int = Field(
        default_factory=lambda: int(os.getenv("MAX_COMPLETION_TOKENS", "2048")))
    # Prompt ceiling. Server-side --max-model-len is the real limit; rejecting
    # here means an over-length request fails fast with a clear error instead of
    # being silently truncated by vLLM (PRD §6.3 C5).
    MAX_PROMPT_TOKENS: int = Field(
        default_factory=lambda: int(os.getenv("MAX_PROMPT_TOKENS", "7000")))

    # ── Audit ────────────────────────────────────────────────────────────────
    # METADATA ONLY. This flag exists so the choice is explicit and greppable,
    # not so it can be turned on: logging prompt text would convert an audit
    # trail into a PII store, on a platform handling investor and KYC data.
    # tests/test_audit_no_prompt_text.py asserts it stays off.
    AUDIT_LOG_PATH: str = Field(
        default_factory=lambda: os.getenv("AUDIT_LOG_PATH", "./logs/audit.jsonl"))
    AUDIT_INCLUDE_PROMPT_TEXT: bool = Field(
        default_factory=lambda: _flag("AUDIT_INCLUDE_PROMPT_TEXT", "false"))

    # ── Observability ────────────────────────────────────────────────────────
    METRICS_ENABLED: bool = Field(default_factory=lambda: _flag("METRICS_ENABLED", "true"))

    # ── Parsed views of the CSV settings ─────────────────────────────────────
    # Properties, not fields. See the note above _csv.

    @property
    def UPSTREAM_URLS(self) -> list[str]:  # noqa: N802 - matches the env var name
        return _csv(self.UPSTREAM_URLS_CSV)

    @property
    def ALLOWED_MODELS(self) -> list[str]:  # noqa: N802 - matches the env var name
        return _csv(self.ALLOWED_MODELS_CSV)


settings = Settings()
