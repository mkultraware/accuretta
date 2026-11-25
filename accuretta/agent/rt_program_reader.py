# SPDX-License-Identifier: AGPL-3.0-only
"""Reading a bug bounty program page into scope rules: the instructions to the model,
its JSON answer, and the limits on text and time.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.agent.token_budget import _approx_tokens
from accuretta.redteam.mission_panel import _rt_program_fields
from accuretta.runtime.llama_http import llama_ready
from accuretta.runtime.llama_post import llama_post
from accuretta.runtime.llama_process import _llama_props_ctx


# ---- bug bounty program reader ------------------------------------------------
# The Bug Bounty questionnaire takes the program text as the platform shows it.
# One call to the loaded model copies its testing rules into the form; plain
# code then checks every value against the text, so a misreading can leave a
# field empty or raise a question but can never widen what the run may touch.
# The user reviews the filled form before anything is authorized.
_RT_PROGRAM_TEXT_MAX = 80000


_RT_PROGRAM_CALL_TIMEOUT_S = 300


_RT_PROGRAM_INSTR = (
    "You read bug bounty program pages and copy their testing rules into a form. "
    "Reply with one JSON object and nothing else. Copy values from the program text; "
    "never invent a host, header, number or rule. When the program does not say, use null "
    "or an empty list.\n\n"
    "Fields:\n"
    '"scope_in": every web host or URL the program lists as in scope, written exactly as the '
    "program writes it. Leave out mobile app store links.\n"
    '"scope_out": every host the program names as out of scope, e.g. "*.example.pt".\n'
    '"subdomains_in_scope": true if the program allows subdomains of the listed hosts, false if '
    "it excludes them, null if it does not say.\n"
    '"user_agent_text": the exact text the program requires in the User-Agent header, or null.\n'
    '"user_agent_mode": "append" if that text is added to a normal User-Agent, "replace" if it '
    "is the whole User-Agent, or null.\n"
    '"required_headers": list of {"name": ..., "value": ...} headers the program requires on '
    "every request (not the User-Agent).\n"
    '"rate_limit_rpm": the request limit in requests per minute (convert per second x60), or null.\n'
    '"automated_scanning_allowed": false if automated scanning or scanners are forbidden, true if '
    "explicitly allowed, null if not mentioned.\n"
    '"destructive_allowed": false if deleting or changing other data is forbidden, else null.\n'
    '"account_rule": how test accounts must be created (e.g. which email alias), or null.\n'
    '"mobile_apps_in_scope": true if mobile apps are listed in scope.\n'
    '"rules": up to 30 short plain sentences the tester must follow: what is forbidden, the '
    "limits on proof for each bug type, what the report must contain, how severity is scored, "
    "and one line listing the bug types the program does not pay for.\n"
    '"questions": only what must be decided before testing starts because the program is unclear '
    "or contradicts itself about WHICH HOSTS may be tested, WHICH HEADERS or LIMITS apply, or WHETHER "
    "a kind of testing is allowed (e.g. an API host that may or may not be in scope). Nothing about "
    "rewards, duplicates or how to write reports, and nothing about mobile apps. Up to 4."
)


def _rt_program_json(text: str) -> dict:
    """First JSON object in a model reply (tolerates fences and think blocks)."""
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            out = json.loads(text[start:end + 1])
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}
    return {}


def _rt_read_program(program_text: str) -> dict:
    """Read a pasted bug bounty program into questionnaire fields."""
    text = str(program_text or "").strip()
    if len(text) < 200:
        return {"ok": False, "error": "Paste the whole program page: scope, rules and all."}
    if len(text) > _RT_PROGRAM_TEXT_MAX:
        return {"ok": False, "error": f"The program text is over {_RT_PROGRAM_TEXT_MAX:,} characters. "
                                      "Leave out reward tables and report counts and try again."}
    if not llama_ready():
        return {"ok": False, "error": "Load a model first. Reading the program uses the loaded model."}
    ctx = _llama_props_ctx()
    need = _approx_tokens(_RT_PROGRAM_INSTR) + _approx_tokens(text) + 2600
    if ctx and need > ctx:
        return {"ok": False, "error": f"The program is too long for the loaded model's context "
                                      f"({ctx:,} tokens; it needs about {need:,}). Leave out the "
                                      "reward tables and report counts, or load the model with more context."}
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": _RT_PROGRAM_INSTR},
            {"role": "user", "content": "PROGRAM TEXT:\n" + text},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 2400,
        "chat_template_kwargs": {"enable_thinking": False, "thinking_budget": 0},
        "response_format": {"type": "json_object"},
    }
    try:
        try:
            resp = llama_post("/v1/chat/completions", payload, timeout=_RT_PROGRAM_CALL_TIMEOUT_S)
        except urllib.error.HTTPError:
            # Older servers reject response_format; the parser copes without it.
            payload.pop("response_format", None)
            resp = llama_post("/v1/chat/completions", payload, timeout=_RT_PROGRAM_CALL_TIMEOUT_S)
    except Exception as exc:
        return {"ok": False, "error": f"The model call failed: {type(exc).__name__}: {exc}"}
    choice = (resp.get("choices") or [{}])[0]
    reply = (choice.get("message") or {}).get("content", "") or ""
    raw = _rt_program_json(reply)
    if not raw:
        return {"ok": False, "error": "The model's answer wasn't readable. Try again, or fill in the form by hand."}
    fields = _rt_program_fields(raw, text)
    fields["ok"] = True
    return fields
