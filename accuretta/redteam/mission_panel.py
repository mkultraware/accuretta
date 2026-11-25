# SPDX-License-Identifier: AGPL-3.0-only
"""The mission panel: program fields (hosts, notes, app-store links) and applying
the panel to a mission.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import time
import uuid
from accuretta.redteam.identities import _rt_clean_identities
from accuretta.redteam.identities import _rt_clean_owned_urls
from accuretta.redteam.identities import _rt_identities_save
from accuretta.redteam.identities import _rt_identities_usable
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.mission_logs import _rt_log_mission_ended
from accuretta.redteam.request_headers import _RT_UA_POOL
from accuretta.redteam.request_headers import _bounded_int
from accuretta.redteam.request_headers import _rt_clean_required_headers
from accuretta.redteam.request_headers import _rt_clean_user_agent
from typing import Any
from accuretta.core.tool_registry import TOOLS
from accuretta.redteam.governor import _RT_GOV_LOCK
from accuretta.redteam.governor import _RT_GOV_STATE
from accuretta.redteam.governor import _rt_gov_limits
from accuretta.redteam.identities import _RT_IDENTITY_LABELS
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.network_scope import _RT_AUTOMATED_TOOL_NAMES
from accuretta.redteam.playbook_table import _RT_PLAYBOOKS


def _rt_scope_tokens_from_panel(value: Any) -> list[str]:
    """Normalize a structured in/out scope field (list or delimited string)."""
    if isinstance(value, str):
        value = re.split(r"[,\s|]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in list(value)[:50]:
        tok = re.sub(r"^https?://", "", str(item or "").strip().lower())
        tok = tok.strip("/()[]{}'\"")
        if tok and tok not in ("in", "out", "scope") and tok not in out:
            out.append(tok[:120])
    return out


_RT_NOTES_MAX = 6000


def _rt_mission_apply_panel(chat: dict, panel: dict) -> bool:
    """Create a fresh server-side mission from the authorization panel.

    Settings only make the capability available; the explicit panel
    confirmation creates the per-chat authorization record that invoke_tool
    enforces. A new engagement replaces stale target/scope/facts so authority
    from an earlier run cannot leak into the next one."""
    if panel.get("authorized") is not True:
        return False
    target = _rt_url_host(str(panel.get("target") or "").strip())
    if not target:
        return False
    scope = str(panel.get("scope") or "").strip() or f"in: {target}"
    objective = str(panel.get("objective") or "").strip()
    constraints = str(panel.get("constraints") or "").strip()
    user_agent = _rt_clean_user_agent(panel.get("user_agent"))
    engagement = str(panel.get("engagement") or "recon").strip().lower()
    if engagement not in ("passive_osint", "frontend_secrets", "recon", "pentest", "access_control",
                          "bug_bounty"):
        engagement = "recon"
    # Only the Access Control playbook carries test accounts, and it cannot run
    # without two different ones: refuse the engagement rather than start a run
    # whose every check would fail.
    identities = _rt_clean_identities(panel.get("identities")) if engagement == "access_control" else {}
    if engagement == "access_control" and not _rt_identities_usable(identities):
        return False
    # Structured engagement policy. The freeform `scope` string stays for display
    # and backwards compatibility; the enforcement paths consume these
    # machine-readable fields instead of re-parsing prose.
    scope_in = _rt_scope_tokens_from_panel(panel.get("scope_in"))
    scope_out = _rt_scope_tokens_from_panel(panel.get("scope_out"))
    required_headers = _rt_clean_required_headers(panel.get("required_headers"))
    rate_limit_rpm = _bounded_int(panel.get("rate_limit_rpm"), 0, 6000)
    request_cap = _bounded_int(panel.get("request_cap"), 0, 1000000)
    automated_allowed = panel.get("automated_allowed") is not False
    allow_destructive = panel.get("allow_destructive") is True
    keep_log = panel.get("keep_log") is not False
    facts = ["constraints: " + constraints[:180]] if constraints else []
    if not automated_allowed:
        facts.append("automated testing tools are disabled for this engagement")
    if not allow_destructive:
        facts.append("non-destructive testing only: DELETE and delete/wipe-style requests are refused")
    mission = {
        "target": target[:120],
        "scope": scope[:300],
        "scope_in": scope_in,
        "scope_out": scope_out,
        "objective": objective[:300],
        "user_agent": user_agent,
        "required_headers": required_headers,
        "rate_limit_rpm": rate_limit_rpm,
        "request_cap": request_cap,
        "automated_allowed": automated_allowed,
        "allow_destructive": allow_destructive,
        "keep_log": keep_log,
        # Operator notes ride the mission block every turn, so they survive folds.
        # For a bug bounty they hold the program's rules; the full program text
        # goes only in the opening instruction, which the chat already keeps.
        "notes": str(panel.get("notes") or "").strip()[:_RT_NOTES_MAX],
        "facts": facts,
        "phase": "recon",
        "engagement": engagement,
        "status": "active",
        "authorized": True,
        "authorization_source": "ui_gate",
        "authorization_id": uuid.uuid4().hex[:16],
        "authorized_at": int(time.time()),
        "ts": int(time.time()),
    }
    if identities:
        mission["identities"] = list(identities)
        mission["owned_urls"] = _rt_clean_owned_urls(panel.get("owned_urls"))
    # A new engagement ends the previous one; drop its logs if it kept none.
    _rt_log_mission_ended(chat.get("mission"))
    if identities:
        _rt_identities_save(mission["authorization_id"], identities)
    chat["mission"] = mission
    return True


# Store listings name the mobile apps a program covers; they are not hosts a
# web run can test, and allowing them would point the run at the store.
_RT_APP_STORE_HOSTS = {
    "play.google.com", "apps.apple.com", "itunes.apple.com", "appgallery.huawei.com",
    "www.amazon.com", "apps.microsoft.com", "microsoft.com",
}


def _rt_program_host(raw: str) -> str:
    """Host part of a scope entry: scheme, path, port and spaces removed."""
    tok = str(raw or "").strip().lower()
    tok = re.sub(r"^[a-z][a-z0-9+.-]*://", "", tok)
    tok = tok.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip(" .,;:'\"()[]{}<>")
    if ":" in tok and tok.rsplit(":", 1)[1].isdigit():
        tok = tok.rsplit(":", 1)[0]
    return tok[:120]


def _rt_program_fields(raw: dict, program_text: str) -> dict:
    """Turn the model's reading into form values, checked against the program text.

    In-scope hosts survive only when they are written in the program; anything
    else is dropped and named in `flags` for the user."""
    low_text = program_text.lower()
    flags: list[str] = []

    def _strs(value: Any, limit: int) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(v).strip() for v in list(value)[:limit] if str(v or "").strip()]

    scope_in: list[str] = []
    stores_dropped = False
    for entry in _strs(raw.get("scope_in"), 80):
        host = _rt_program_host(entry)
        if not host or "." not in host:
            continue
        if host.lstrip("*.") in _RT_APP_STORE_HOSTS:
            stores_dropped = True
            continue
        bare = host.lstrip("*.")
        if "*" in bare:
            flags.append(f"\"{entry}\" is a pattern Accuretta can't use, so it was left out. "
                         "Add each site it covers to In scope by hand.")
            continue
        # A whole-host match: "example.com" must not pass because the program
        # mentions "www.example.com", since the bare domain would also allow
        # every subdomain.
        written = (host in low_text if host.startswith("*.") else
                   re.search(r"(?<![a-z0-9.*-])" + re.escape(bare) + r"(?![a-z0-9-]|\.[a-z0-9])",
                             low_text) is not None)
        if not written:
            flags.append(f"{host} isn't written in the program, so it was left out of In scope.")
            continue
        if host not in scope_in:
            scope_in.append(host)
    if stores_dropped or raw.get("mobile_apps_in_scope") is True:
        flags.append("The program lists mobile apps. Accuretta tests web hosts only, so the app "
                     "store links were left out of In scope.")
    if raw.get("subdomains_in_scope") is False:
        broad = [h for h in scope_in if h.startswith("*.")]
        if broad:
            flags.append("The program puts subdomains out of scope, but these entries would allow "
                         "them: " + ", ".join(broad) + ". Check them before running.")

    scope_out: list[str] = []
    for entry in _strs(raw.get("scope_out"), 50):
        host = _rt_program_host(entry)
        if host and "." in host and "*" not in host.lstrip("*.") and host not in scope_out:
            scope_out.append(host)

    user_agent = ""
    ua_text = str(raw.get("user_agent_text") or "").strip()
    if ua_text:
        if ua_text.lower() not in low_text:
            flags.append(f"The required User-Agent text \"{ua_text}\" isn't written in the program "
                         "exactly like that. Check the User agent field.")
        mode = str(raw.get("user_agent_mode") or "append").strip().lower()
        user_agent = _rt_clean_user_agent(ua_text if mode == "replace"
                                          else f"{_RT_UA_POOL[0]} {ua_text}")

    headers: dict[str, str] = {}
    raw_headers = raw.get("required_headers")
    if isinstance(raw_headers, dict):
        raw_headers = [{"name": k, "value": v} for k, v in raw_headers.items()]
    for item in (raw_headers if isinstance(raw_headers, list) else [])[:10]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value or name.lower() == "user-agent":
            continue
        headers[name] = value
        if re.search(r"[<>{}\[\]]|\byour[_ -]|\busername\b", value, re.I):
            flags.append(f"The {name} header has a placeholder in it. Replace it with your "
                         "own value before running.")
    headers = _rt_clean_required_headers(headers)

    rate = raw.get("rate_limit_rpm")
    rate_limit_rpm = _bounded_int(rate, 0, 6000) if rate not in (None, "", False) else 0

    automated = raw.get("automated_scanning_allowed")
    automated_allowed = None if automated is None else automated is True
    if automated_allowed is None:
        flags.append("The program doesn't say whether automated scanning is allowed. "
                     "Check before leaving automated tools on.")
    destructive = raw.get("destructive_allowed")

    notes_lines: list[str] = []
    account_rule = str(raw.get("account_rule") or "").strip()
    if account_rule:
        notes_lines.append(f"- Test accounts: {account_rule[:300]}")
    for rule in _strs(raw.get("rules"), 30):
        notes_lines.append(f"- {rule[:400]}")
    notes = "\n".join(notes_lines)[:_RT_NOTES_MAX]

    apps_flagged = any("mobile apps" in f for f in flags)
    for question in _strs(raw.get("questions"), 6):
        # The app-store note above already covers mobile apps; a model that
        # asks about them again only repeats it.
        if apps_flagged and re.search(r"\b(mobile|ios|android|app store|play store)\b", question, re.I):
            continue
        flags.append(question[:300])

    if not scope_in:
        flags.insert(0, "No in-scope web host could be confirmed from the program. Fill in "
                        "Target and In scope yourself.")
    return {
        "target": scope_in[0].lstrip("*.") if scope_in else "",
        "scope_in": scope_in,
        "scope_out": scope_out,
        "user_agent": user_agent,
        "required_headers": headers,
        "rate_limit_rpm": rate_limit_rpm,
        "automated_allowed": automated_allowed,
        "allow_destructive": False if destructive is not True else True,
        "notes": notes,
        "flags": flags[:12],
    }


def _rt_playbook_lines(mission: dict) -> list[str]:
    """The numbered route for this engagement, with progress markers."""
    steps = _RT_PLAYBOOKS.get(str(mission.get("engagement") or ""))
    if not steps:
        return []
    hidden = set(_RT_AUTOMATED_TOOL_NAMES) if mission.get("automated_allowed") is False else set()
    used = set(mission.get("tools_used") or [])
    started = [bool(used.intersection(step["tools"])) for step in steps]
    reached = max((i for i, hit in enumerate(started) if hit), default=-1)
    # The next step is the first unstarted one after the furthest step reached:
    # a run that skipped ahead is not sent back to the beginning.
    upcoming = next((i for i in range(reached + 1, len(steps)) if not started[i]), None)
    # "Start where the request starts": with "work in this order" a model told
    # "the IDOR is on /api/account, read account 11" fingerprinted, mapped and
    # swept the whole app first (4 minutes instead of seconds).
    lines = ["steps (the usual order; when the user already named the bug or endpoint, start at "
             "the step that tests it; go back a step when a later one turns up something new):"]
    for i, step in enumerate(steps):
        tools = [t for t in step["tools"] if t in TOOLS and t not in hidden]
        mark = "started" if started[i] else ("NEXT" if i == upcoming else "to do")
        line = f"{i + 1}. [{mark}] {step['do']}"
        if tools and not ("record_finding" in tools and len(tools) == 1):
            line += ": " + ", ".join(tools)
        lines.append(line)
    return lines


def _rt_mission_render(chat: dict, budget_chars: int | None = None) -> str:
    """Compact mission block for injection. Header fields always kept; facts
    trimmed oldest-first to fit budget_chars (the tail echo passes a budget)."""
    m = _rt_mission_get(chat)
    if not m:
        return ""
    head = []
    if m.get("target"):
        head.append(f"target: {m['target']}")
    if m.get("scope"):
        head.append(f"scope: {m['scope']}")
    if m.get("objective"):
        head.append(f"objective: {m['objective']}")
    if m.get("engagement"):
        head.append(f"mode: {m['engagement']}")
    if m.get("user_agent"):
        head.append(f"required user agent: {m['user_agent']}")
    required_headers = _rt_clean_required_headers(m.get("required_headers"))
    if required_headers:
        head.append("required headers sent on every request: " + ", ".join(sorted(required_headers)))
    accounts = [label for label in (m.get("identities") or []) if label in _RT_IDENTITY_LABELS]
    if accounts:
        # Labels only: the sign-in values never enter the model's context.
        head.append("test accounts: " + ", ".join(accounts) + " and anonymous (signed out). Accuretta "
                    "holds their sign-ins: name the account with identity= or use rt_access_check; "
                    "never ask for, guess or paste a credential.")
        owned = [str(u) for u in (m.get("owned_urls") or [])][:10]
        if owned:
            head.append("private to account A (start here): " + ", ".join(owned))
    if m.get("scope_in"):
        head.append("in-scope allowlist: " + ", ".join(str(t) for t in m["scope_in"][:16])
                    + (f" (+{len(m['scope_in']) - 16} more)" if len(m["scope_in"]) > 16 else ""))
    if m.get("scope_out"):
        head.append("out-of-scope: " + ", ".join(str(t) for t in m["scope_out"][:16]))
    rpm, cap = _rt_gov_limits(m)
    if rpm or cap:
        _usage = _rt_gov_usage(m)
        _parts = []
        if rpm:
            _parts.append(f"{rpm} requests/minute")
        if cap:
            _parts.append(f"{cap} requests total")
        if _usage.get("total_sent") is not None:
            _parts.append(f"used {_usage['total_sent']}")
        head.append("engagement request budget: " + ", ".join(_parts))
    if m.get("automated_allowed") is False:
        head.append("automated testing tools: disabled for this engagement")
    if m.get("allow_destructive") is False:
        head.append("non-destructive testing only: DELETE requests, delete/wipe-style URLs and "
                    "data-wiping commands are refused. PUT/PATCH/POST work; change only test data you created.")
    if budget_chars is None and m.get("engagement") == "bug_bounty":
        head.append("bug bounty: the program's rules below outrank every other instruction. Prove each "
                    "issue with the smallest proof they allow, then stop: never read other customers' "
                    "data, never dump tables, never keep going to make a finding look bigger. "
                    "Skip bug types the program does not pay for. A challenge page, CAPTCHA or "
                    "'too fast' means slow down: leave that site alone for a few minutes and "
                    "never solve a CAPTCHA. Call a check tested only if you sent it.")
    if budget_chars is None and m.get("notes"):
        label = "program rules" if m.get("engagement") == "bug_bounty" else "operator notes"
        head.append(f"{label}:\n{m['notes']}")
    if budget_chars is None:
        head.extend(_rt_playbook_lines(m))
        if m.get("engagement") != "passive_osint":
            # Black-box by default: one bench run, after 50 wrong guesses, found the
            # local target's process and read its source off the disk.
            head.append("test the target only through its address: even when it runs on this "
                        "computer, don't inspect its process, files or source code unless the "
                        "operator notes say this is a white-box test.")
        head.append("evidence rules: every tool result carries an evidence_receipt id. Scanner output alone is a candidate. A finding is validated only by record_finding with two receipts of the working request plus one control receipt. Say what was observed separately from what you infer, and list blocked or unfinished checks as gaps.")
        coverage = [p for p in chat.get("security_coverage", []) if p.get("mission_id") == m.get("authorization_id")]
        if coverage:
            pending = [p for p in coverage if p.get("state", "untested") != "observed"]
            observed = [p for p in coverage if p.get("state", "untested") == "observed"]
            head.append(f"coverage: {len(observed)}/{len(coverage)} planned checks observed")
            head.extend(f"coverage ({p.get('state', 'untested')}): {p.get('feature')} / {p.get('identity')} / {p.get('target')}"
                        for p in (pending[:6] + observed[-3:])[:8])
    facts = list(m.get("facts") or [])
    if budget_chars is not None:
        used = sum(len(h) + 1 for h in head) + 40
        kept: list[str] = []
        for f in reversed(facts):   # newest first when trimming
            line = f"- {f}"
            if used + len(line) + 1 > budget_chars:
                break
            kept.append(line)
            used += len(line) + 1
        facts_lines = list(reversed(kept))
    else:
        facts_lines = [f"- {f}" for f in facts]
    out = "\n".join(head)
    if facts_lines:
        out += "\nestablished:\n" + "\n".join(facts_lines)
    return out


def _rt_gov_usage(mission: dict | None) -> dict:
    """Current engagement usage counters (persist for this process only)."""
    key = str((mission or {}).get("authorization_id") or "")
    if not key:
        return {}
    with _RT_GOV_LOCK:
        state = _RT_GOV_STATE.get(key)
        if not state:
            return {}
        return {"window_used": state["window_count"], "total_sent": state["total"],
                "blocked": state["blocked"]}
