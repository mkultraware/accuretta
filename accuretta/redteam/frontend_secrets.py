# SPDX-License-Identifier: AGPL-3.0-only
"""Finding secrets shipped in frontend code: known key formats, JWTs, private
keys and high-entropy strings, minus public keys and placeholders.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import hashlib
import math
import re
from accuretta.redteam.recon_map import _decode_jwt_claims


# Provider-shaped credentials. Public client identifiers have their own list
# and never become findings merely because they occur in a frontend bundle.
_FRONTEND_SECRET_SPECS = (
    {"type": "AWS secret access key", "provider": "aws", "severity": "critical", "group": 1,
     "regex": re.compile(r"(?i)(?:aws_secret_access_key|secretAccessKey)\s*[:=]\s*['\"]([A-Za-z0-9/+=]{40})['\"]")},
    {"type": "Google OAuth access token", "provider": "google", "severity": "high", "group": 0,
     "regex": re.compile(r"ya29\.[0-9A-Za-z_-]{20,}")},
    {"type": "Slack access token", "provider": "slack", "severity": "high", "group": 0,
     "regex": re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")},
    {"type": "Slack webhook", "provider": "slack_webhook", "severity": "high", "group": 0,
     "regex": re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")},
    {"type": "GitHub access token", "provider": "github", "severity": "high", "group": 0,
     "regex": re.compile(r"gh[pousr]_[0-9A-Za-z]{36,255}")},
    {"type": "Stripe live secret key", "provider": "stripe", "severity": "critical", "group": 0,
     "regex": re.compile(r"sk_live_[0-9A-Za-z]{24,}")},
    {"type": "SendGrid API key", "provider": "sendgrid", "severity": "high", "group": 0,
     "regex": re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}")},
    {"type": "Mailgun API key", "provider": "mailgun", "severity": "high", "group": 0,
     "regex": re.compile(r"key-[0-9A-Za-z]{32}")},
    {"type": "Private key", "provider": "private_key", "severity": "critical", "group": 0,
     "regex": re.compile(r"-----BEGIN (?:(?:RSA|EC|OPENSSH|DSA|PGP) )?PRIVATE KEY-----[\s\S]{40,12000}?-----END (?:(?:RSA|EC|OPENSSH|DSA|PGP) )?PRIVATE KEY-----")},
    {"type": "Embedded basic-auth credential", "provider": "basic_auth", "severity": "high", "group": 0,
     "regex": re.compile(r"https?://[A-Za-z0-9._%+-]+:[^@\s/'\"]{3,}@[A-Za-z0-9.-]+")},
)


_FRONTEND_PUBLIC_SPECS = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Stripe publishable key", re.compile(r"pk_live_[0-9A-Za-z]{24,}")),
    ("Twilio API key sid", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("Firebase database URL", re.compile(r"https://[a-z0-9-]+\.firebaseio\.com")),
    ("Supabase project URL", re.compile(r"https://[a-z0-9]+\.supabase\.co")),
)


_FRONTEND_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}")


_GENERIC_SECRET_RE = re.compile(
    r"""['"]?(?P<k>[A-Za-z0-9_]*(?:api[_-]?key|secret|token|passwd|password|pwd|access[_-]?key|client[_-]?secret|auth[_-]?token)[A-Za-z0-9_]*)['"]?\s*[:=]\s*['"](?P<v>[^'"\r\n]{12,512})['"]""",
    re.IGNORECASE,
)


_SECRET_PLACEHOLDER_RE = re.compile(
    r"(?i)(?:example|sample|placeholder|replace[_ -]?me|insert[_ -]?here|your[_ -]?"
    r"(?:api[_ -]?key|secret|token|password)|dummy|fake|mock|changeme|todo|xxx+|"
    r"test[_ -]?(?:key|token|secret)|\$\{|\{\{|<[^>]+>)"
)


_SECRET_CONTEXT_NEGATIVE_RE = re.compile(
    r"(?i)\b(?:example|sample|fixture|mock|storybook|documentation|placeholder|unit.?test)\b"
)


_SECRET_CONTEXT_POSITIVE_RE = re.compile(
    r"(?i)\b(?:authorization|bearer|credential|client_secret|access_token|api_key|"
    r"apikey|password|secret|fetch|axios|headers|initialize|config)\b"
)


def _secret_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {char: value.count(char) for char in set(value)}
    total = float(len(value))
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _secret_is_placeholder(value: str, *, generic: bool = False) -> bool:
    cleaned = value.strip()
    low = cleaned.lower()
    if not cleaned or _SECRET_PLACEHOLDER_RE.search(cleaned):
        return True
    if len(set(low)) <= 4 and len(cleaned) >= 12:
        return True
    if re.fullmatch(r"[0-9a-f]{32,128}", low) and generic:
        return True
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", low):
        return True
    return low in {"undefined", "null", "true", "false", "not-set", "none"}


def _secret_redact(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    if len(compact) <= 10:
        return compact[:2] + "…" + compact[-2:]
    return compact[:6] + "…" + compact[-4:]


def _private_key_complete(value: str) -> bool:
    try:
        body = re.sub(r"-----[^-]+-----", "", value)
        body = re.sub(r"\s+", "", body)
        return len(_b64.b64decode(body, validate=True)) >= 64
    except Exception:
        return False


def _scan_frontend_source(source: str, text: str) -> tuple[list[dict], list[dict], list[dict]]:
    findings: list[dict] = []
    candidates: list[dict] = []
    public_ids: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, value: str, start: int, *, provider: str = "generic",
            severity: str = "medium", confidence: str = "candidate", evidence: str = "") -> None:
        value = value.strip()
        fingerprint = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
        if not value or fingerprint in seen:
            return
        seen.add(fingerprint)
        context = text[max(0, start - 140):min(len(text), start + len(value) + 140)]
        if _secret_is_placeholder(value, generic=provider == "generic"):
            return
        if provider == "generic" and _SECRET_CONTEXT_NEGATIVE_RE.search(context):
            return
        if provider == "private_key":
            if not _private_key_complete(value):
                return
            confidence = "verified_structure"
            evidence = "complete, decodable private-key block"
        record = {
            "type": kind,
            "provider": provider,
            "severity": severity,
            "confidence": confidence,
            "source": source,
            "line": text.count("\n", 0, start) + 1,
            "preview": _secret_redact(value),
            "fingerprint": fingerprint[:12],
            "evidence": evidence,
            "_value": value,
        }
        (findings if confidence in {"verified_structure", "high_confidence"} else candidates).append(record)

    for spec in _FRONTEND_SECRET_SPECS:
        for match in spec["regex"].finditer(text):
            group = int(spec.get("group") or 0)
            add(spec["type"], match.group(group), match.start(group),
                provider=spec["provider"], severity=spec["severity"],
                confidence="high_confidence",
                evidence="provider-specific secret-bearing format")

    for label, regex in _FRONTEND_PUBLIC_SPECS:
        for match in regex.finditer(text):
            value = match.group(0)
            fingerprint = hashlib.sha256(value.encode()).hexdigest()
            if fingerprint in seen or _secret_is_placeholder(value):
                continue
            seen.add(fingerprint)
            public_ids.append({
                "type": label,
                "source": source,
                "line": text.count("\n", 0, match.start()) + 1,
                "preview": _secret_redact(value),
                "fingerprint": fingerprint[:12],
                "reason": "public client identifier or endpoint; exposure alone is not a vulnerability",
                "_value": value,
            })

    for match in _FRONTEND_JWT_RE.finditer(text):
        value = match.group(0)
        payload, detail = _decode_jwt_claims(value)
        if payload is None or detail == "expired JWT":
            continue
        add("Embedded JWT", value, match.start(), provider="jwt", severity="medium",
            confidence="candidate", evidence=detail)

    for match in _GENERIC_SECRET_RE.finditer(text):
        name, value = match.group("k"), match.group("v").strip()
        entropy = _secret_entropy(value)
        if entropy < 3.35:
            continue
        context = text[max(0, match.start() - 140):min(len(text), match.end() + 140)]
        strong_name = bool(re.search(
            r"(?i)(?:client[_-]?secret|private[_-]?key|auth[_-]?token|access[_-]?token|password|passwd)",
            name,
        ))
        positive = bool(_SECRET_CONTEXT_POSITIVE_RE.search(context))
        confidence = "high_confidence" if strong_name and positive and entropy >= 3.7 else "candidate"
        add(f"Hardcoded {name}", value, match.start("v"), provider="generic",
            severity="high" if confidence == "high_confidence" else "medium",
            confidence=confidence,
            evidence=f"secret-bearing assignment with entropy {entropy:.2f}")

    return findings, candidates, public_ids
