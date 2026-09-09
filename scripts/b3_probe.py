#!/usr/bin/env python3
"""Confirm PRD §6.1 B3 against real Qwen3, including the `/think` soft switch.

THE QUESTION THIS ANSWERS
=========================
B3's fix is `chat_template_kwargs: {"enable_thinking": false}`, and PRD §6.1
flags one thing about it as unverified:

    "whether `enable_thinking: false` overrides a user typing `/think` in their
     query (Qwen3 parses that as a soft switch)"

That matters because the two mechanisms sit at different layers. The request
flag is consumed by the chat template at render time; `/think` and `/no_think`
are parsed from the message text by the template's own logic. Which wins is a
property of the template shipped with the model, not something the API contract
states, and getting it wrong has a specific consequence: a user who types
`/think` re-enables reasoning, `FAQ_MAX_TOKENS = 160` is consumed entirely by
an unterminated `<think>` block, `generate()` returns empty, and the facade maps
empty to `("", True)` -- a degraded, RAG-only answer with no error anywhere.
One user's habit silently downgrades their own answers.

WHY THIS IS A STANDALONE SCRIPT AND NOT THE APP
===============================================
The app is not transferred to a rented box: its eval suite retrieves from the
real knowledge base and injects the chunks into the prompt, so real content
would travel in the request body regardless (PRD §4.8). What is actually needed
to answer the question is the WIRE PAYLOAD, and that is reproduced here exactly
as `app/core/llm_backends/openai_http.py:186-188` builds it -- `body.update()`
of a top-level `chat_template_kwargs`, NOT nested under `extra_body`. The app's
`strip_think` is already unit-tested against `stub/server.py --stub-think`; what
cannot be tested without a GPU is the server's behaviour, which is all this
probe is for.

RUN IT TWICE
============
Once through the gateway and once with `--direct` at vLLM. If the two disagree,
the gateway is dropping `chat_template_kwargs` on the way through -- which would
break B3's fix in production while every local test stayed green.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

# The same two patterns as `openai_http.strip_think`, in the same order.
# Duplicated rather than imported because the app repo is not on the box; if
# they ever diverge, the app's tests are the authority.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_UNTERMINATED = re.compile(r"<think>.*\Z", re.DOTALL)


def strip_think(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_UNTERMINATED.sub("", text)
    return text.strip()


# ── The matrix ───────────────────────────────────────────────────────────────
# `flag` is what goes into chat_template_kwargs.enable_thinking:
#   None  -> the key is not sent at all (the pre-B3 baseline)
#   False -> B3's fix
#   True  -> thinking explicitly requested
CASES = [
    ("baseline_no_flag", None, "Summarise the treasury position in one sentence.", 512,
     "Pre-B3 baseline. Qwen3 is a hybrid reasoning model and its default chat "
     "template emits <think>. If no block appears here, this build's default "
     "differs from what B3 assumed and the rest of the matrix must be re-read."),
    ("flag_false", False, "Summarise the treasury position in one sentence.", 512,
     "B3's fix on its own. A <think> block here means the flag was ignored."),
    ("flag_false_user_types_think", False,
     "/think Summarise the treasury position in one sentence.", 512,
     "*** THE OPEN QUESTION (PRD §6.1 B3). *** Does the user's soft switch "
     "override the request-level flag?"),
    ("flag_false_user_types_no_think", False,
     "/no_think Summarise the treasury position in one sentence.", 512,
     "Flag and soft switch agree. Confirms the template parses the switch at all, "
     "which is what makes the previous case's result interpretable."),
    ("flag_true_user_types_no_think", True,
     "/no_think Summarise the treasury position in one sentence.", 512,
     "The mirror image. Together with the case above it establishes which layer "
     "wins, rather than just what happens once."),
    ("flag_absent_tight_budget", None,
     "Summarise the treasury position in one sentence.", 160,
     "Reproduces B3's stated failure exactly: FAQ_MAX_TOKENS = 160 consumed by an "
     "unterminated <think> block, leaving an empty answer after stripping."),
    ("flag_false_tight_budget", False,
     "Summarise the treasury position in one sentence.", 160,
     "The same tight budget with the fix on. This is the case that shows B3's fix "
     "is what makes a 160-token allowance usable."),
]


def probe(client: httpx.AsyncClient, url: str, model: str, case) -> dict:
    name, flag, prompt, max_tokens, why = case
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
        # Qwen3 non-thinking sampling defaults (PRD §6.3 C1).
        "temperature": 0.7, "top_p": 0.8,
    }
    if flag is not None:
        # Exactly as openai_http.py does it: merged at the TOP LEVEL of the
        # body. A strict proxy that only forwards known OpenAI fields would
        # drop this, which is why the probe is also run through the gateway.
        body.update({"chat_template_kwargs": {"enable_thinking": flag}})

    t0 = time.perf_counter()
    resp = client.post(url, json=body)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    row = {
        "case": name,
        "enable_thinking": flag,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "rationale": why,
        "status": resp.status_code,
        "latency_ms": round(latency_ms, 1),
    }
    if resp.status_code >= 400:
        row["error"] = resp.text[:400]
        return row

    payload = resp.json()
    raw = (payload["choices"][0]["message"].get("content") or "")
    stripped = strip_think(raw)
    usage = payload.get("usage") or {}

    row.update({
        "has_think_open": "<think>" in raw,
        "has_think_close": "</think>" in raw,
        # An opened-but-unclosed block is the shape that empties an answer: the
        # allowance ran out mid-reasoning.
        "think_unterminated": "<think>" in raw and "</think>" not in raw,
        "raw_chars": len(raw),
        "stripped_chars": len(stripped),
        # The B3 failure, named: content came back, and nothing survived
        # stripping. `generate()` maps this to ("", True) -> degraded.
        "empty_after_strip": bool(raw) and not stripped,
        "finish_reason": payload["choices"][0].get("finish_reason"),
        "completion_tokens": usage.get("completion_tokens"),
        "raw_head": raw[:240],
        "stripped_head": stripped[:240],
    })
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--direct", action="store_true",
                    help="Recorded in the output: this run bypassed the gateway.")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="Qwen/Qwen3-8B-AWQ")
    ap.add_argument("-o", "--output", default="logs/b3_probe.jsonl")
    args = ap.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    url = f"{args.url.rstrip('/')}/chat/completions"

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    with httpx.Client(headers=headers, timeout=httpx.Timeout(
            connect=10.0, read=300.0, write=30.0, pool=30.0)) as client:
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "kind": "header", "tool": "b3_probe.py", "target": args.url,
                "direct": args.direct, "model": args.model,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }) + "\n")
            for case in CASES:
                row = probe(client, url, args.model, case)
                rows.append(row)
                fh.write(json.dumps({"kind": "case", **row}) + "\n")
                fh.flush()
                print(f"  ran {row['case']:<32} status={row['status']}")

    # ── Report ──────────────────────────────────────────────────────────────
    print()
    print(f"{'case':<34} {'flag':>6}  {'<think>':>8}  {'unterm':>7}  "
          f"{'empty':>6}  {'finish':>10}")
    print("-" * 84)
    for r in rows:
        if r.get("error"):
            print(f"{r['case']:<34} {'ERROR':>6}  {r['error'][:40]}")
            continue
        print(f"{r['case']:<34} {str(r['enable_thinking']):>6}  "
              f"{str(r['has_think_open']):>8}  {str(r['think_unterminated']):>7}  "
              f"{str(r['empty_after_strip']):>6}  {str(r['finish_reason']):>10}")

    by_name = {r["case"]: r for r in rows}
    print()
    print("=== Verdicts ===")

    baseline = by_name.get("baseline_no_flag", {})
    if baseline.get("error") is None and not baseline.get("has_think_open"):
        print("! baseline_no_flag produced NO <think> block. This build does not "
              "default to thinking mode, so B3's premise does not hold as stated "
              "here and every verdict below must be read in that light.")

    fix = by_name.get("flag_false", {})
    if fix.get("error") is None:
        ok = not fix.get("has_think_open")
        print(f"B3 fix alone (enable_thinking: false): "
              f"{'WORKS -- no reasoning block' if ok else 'IGNORED -- block still present'}")

    override = by_name.get("flag_false_user_types_think", {})
    if override.get("error") is None:
        if override.get("has_think_open"):
            print("*** `/think` OVERRIDES the request flag. *** The soft switch wins. "
                  "The `strip_think` stripper is therefore LOAD-BEARING, not a belt: "
                  "any user typing /think re-enables reasoning, and with a tight "
                  "max_tokens their answers silently degrade to RAG-only. Consider "
                  "stripping the switch from user input at the app boundary.")
        else:
            print("`/think` does NOT override the request flag. enable_thinking: false "
                  "holds even when the user asks for reasoning, so the stripper is a "
                  "belt rather than the primary defence.")

    tight_off = by_name.get("flag_absent_tight_budget", {})
    tight_on = by_name.get("flag_false_tight_budget", {})
    if tight_off.get("error") is None and tight_on.get("error") is None:
        print(f"B3's stated failure at max_tokens=160: "
              f"empty_after_strip={tight_off.get('empty_after_strip')} without the "
              f"fix, {tight_on.get('empty_after_strip')} with it")

    failures = [r for r in rows if r.get("error")]
    print()
    print(f"wrote {out}")
    if failures:
        print(f"WARNING: {len(failures)} case(s) errored and have no verdict: "
              f"{[r['case'] for r in failures]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
