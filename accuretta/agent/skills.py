# SPDX-License-Identifier: AGPL-3.0-only
"""Skills: reading SKILL.md files and their front matter, the skills catalog,
and the active skill's section of the prompt.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from pathlib import Path


# User-curated skills: plain .md procedures in skills/ (frontmatter
# name/description/budget). Lazily loaded ONLY on demand — zero context cost
# otherwise. SKILL_BODY_CAP_CHARS is the compress cap for a loaded body;
# SKILL_MAX_BODY_CHARS is the scanner's sanity ceiling (larger files are
# ignored entirely rather than loaded chopped).
SKILLS_DIR = USER_DIR / "skills"


SKILL_BODY_CAP_CHARS = 16000


SKILL_MAX_BODY_CHARS = 64000


_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


_SKILLS_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}


def _parse_skill_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, object] = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        value = v.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        out[k.strip().lower()] = value
    return out


def _scan_skills() -> dict[str, dict]:
    """name -> {name, description, budget, path, mtime, body_chars}.
    Cached on the directory's newest mtime (like .accurettaignore rules). A
    missing skills/ dir is never cached — it is rechecked so the first file
    the user drops in is picked up immediately."""
    # email_plugin/ is a skill that ships with its own scripts (see
    # _read_skill_body for how the skill finds them).
    directories = [ROOT / "builtin_skills", ROOT / "email_plugin", SKILLS_DIR]
    paths = [p for directory in directories if directory.is_dir() for p in sorted(directory.glob("*.md"))]
    try:
        mtime = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    except Exception:
        mtime = 0.0
    cached = _SKILLS_CACHE.get(str(SKILLS_DIR))
    if cached and cached[0] == mtime:
        return cached[1]
    found: dict[str, dict] = {}
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            size = text.count("\n") + 1
            if len(text) > SKILL_MAX_BODY_CHARS:
                continue
        except Exception:
            continue
        m = _FRONTMATTER_RE.match(text)
        body = text[m.end():].strip() if m else text.strip()
        if not body:
            continue
        fm = _parse_skill_frontmatter(text)
        name = str(fm.get("name") or p.stem).strip()
        if not name or not _SKILL_NAME_RE.fullmatch(name):
            continue
        try:
            budget = int(str(fm.get("budget") or "0"))
        except (TypeError, ValueError):
            budget = 0
        found[name] = {
            "name": name,
            "description": str(fm.get("description") or "").strip(),
            "budget": max(0, budget),
            "path": str(p),
            "mtime": p.stat().st_mtime,
            "body_chars": len(body),
            "lines": size,
        }
    _SKILLS_CACHE[str(SKILLS_DIR)] = (mtime, found)
    return found


def _skills_catalog() -> list[dict]:
    return [
        {"name": s["name"], "description": s["description"][:220],
         "budget": s["budget"], "body_chars": s["body_chars"], "lines": s["lines"]}
        for s in sorted(_scan_skills().values(), key=lambda x: x["name"].lower())
    ]


def _read_skill_body(entry: dict) -> str | None:
    try:
        text = Path(entry["path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = _FRONTMATTER_RE.match(text)
    body = text[m.end():].strip() if m else text.strip()
    # A skill that ships scripts beside its .md writes {{skill_dir}} where it
    # needs their location, so the text carries no machine-specific path.
    if "{{skill_dir}}" in body:
        body = body.replace("{{skill_dir}}", Path(entry["path"]).resolve().parent.as_posix())
    return body or None


def _active_skill_section(chat: dict) -> str:
    """Formatted ACTIVE SKILL section for a chat record, or "" when none.

    run_chat_turn appends this to the recency tail (like mission/pins) so the
    body the user picked via # reaches the model every turn; the context
    estimator mirrors it. A model-side load_skill carries the body in its own
    tool result instead."""
    try:
        name = str((chat or {}).get("active_skill") or "").strip()
    except Exception:
        name = ""
    if not name:
        return ""
    entry = _scan_skills().get(name)
    if not entry:
        return ""
    body = _read_skill_body(entry)
    if not body:
        return ""
    body = body[:SKILL_BODY_CAP_CHARS]
    return (
        "=== ACTIVE SKILL: " + name + " (user-loaded — follow this procedure "
        "for the current request; its steps beat improvisation; re-derive nothing "
        "it already provides) ===\n" + body
    )
