"""/v1/chat/completions — the one route that reaches a GPU.

Everything here runs BEFORE the request is forwarded, and that ordering is the
design: each check is cheap and stands in front of a resource that is not. A
request rejected here costs microseconds; the same request accepted occupies a
decode slot on a card shared by at most five concurrent users.
"""
from __future__ import annotations

import json
import logging
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from gateway import metrics
from gateway.audit import AuditRecord, audit_log
from gateway.auth import Principal, require_principal
from gateway.config import settings
from gateway.quota import QuotaExceeded, quota
from gateway.upstream.client import AllReplicasOpen, UpstreamUnreachable, pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["inference"])


def _reject(status_code: int, reason: str, principal: Principal,
            model: str | None, started: float,
            headers: dict[str, str] | None = None) -> HTTPException:
    # Counted from the same place, with the same vocabulary, as the audit record
    # -- so the two cannot drift apart.
    metrics.record_rejection(reason)
    audit_log.write(
        AuditRecord(
            timestamp=audit_log.now(),
            event="chat.completions",
            keyid=principal.keyid,
            subject=principal.subject,
            auth_method=principal.method,
            model=model,
            status=status_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            prompt_tokens=0,
            completion_tokens=0,
            upstream=None,
            reason=reason,
        )
    )
    return HTTPException(status_code=status_code, detail=reason, headers=headers)


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Cheap word-count estimate, deliberately not a real tokenizer.

    Loading a tokenizer here would tie the gateway to a specific model's
    vocabulary -- and the gateway fronts a model allowlist, not one model. The
    estimate only has to be good enough to reject the obviously-oversized before
    the GPU sees them; vLLM's own --max-model-len is the real limit, and it
    errors precisely.

    It errs LOW (words < tokens), so it will not reject a legitimate request. It
    is a guard rail, not a budget.
    """
    return sum(len(str(m.get("content", "")).split()) for m in messages)


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    principal: Principal = Depends(require_principal),
):
    started = time.perf_counter()

    raw = await request.body()
    if len(raw) > settings.MAX_BODY_BYTES:
        raise _reject(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                      "body_too_large", principal, None, started)

    try:
        body = await request.json()
    except Exception as e:
        raise _reject(status.HTTP_400_BAD_REQUEST,
                      "invalid_json", principal, None, started) from e

    if not isinstance(body, dict):
        raise _reject(status.HTTP_400_BAD_REQUEST, "invalid_body", principal, None, started)

    model = body.get("model")
    if not principal.has_scope("chat"):
        raise _reject(status.HTTP_403_FORBIDDEN, "scope_denied", principal, model, started)

    # Model allowlist. Without it a caller names any model string and reaches
    # whatever else that vLLM process serves.
    if model not in settings.ALLOWED_MODELS:
        raise _reject(status.HTTP_400_BAD_REQUEST,
                      "model_not_allowed", principal, model, started)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _reject(status.HTTP_400_BAD_REQUEST,
                      "messages_required", principal, model, started)

    if _estimate_prompt_tokens(messages) > settings.MAX_PROMPT_TOKENS:
        raise _reject(status.HTTP_400_BAD_REQUEST,
                      "prompt_too_long", principal, model, started)

    # Ceiling on caller-supplied max_tokens. Clamped rather than rejected: a
    # caller asking for more than they may have is served a shorter answer, not
    # an error, which is the friendlier behaviour for the same protection. One
    # 8k-context request costs what twenty short ones do (PRD §5.3).
    #
    # The parse is guarded, and it has to be: `int(requested)` on its own raises
    # on `{"max_tokens": "abc"}` and on `{"max_tokens": {}}`, which turned a
    # malformed request into a 500 from the one layer whose entire job is
    # rejecting malformed requests before the GPU sees them. A non-positive
    # value is caught in the same place, because it is not a smaller request --
    # vLLM has no sensible reading of it, and clamping UP to the ceiling would
    # be the opposite of what the caller asked.
    requested = body.get("max_tokens")
    if requested is None:
        body["max_tokens"] = settings.MAX_COMPLETION_TOKENS
    else:
        try:
            wanted = int(requested)
        except (TypeError, ValueError):
            raise _reject(status.HTTP_400_BAD_REQUEST,
                          "max_tokens_invalid", principal, model, started) from None
        if wanted < 1:
            raise _reject(status.HTTP_400_BAD_REQUEST,
                          "max_tokens_invalid", principal, model, started)
        body["max_tokens"] = min(wanted, settings.MAX_COMPLETION_TOKENS)

    streaming = bool(body.get("stream"))

    # Per-key budget and concurrency (PRD §5.3), the last gate before the GPU.
    # Only an API-key principal carries a keyid; a JWT principal has neither
    # field set and skips both -- JWT is the trusted service-to-service path
    # quotas exist to constrain the OTHER credential type against. Budget is
    # checked first: it is a comparison, not a mutation, so the cheaper check
    # runs before the one that changes shared state. Deliberately placed AFTER
    # every rejecting validation above: acquiring a concurrency slot commits
    # the gateway to releasing it, so nothing between acquire and the
    # forwarding call below may raise without going through that release.
    keyid = principal.keyid
    if keyid is not None and principal.token_budget is not None:
        try:
            quota.check_budget(keyid, principal.token_budget)
        except QuotaExceeded as e:
            raise _reject(status.HTTP_429_TOO_MANY_REQUESTS, e.reason,
                          principal, model, started) from e

    # Set only once `quota.acquire` actually succeeds, so the release below
    # never fires for a key that was never granted a slot.
    release_keyid: str | None = None
    if keyid is not None and principal.max_concurrency is not None:
        try:
            quota.acquire(keyid, principal.max_concurrency)
        except QuotaExceeded as e:
            raise _reject(status.HTTP_429_TOO_MANY_REQUESTS, e.reason,
                          principal, model, started) from e
        release_keyid = keyid

    # pick + attempt + bounded retry with jitter on 429/502/503/504, and breaker
    # recording, are all owned by the pool now -- see UpstreamPool._with_retry.
    # This is the only place either exception surfaces, for both paths, which is
    # the point: a future call site cannot bypass the breaker or the retry bound
    # by hand-rolling its own `pool.client.post(...)`.
    try:
        if streaming:
            # _stream owns release_keyid from here: the slot must survive past
            # this function returning, since the request is not done until the
            # stream is. See _stream's own try/except and relay()'s finally.
            return await _stream(body, principal, model, started,
                                 keyid=keyid, release_keyid=release_keyid)
        upstream, resp = await pool.request("/chat/completions", json=body)
    except AllReplicasOpen as e:
        # 503, not 502. 502 asserts "I reached the upstream and it was broken";
        # 503 says "I am not accepting this right now, try later", which is the
        # truthful and retryable status here -- and the one the OpenAI client
        # libraries already back off on.
        raise _reject(
            status.HTTP_503_SERVICE_UNAVAILABLE, "all_replicas_open",
            principal, model, started,
            headers={"Retry-After": str(max(1, math.ceil(e.retry_after)))},
        ) from e
    except UpstreamUnreachable as e:
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_unreachable", principal, model, started) from e
    finally:
        # `finally` runs even on the streaming branch's `return`, so this must
        # be excluded there explicitly -- `_stream` already owns release for
        # that path (see above), and releasing here too would double-release
        # the same slot the moment a stream is requested.
        if release_keyid is not None and not streaming:
            quota.release(release_keyid)

    try:
        payload = resp.json() if resp.status_code < 400 else {}
    except Exception as e:
        # A 200 carrying a non-JSON body used to raise here and surface as a 500
        # from the one layer whose entire job is rejecting malformed traffic
        # before it becomes an error.
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_bad_response", principal, model, started) from e
    usage = payload.get("usage") or {}
    _record_usage(model, usage, keyid=keyid)
    audit_log.write(
        AuditRecord(
            timestamp=audit_log.now(),
            event="chat.completions",
            keyid=principal.keyid,
            subject=principal.subject,
            auth_method=principal.method,
            model=model,
            status=resp.status_code,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            upstream=upstream,
            reason=None if resp.status_code < 400 else "upstream_error",
        )
    )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail="upstream error")
    return payload


def _record_usage(model: str | None, usage: dict, *, keyid: str | None = None) -> None:
    """Count reported tokens, and count the requests where there were none.

    The second half matters more than it looks. Until stream_options.include_usage
    is both requested and parsed out of the SSE frames -- see _stream below --
    a streamed request carries no usage at all; in Phase 1, 70 of 78 requests
    were streamed, so this counter would otherwise read about a tenth of
    reality with nothing to say so.

    Also the only place PRD §5.3's token budget accumulates: a request whose
    usage cannot be observed cannot be charged against a budget either, which
    is the same gap by a different name, not a new one.
    """
    label = metrics.safe_model(model)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        metrics.requests_missing_usage_total.labels(model=label).inc()
        return
    if prompt:
        metrics.tokens_total.labels(model=label, kind="prompt").inc(prompt)
    if completion:
        metrics.tokens_total.labels(model=label, kind="completion").inc(completion)
    if keyid is not None:
        quota.record_usage(keyid, (prompt or 0) + (completion or 0))


# An upstream error body is not protocol-bounded, and this one is only read so
# the connection can be reused and the failure logged. Reading it is not
# "buffering the stream": PRD §6.3 C2 protects the PAYLOAD, and an error is not
# the payload.
_MAX_ERROR_BODY_BYTES = 64 * 1024


async def _drain_error(resp) -> None:
    """Consume a bounded prefix of an error response, then close it."""
    try:
        seen = 0
        async for chunk in resp.aiter_raw():
            seen += len(chunk)
            if seen >= _MAX_ERROR_BODY_BYTES:
                break
    except Exception as e:  # noqa: BLE001 - already on the failure path
        logger.debug("draining upstream error body failed: %s", type(e).__name__)
    finally:
        await resp.aclose()


def _parse_usage_frame(raw_event: bytes) -> dict | None:
    """Pull `usage` out of one complete SSE event, if this one carries it.

    Only the trailing frame stream_options.include_usage adds has a `usage`
    key at all (PRD §2.1(e)) -- every ordinary delta frame does not, and every
    malformed or non-JSON line returns None rather than raising. This must
    never be the thing that breaks a passthrough it is only observing.
    """
    for line in raw_event.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        text = line[len(b"data:"):].strip()
        if text == b"[DONE]":
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


async def _stream(body: dict, principal: Principal, model: str | None,
                  started: float, *, keyid: str | None = None,
                  release_keyid: str | None = None):
    """Relay SSE frames as they arrive.

    THE STATUS IS RESOLVED BEFORE ANY HEADER IS COMMITTED
    ----------------------------------------------------
    This used to build the StreamingResponse first and read the upstream status
    inside the generator -- which runs after the 200 is already on the wire. An
    upstream 503 therefore reached the caller as **HTTP 200** carrying an error
    body dressed as a stream, and the real status reached only the audit log.
    That also left the circuit breaker with no client-visible status to key on.

    `pool.open_stream()` gives status and headers with the body still unread --
    which is also what makes retrying a streamed request possible at all: a 503
    can be retried before a single byte has reached the caller, and the pool
    does exactly that (bounded, with jitter) before this function ever sees the
    result. Ownership rule: whoever receives a non-error response owns
    `aclose()`, which is why it sits in the generator's finally beside the audit
    write. AllReplicasOpen and UpstreamUnreachable still propagate to
    chat_completions's single try/except, which handles both paths identically
    -- they pass through this function's own except block first, on the way,
    so a concurrency slot acquired for this request is always released before
    either one reaches chat_completions, streaming or not.

    RELEASE OWNERSHIP FOR release_keyid
    ------------------------------------
    chat_completions acquires the slot and then hands this function ownership
    of releasing it, because the request is not done until the STREAM is --
    long after chat_completions has already returned the StreamingResponse.
    Two release paths, exactly one of which fires per call: the except clause
    below covers everything that goes wrong before the response is handed back
    (including AllReplicasOpen/UpstreamUnreachable propagating out of
    open_stream, and the _reject on a >=400 upstream status); relay()'s
    finally covers everything after.

    stream_options.include_usage IS FORCED ON, UNLESS THE CALLER SAID NO
    ----------------------------------------------------------------------
    Without it, a streamed response carries no usage at all, which means PRD
    §5.3's per-key token budget can never be enforced against a streaming
    caller. Forcing it by default is the trade: the client receives one extra
    trailing frame with an empty `choices` list, which the OpenAI streaming
    protocol already defines for exactly this option and which stub/server.py
    reproduces deliberately (see its docstring) so this shape is tested.
    `.setdefault` respects an explicit `False` from the caller -- if a caller
    has a reason to refuse the extra frame, this does not override that
    choice, and that request's tokens simply go uncounted as before.
    """
    stream_options = body.setdefault("stream_options", {})
    if not isinstance(stream_options, dict):
        stream_options = {}
        body["stream_options"] = stream_options
    stream_options.setdefault("include_usage", True)

    try:
        upstream, resp = await pool.open_stream("/chat/completions", json=body)

        if resp.status_code >= 400:
            await _drain_error(resp)
            raise _reject(resp.status_code, "upstream_error",
                          principal, model, started)
    except Exception:
        if release_keyid is not None:
            quota.release(release_keyid)
        raise

    async def relay():
        first_byte = True
        broken = False
        usage: dict = {}
        buf = b""
        try:
            try:
                async for chunk in resp.aiter_raw():
                    if first_byte and chunk:
                        # TTFT is only measurable on this path. Phase 1 measured
                        # 92-132ms on a prefix-cache hit against 1.4-3.8s on a
                        # miss, which is why the buckets resolve both regimes.
                        first_byte = False
                        metrics.time_to_first_token_seconds.labels(
                            model=metrics.safe_model(model)
                        ).observe(time.perf_counter() - started)
                    buf += chunk
                    while b"\n\n" in buf:
                        raw_event, buf = buf.split(b"\n\n", 1)
                        parsed = _parse_usage_frame(raw_event)
                        if parsed is not None:
                            usage = parsed
                    yield chunk
            except Exception:  # noqa: BLE001
                # A truncated SSE stream is indistinguishable from a SHORT ANSWER
                # to a consumer that stops at [DONE] -- which is what
                # openai_http.py does. On a system reporting financial figures
                # that is the worst available failure mode, so say so explicitly
                # and deliberately do not send [DONE].
                broken = True
                metrics.record_rejection("upstream_stream_broken")
                yield b'data: {"error": {"message": "upstream stream failed", '
                yield b'"type": "upstream_stream_broken"}}\n\n'
        finally:
            await resp.aclose()
            if release_keyid is not None:
                quota.release(release_keyid)
            # Counts the tokens if the trailing usage frame arrived, and folds
            # to requests_missing_usage_total via the same path the
            # non-streaming branch uses if it did not -- one accounting rule
            # for both, not two that can drift apart.
            _record_usage(model, usage, keyid=keyid)
            audit_log.write(
                AuditRecord(
                    timestamp=audit_log.now(),
                    event="chat.completions",
                    keyid=principal.keyid,
                    subject=principal.subject,
                    auth_method=principal.method,
                    model=model,
                    status=resp.status_code,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                    completion_tokens=usage.get("completion_tokens", 0) or 0,
                    upstream=upstream,
                    reason="upstream_stream_broken" if broken else None,
                    streamed=True,
                )
            )

    # Headers are gateway-generated only. Copying the upstream's would carry a
    # Content-Length or Content-Encoding onto a StreamingResponse and corrupt it.
    return StreamingResponse(relay(), status_code=200,
                             media_type="text/event-stream")
