"""/v1/chat/completions — the one route that reaches a GPU.

Everything here runs BEFORE the request is forwarded, and that ordering is the
design: each check is cheap and stands in front of a resource that is not. A
request rejected here costs microseconds; the same request accepted occupies a
decode slot on a card shared by at most five concurrent users.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from gateway import metrics
from gateway.audit import AuditRecord, audit_log
from gateway.auth import Principal, require_principal
from gateway.config import settings
from gateway.upstream.client import pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["inference"])


def _upstream_label(url: str) -> str:
    """Replicas are labelled by index, never by URL.

    /metrics is unauthenticated, and the upstream URLs are private-subnet vLLM
    addresses. Publishing them to an anonymous scraper is the same class of leak
    that keeps `keyid` out of the labels -- topology instead of tenancy. The
    index -> URL map is logged once at startup for whoever needs to resolve it.
    """
    try:
        return str(pool.urls.index(url))
    except ValueError:
        return "unknown"


def _reject(status_code: int, reason: str, principal: Principal,
            model: str | None, started: float) -> HTTPException:
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
    return HTTPException(status_code=status_code, detail=reason)


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
    upstream = pool.pick()
    url = f"{upstream.rstrip('/')}/chat/completions"

    if streaming:
        return await _stream(url, body, principal, model, started, upstream)

    try:
        resp = await pool.client.post(url, json=body)
    except Exception as e:
        metrics.upstream_requests_total.labels(
            upstream=_upstream_label(upstream), outcome="unreachable").inc()
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_unreachable", principal, model, started) from e

    metrics.upstream_requests_total.labels(
        upstream=_upstream_label(upstream),
        outcome="ok" if resp.status_code < 400 else "http_error",
    ).inc()

    try:
        payload = resp.json() if resp.status_code < 400 else {}
    except Exception as e:
        # A 200 carrying a non-JSON body used to raise here and surface as a 500
        # from the one layer whose entire job is rejecting malformed traffic
        # before it becomes an error.
        metrics.upstream_requests_total.labels(
            upstream=_upstream_label(upstream), outcome="bad_response").inc()
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_bad_response", principal, model, started) from e
    usage = payload.get("usage") or {}
    _record_usage(model, usage)
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


def _record_usage(model: str | None, usage: dict) -> None:
    """Count reported tokens, and count the requests where there were none.

    The second half matters more than it looks. A streamed request carries no
    usage unless the SSE frames are parsed for it, which this gateway does not do
    yet -- 70 of Phase 1's 78 requests were streamed, so this counter would
    otherwise read about a tenth of reality with nothing to say so.
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


async def _stream(url: str, body: dict, principal: Principal,
                  model: str | None, started: float, upstream: str):
    """Relay SSE frames as they arrive.

    THE STATUS IS RESOLVED BEFORE ANY HEADER IS COMMITTED
    ----------------------------------------------------
    This used to build the StreamingResponse first and read the upstream status
    inside the generator -- which runs after the 200 is already on the wire. An
    upstream 503 therefore reached the caller as **HTTP 200** carrying an error
    body dressed as a stream, and the real status reached only the audit log.
    That also left the circuit breaker with no client-visible status to key on.

    `client.stream()` is just `send(request, stream=True)` with an `aclose()` in a
    finally, so calling `send()` directly gives the status and headers with the
    body still unread, and the context-manager problem disappears. Ownership rule:
    whoever receives a non-error response owns `aclose()`, which is why it sits in
    the generator's finally beside the audit write.

    Token counts are still not available here: usage is not carried on every
    frame, and assembling the completion to count it would defeat the purpose of
    streaming. The audit record carries zeros with streamed=true, which is honest
    and better than an invented number. Parsing `stream_options.include_usage`
    out of the passthrough is the per-key-budget slice's work;
    bkn301_gateway_requests_missing_usage_total counts the gap meanwhile.
    """
    client = pool.client
    try:
        request = client.build_request("POST", url, json=body)
        resp = await client.send(request, stream=True)
    except Exception as e:
        metrics.upstream_requests_total.labels(
            upstream=_upstream_label(upstream), outcome="unreachable").inc()
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_unreachable", principal, model, started) from e

    metrics.upstream_requests_total.labels(
        upstream=_upstream_label(upstream),
        outcome="ok" if resp.status_code < 400 else "http_error",
    ).inc()

    if resp.status_code >= 400:
        await _drain_error(resp)
        raise _reject(resp.status_code, "upstream_error",
                      principal, model, started)

    async def relay():
        first_byte = True
        broken = False
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
            # No usage frame is parsed out of the passthrough yet, so every
            # streamed request is an unobserved one. Counted, not guessed at.
            metrics.requests_missing_usage_total.labels(
                model=metrics.safe_model(model)).inc()
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
                    prompt_tokens=0,
                    completion_tokens=0,
                    upstream=upstream,
                    reason="upstream_stream_broken" if broken else None,
                    streamed=True,
                )
            )

    # Headers are gateway-generated only. Copying the upstream's would carry a
    # Content-Length or Content-Encoding onto a StreamingResponse and corrupt it.
    return StreamingResponse(relay(), status_code=200,
                             media_type="text/event-stream")
