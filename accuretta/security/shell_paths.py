# SPDX-License-Identifier: AGPL-3.0-only
"""Turn command arguments into real paths, including targets the shell fills in (foreach, Get-ChildItem).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import posixpath
import re
from accuretta.security.shell_vars import _soft_expand


def _soft_resolve(token: str, lits: list[str], variables: dict[str, str],
                  cwd: str | None, posix: bool) -> tuple[str, str] | None:
    """Resolve one argument token to (zone, normalized path). Zone "guest" is a
    path inside the WSL sandbox (not a mounted Windows drive); "host" is a
    Windows path. None when the path cannot be known for certain."""
    token = token.rstrip(",")
    m = re.fullmatch(r'"(\d+)"', token)
    text = lits[int(m.group(1))] if m else token
    if not m and ('"' in token or "'" in token or "`" in token):
        return None
    text = _soft_expand(text.strip(), variables, posix)
    if not text or re.search(r"[$`@{}]", text):
        return None
    if posix:
        mnt = re.fullmatch(r"/mnt/([a-zA-Z])(/.*)?", text)
        if mnt:
            text = f"{mnt.group(1).upper()}:" + (mnt.group(2) or "/").replace("/", "\\")
        elif text.startswith("/"):
            return "guest", posixpath.normpath(text)
        else:
            if not cwd:
                return None
            joined = posixpath.join(cwd, text) if cwd.startswith("/") else os.path.join(cwd, text)
            if cwd.startswith("/"):
                return "guest", posixpath.normpath(joined)
            text = joined
    if not re.match(r"^[a-zA-Z]:[\\/]|^\\\\", text):
        if not cwd or cwd.startswith("/"):
            return None
        text = os.path.join(cwd, text)
    return "host", os.path.normcase(os.path.normpath(text))


def _soft_expand_delete_targets(targets: list[str], code: str, lits: list[str], posix: bool,
                                cwd: str | None, contents: bool) -> list[str]:
    """Deletion targets the shell fills in itself: a foreach over a literal
    list (`foreach ($p in @("a", "b")) { Remove-Item $p }`) and the items of a
    Get-ChildItem pipeline (`gci | Where-Object {...} | % { Remove-Item $_ }`,
    which can only reach the listed folder)."""
    if posix:
        return targets
    out: list[str] = []
    for t in (targets or []):
        m = re.fullmatch(r"\$(\w+)(?:\.fullname|\.name)?", t, re.IGNORECASE)
        if not m:
            out.append(t)
            continue
        var = m.group(1).lower()
        loop = re.search(rf"foreach\s*\(\s*\${re.escape(var)}\s+in\s+(?:@?\(([^()]*)\)|\$(\w+))\s*\)", code,
                         re.IGNORECASE)
        if loop:
            listed = loop.group(1)
            if listed is None:
                # foreach ($p in $remove) with $remove = @('a', 'b')
                arr = re.search(rf"\${re.escape(loop.group(2))}\s*=\s*@?\(([^()]*)\)", code, re.IGNORECASE)
                listed = arr.group(1) if arr else ""
            items = [i.strip() for i in listed.split(",") if i.strip()]
            if items and all(re.fullmatch(r'"\d+"', i) for i in items):
                out += items
                continue
        if var in ("_", "psitem"):
            gci = re.search(r"\b(?:get-childitem|gci|dir|ls)\b([^|;\n]*)\|", code, re.IGNORECASE)
            if gci:
                args = gci.group(1).split()
                low = [a.lower() for a in args]
                path_tok = next((args[k + 1] for k, a in enumerate(low)
                                 if a in ("-path", "-literalpath") and k + 1 < len(args)),
                                next((a for k, a in enumerate(args) if not a.startswith("-")
                                      and (k == 0 or not low[k - 1].startswith("-")
                                           or low[k - 1] in ("-name", "-recurse", "-force", "-file", "-directory"))),
                                     None))
                narrowed = (re.search(r"\bwhere-object\b|\bwhere\b|\?\s*\{", code, re.IGNORECASE)
                            or {"-filter", "-include"} & set(low))
                base = (lits[int(path_tok[1:-1])] if path_tok and re.fullmatch(r'"\d+"', path_tok)
                        else path_tok) or cwd or ""
                if base:
                    lits.append(base.rstrip("\\/") + ("\\*.matched" if narrowed else "\\*"))
                    out.append(f'"{len(lits) - 1}"')
                    continue
        out.append(t)
    return out
