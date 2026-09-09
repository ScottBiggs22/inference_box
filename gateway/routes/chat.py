"""/v1/chat/completions — the one route that reaches a GPU.

Everything here runs BEFORE the request is forwarded, and that ordering is the
design: each check is cheap and stands in front of a resource that is not. A
request rejected here costs microseconds; the same request accepted occupies a
decode slot on a card shared by at most five concurrent users.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from gateway.audit import AuditRecord, audit_log
from gateway.auth import Principal, require_principal
from gateway.config import settings
from gateway.upstream.client import pool

router = APIRouter(prefix="/v1", tags=["inference"])


def _reject(status_code: int, reason: str, principal: Principal,
            model: str | None, started: float) -> HTTPException:
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
    requested = body.get("max_tokens")
    if requested is None or int(requested) > settings.MAX_COMPLETION_TOKENS:
        body["max_tokens"] = settings.MAX_COMPLETION_TOKENS

    streaming = bool(body.get("stream"))
    upstream = pool.pick()
    url = f"{upstream.rstrip('/')}/chat/completions"

    if streaming:
        return await _stream(url, body, principal, model, started, upstream)

    try:
        resp = await pool.client.post(url, json=body)
    except Exception as e:
        raise _reject(status.HTTP_502_BAD_GATEWAY,
                      "upstream_unreachable", principal, model, started) from e

    payload = resp.json() if resp.status_code < 400 else {}
    usage = payload.get("usage") or {}
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


async def _stream(url: str, body: dict, principal: Principal,
                  model: str | None, started: float, upstream: str):
    """Relay SSE frames as they arrive.

    Token counts are not available here: usage is not carried on every frame,
    and assembling the completion to count it would defeat the purpose of
    streaming. The audit record therefore carries zeros with streamed=true --
    which is honest, and better than an invented number. Recovering real counts
    means asking the upstream for `stream_options.include_usage`, which is
    Phase 2 work alongside the per-key token budget that would need it.
    """
    async def relay():
        status_code = 200
        try:
            async with pool.client.stream("POST", url, json=body) as resp:
                status_code = resp.status_code
                async for chunk in resp.aiter_raw():
                    yield chunk
        finally:
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
                    upstream=upstream,
                    streamed=True,
                )
            )

    return StreamingResponse(relay(), media_type="text/event-stream")
