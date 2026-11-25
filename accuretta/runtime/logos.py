# SPDX-License-Identifier: AGPL-3.0-only
"""Matching a model maker's name to a vendored logo when no rule names one.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.core.paths import ROOT


_LOGO_DIR = ROOT / "assets" / "vendor" / "lobe-icons"


_LOGO_INDEX: dict | None = None


def _match_logo(name: str) -> str:
    """A vendored logo for a maker we have no rule for ("LiquidAI", "Nous
    Research"): match its name, with a trailing AI / Labs / Research and the
    like dropped, against the logo files. "" when nothing fits."""
    global _LOGO_INDEX
    if _LOGO_INDEX is None:
        try:
            _LOGO_INDEX = {re.sub(r"[^a-z0-9]", "", p.stem.lower()): p.stem for p in _LOGO_DIR.glob("*.svg")}
        except OSError:
            _LOGO_INDEX = {}
    slug = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    for candidate in (slug, re.sub(r"(?:ai|labs?|research|inc|org|team|hf|community)$", "", slug)):
        if candidate and candidate in _LOGO_INDEX:
            return _LOGO_INDEX[candidate]
    return ""
