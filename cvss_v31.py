"""CVSS v3.1 base scoring and CWE normalization for finding records.

Server-side on purpose: a local model can judge qualitative impact, but it
should not be trusted to emit a correct vector or score. The harness accepts
an optional vector, validates every base metric, computes the official base
score, and stores the derived severity band; the model never performs the
arithmetic. Wrong vectors are rejected with a directive error instead of
being passed through into a client or platform report.

Reference: FIRST CVSS v3.1 specification (base metric group only). Temporal
and environmental metrics are accepted on input and ignored for scoring.
"""

from __future__ import annotations

import math
import re

_BASE_ORDER = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")
_ALLOWED = {
    "AV": {"N", "A", "L", "P"},
    "AC": {"L", "H"},
    "PR": {"N", "L", "H"},
    "UI": {"N", "R"},
    "S": {"U", "C"},
    "C": {"H", "L", "N"},
    "I": {"H", "L", "N"},
    "A": {"H", "L", "N"},
}
# Temporal and environmental metric names. Accepted on input, ignored for the
# base score; the normalized output vector always carries base metrics only.
_IGNORED_METRICS = {
    "E", "RL", "RC", "CR", "IR", "AR",
    "MAV", "MAC", "MPR", "MUI", "MS", "MC", "MI", "MA",
}

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}
_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.50}


def _roundup(value: float) -> float:
    """CVSS v3.1 Roundup(): round up to one decimal, exact per specification."""
    if value <= 0:
        return 0.0
    scaled = int(round(value * 100000))
    if scaled % 10000 == 0:
        return scaled / 100000
    return (math.floor(scaled / 10000) + 1) / 10.0


def severity_for_score(score: float) -> str:
    if score <= 0:
        return "none"
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


def parse_vector(text: str) -> dict[str, str] | None:
    """Parse a CVSS v3.x vector into its metric pairs, or None when invalid.

    Accepts an optional ``CVSS:3.0/`` or ``CVSS:3.1/`` prefix and ignores
    temporal/environmental metrics. Requires all eight base metrics with
    valid values.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [p for p in raw.split("/") if p]
    if parts and parts[0].upper().startswith("CVSS:"):
        version = parts[0].split(":", 1)[1].strip()
        if not version.startswith("3."):
            return None
        parts = parts[1:]
    metrics: dict[str, str] = {}
    for part in parts:
        if ":" not in part:
            return None
        name, _, value = part.partition(":")
        name = name.strip().upper()
        value = value.strip().upper()
        if name in _ALLOWED:
            if value not in _ALLOWED[name]:
                return None
            metrics[name] = value
        elif name in _IGNORED_METRICS:
            continue
        else:
            return None
    if any(name not in metrics for name in _BASE_ORDER):
        return None
    return metrics


def base_score(vector: str) -> dict | None:
    """Validate and score a CVSS v3.1 base vector.

    Returns {"vector", "score", "severity"} on success, None on any invalid
    or incomplete vector. The returned vector is the normalized base-only
    form (AV/AC/PR/UI/S/C/I/A in specification order).
    """
    metrics = parse_vector(vector)
    if not metrics:
        return None
    scope_changed = metrics["S"] == "C"
    pr_table = _PR_CHANGED if scope_changed else _PR_UNCHANGED
    exploitability = (
        8.22
        * _AV[metrics["AV"]]
        * _AC[metrics["AC"]]
        * pr_table[metrics["PR"]]
        * _UI[metrics["UI"]]
    )
    iss = 1.0 - (
        (1.0 - _CIA[metrics["C"]])
        * (1.0 - _CIA[metrics["I"]])
        * (1.0 - _CIA[metrics["A"]])
    )
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    if impact <= 0:
        score = 0.0
    elif scope_changed:
        score = _roundup(min(1.08 * (impact + exploitability), 10.0))
    else:
        score = _roundup(min(impact + exploitability, 10.0))
    normalized = "CVSS:3.1/" + "/".join(f"{name}:{metrics[name]}" for name in _BASE_ORDER)
    return {"vector": normalized, "score": score, "severity": severity_for_score(score)}


# Common web/application CWEs. Any well-formed CWE id is accepted; this list
# only supplies a canonical name so reports never trust a model-invented one.
CWE_NAMES = {
    "CWE-16": "Configuration",
    "CWE-20": "Improper Input Validation",
    "CWE-22": "Path Traversal",
    "CWE-77": "Command Injection",
    "CWE-78": "OS Command Injection",
    "CWE-79": "Cross-site Scripting (XSS)",
    "CWE-80": "Improper Neutralization of Script-Related HTML Tags",
    "CWE-89": "SQL Injection",
    "CWE-90": "LDAP Injection",
    "CWE-91": "XML Injection",
    "CWE-94": "Code Injection",
    "CWE-116": "Improper Encoding or Escaping of Output",
    "CWE-200": "Exposure of Sensitive Information",
    "CWE-209": "Generation of Error Message Containing Sensitive Information",
    "CWE-269": "Improper Privilege Management",
    "CWE-276": "Incorrect Default Permissions",
    "CWE-284": "Improper Access Control",
    "CWE-285": "Improper Authorization",
    "CWE-287": "Improper Authentication",
    "CWE-288": "Authentication Bypass Using an Alternate Path or Channel",
    "CWE-290": "Authentication Bypass by Spoofing",
    "CWE-306": "Missing Authentication for Critical Function",
    "CWE-307": "Improper Restriction of Excessive Authentication Attempts",
    "CWE-311": "Missing Encryption of Sensitive Data",
    "CWE-326": "Inadequate Encryption Strength",
    "CWE-346": "Origin Validation Error",
    "CWE-347": "Improper Verification of Cryptographic Signature",
    "CWE-352": "Cross-Site Request Forgery (CSRF)",
    "CWE-384": "Session Fixation",
    "CWE-400": "Uncontrolled Resource Consumption",
    "CWE-434": "Unrestricted Upload of File with Dangerous Type",
    "CWE-441": "Unintended Proxy or Intermediary (Confused Deputy)",
    "CWE-444": "Inconsistent Interpretation of HTTP Requests",
    "CWE-502": "Deserialization of Untrusted Data",
    "CWE-521": "Weak Password Requirements",
    "CWE-522": "Insufficiently Protected Credentials",
    "CWE-538": "Insertion of Sensitive Information into Externally-Accessible File or Directory",
    "CWE-548": "Exposure of Information Through Directory Listing",
    "CWE-601": "Open Redirect",
    "CWE-611": "Improper Restriction of XML External Entity Reference (XXE)",
    "CWE-613": "Insufficient Session Expiration",
    "CWE-614": "Sensitive Cookie in HTTPS Session Without 'Secure' Attribute",
    "CWE-620": "Unverified Password Change",
    "CWE-639": "Authorization Bypass Through User-Controlled Key (IDOR)",
    "CWE-640": "Weak Password Recovery Mechanism for Forgotten Password",
    "CWE-643": "Improper Neutralization of Data within XPath Expressions",
    "CWE-732": "Incorrect Permission Assignment for Critical Resource",
    "CWE-778": "Insufficient Logging",
    "CWE-798": "Use of Hard-coded Credentials",
    "CWE-862": "Missing Authorization",
    "CWE-863": "Incorrect Authorization",
    "CWE-918": "Server-Side Request Forgery (SSRF)",
    "CWE-942": "Permissive Cross-domain Policy with Untrusted Domains",
    "CWE-1004": "Sensitive Cookie Without 'HttpOnly' Flag",
    "CWE-1021": "Improper Restriction of Rendered UI Layers (Clickjacking)",
    "CWE-1333": "Inefficient Regular Expression Complexity (ReDoS)",
}

_CWE_RE = re.compile(r"^(?:CWE[-_ ]?)?(\d{1,5})$", re.IGNORECASE)


def normalize_cwe(value) -> dict | None:
    """Normalize '79' / 'CWE-79' / 'cwe 79' to {'id', 'name'}.

    Returns None for anything that is not a plausible CWE id. Unknown but
    well-formed ids are kept with name=None so the report never invents one.
    """
    match = _CWE_RE.match(str(value or "").strip())
    if not match:
        return None
    cwe_id = f"CWE-{match.group(1)}"
    return {"id": cwe_id, "name": CWE_NAMES.get(cwe_id)}
