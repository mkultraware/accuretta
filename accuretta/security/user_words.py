# SPDX-License-Identifier: AGPL-3.0-only
"""The user's own words as approval: what they asked for, per chat and per turn.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request


# -- the user's own words -------------------------------------------------------------
# Only what the user typed in this chat counts: never page text, file content or
# tool output. Narrow on purpose (an action word plus what it applies to) and a
# "don't" / "not yet" in front cancels it.
_USER_WORDS: dict[str, str] = {}


_TURN_OK: dict[str, set[str]] = {}       # approvals the user gave this turn (per chat)


_CHAT_OK: dict[str, set[str]] = {}       # "allow for the rest of this chat" (per chat)


_CHAT_ALLOWABLE = {"kill", "launch", "push", "web"}


_NEGATION = r"(?:don'?t|do not|never|no|not|without|stop)\W+(?:\w+\W+){0,2}"


def _note_user_words(chat_id: str, text: str) -> None:
    """Remember what the user typed for this chat; a new message ends the turn's approvals."""
    if not chat_id:
        return
    words = str(text or "")
    # Pasted material (fenced code, quoted lines) is not the user's own instruction.
    words = re.sub(r"```.*?(?:```|$)", " ", words, flags=re.DOTALL)
    words = "\n".join(line for line in words.splitlines() if not line.lstrip().startswith(">"))
    _USER_WORDS[chat_id] = words[:4000]
    _TURN_OK.pop(chat_id, None)


def _said(words: str, verbs: str, obj: str = "") -> bool:
    """The user asked for it: an action word (and, when given, its object within a
    few words), not negated."""
    if not words:
        return False
    pattern = rf"\b(?:{verbs})\b" + (rf"\W+(?:[\w.-]+\W+){{0,4}}?(?:{obj})" if obj else "")
    for m in re.finditer(pattern, words, re.IGNORECASE):
        before = words[max(0, m.start() - 40):m.start()]
        if re.search(_NEGATION + r"$", before, re.IGNORECASE):
            continue
        # A question about it ("should I push?", "what does `git push -f` do?")
        # is not a request; "can you push it?" is.
        start = max(words.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        ends = [i for i in (words.find(c, m.end()) for c in ".!?\n") if i >= 0]
        end = min(ends) if ends else len(words)
        sentence = words[start:end + 1].strip()
        if sentence.endswith("?") and re.match(
                r"(?:what|why|how|does|do|did|is|are|was|were|should|shall|when|where|which|who|will|isn'?t|"
                r"aren'?t|doesn'?t|don'?t)\b", sentence, re.IGNORECASE):
            continue
        return True
    return False


_CONCERN_WRAPPER_RE = re.compile(r"^(?:cmd /c|wsl|background [\w.-]+|powershell -Command|bash -c): ")


def _concern_category(concern: str) -> tuple[str, str]:
    """(category, subject) for an ask reason."""
    c = concern or ""
    while _CONCERN_WRAPPER_RE.match(c):
        c = _CONCERN_WRAPPER_RE.sub("", c, count=1)
    subject = c.rsplit(": ", 1)[1].strip() if ": " in c else ""
    if c.startswith("rewrites or deletes Git history"):
        return "history", subject
    if c.startswith("pushes to a remote"):
        return "push", subject
    if c.startswith("stops "):
        m = re.search(r"\(([^)]+)\)", c)
        return "kill", (m.group(1) if m else subject)
    if c.startswith(("starts a program", "launches a program")):
        return "launch", subject
    if c.startswith("opens a web page"):
        return "web", subject
    if (c.startswith(("deletes ", "throws away"))
            or re.match(r"runs (?:remove-item|ri|rm|del|erase|rmdir|rd) on files", c)
            or re.search(r"\bdeletes files\b", c)):
        return "delete", subject
    if c.startswith("installs a package"):
        return "install", subject
    return "other", subject


def _concern_authorized(concern: str, chat_id: str, command: str = "") -> bool:
    """True when the user's own words (or an approval they gave this turn or
    chat) already cover this reason."""
    if concern in _TURN_OK.get(chat_id, ()):
        return True
    cat, subject = _concern_category(concern)
    if cat in _CHAT_OK.get(chat_id, ()) or (cat in ("kill", "push", "launch", "web")
                                            and f"turn:{cat}" in _TURN_OK.get(chat_id, ())):
        return True
    words = _USER_WORDS.get(chat_id, "")
    if not words:
        return False
    base = os.path.basename(subject.replace("/", "\\").rstrip("\\*")).lower() if subject else ""
    stem = os.path.splitext(base)[0]
    if cat == "push":
        return _said(words, r"push(?:es|ed|ing)?|publish|ship")
    if cat == "history":
        low = command.lower()
        if "push" in low:
            return _said(words, r"force[- ]?push(?:ed|ing)?") or bool(re.search(r"push\s+(?:-f|--force)\b", words, re.I))
        if "reset" in low:
            return bool(re.search(r"(?:reset\s+--hard|hard\s+reset)", words, re.I))
        return False
    if cat == "kill":
        names = r"servers?|apps?|process(?:es)?|programs?|instances?|it|them|that|those|everything"
        if stem and len(stem) >= 3:
            names += "|" + re.escape(stem)
        return _said(words, r"kill|stop|close|quit|terminate|end|restart|shut\s*down", names)
    if cat in ("launch", "web"):
        target = r"it|the app|the program|the site|the page|the link|the url"
        if stem and len(stem) >= 3:
            target += "|" + re.escape(stem)
        if cat == "web" and subject:
            host = urllib.parse.urlsplit(subject).hostname or ""
            if host:
                target += "|" + re.escape(host.removeprefix("www."))
        return _said(words, r"open|launch|start|run|show|visit|browse|go to", target)
    if cat == "delete":
        if not stem or len(stem) < 3:
            return False
        return _said(words, r"delete|remove|rm|wipe|erase|clean(?:\s*up)?|clear|get rid of|trash|discard|throw away",
                     re.escape(stem) + r"|" + re.escape(base))
    if cat == "install":
        tail = re.findall(r"[\w.-]{4,}", subject or command)
        return _said(words, r"install") and any(t.lower() in words.lower() for t in tail[-3:])
    return False


def _remember_turn_approval(chat_id: str, reasons: list[str], always: bool) -> None:
    if not chat_id:
        return
    ok = _TURN_OK.setdefault(chat_id, set())
    for r in reasons or []:
        ok.add(r)
        cat, _ = _concern_category(r)
        ok.add(f"turn:{cat}")
        if always and cat in _CHAT_ALLOWABLE:
            _CHAT_OK.setdefault(chat_id, set()).add(cat)
