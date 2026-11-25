# SPDX-License-Identifier: AGPL-3.0-only
"""Finding secrets in one file of an unpacked app or repository.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from pathlib import Path


# Secret/leak patterns. Conservative: false positives cost an LLM workflow
# more than misses (the model burns context chasing red herrings). Each entry
# is (label, compiled_regex, min_match_length_for_positive).
_APK_SECRET_RE: list[tuple[str, "re.Pattern[bytes]", int]] = [
    ("aws_access_key_id",        re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                                 20),
    ("aws_secret_access_key",    re.compile(rb"(?i)aws(.{0,20})?(secret|sk)[^a-z0-9]{0,5}([A-Za-z0-9/+=]{40})"),  40),
    ("google_api_key",           re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"),                                     39),
    ("firebase_db_url",          re.compile(rb"https?://[a-z0-9\-]+\.firebaseio\.com\b"),                        20),
    ("firebase_app_url",         re.compile(rb"https?://[a-z0-9\-]+\.firebaseapp\.com\b"),                       20),
    ("gcp_service_account",      re.compile(rb"\"type\"\s*:\s*\"service_account\""),                             20),
    ("jwt",                      re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), 60),
    ("github_pat",               re.compile(rb"\bghp_[A-Za-z0-9]{36}\b"),                                         40),
    ("github_app_token",         re.compile(rb"\b(?:ghs|ghu|gho|ghr)_[A-Za-z0-9]{36}\b"),                         40),
    ("slack_token",              re.compile(rb"\bxox[abpr]-[A-Za-z0-9\-]{10,}\b"),                                15),
    ("stripe_secret",            re.compile(rb"\bsk_(?:live|test)_[0-9a-zA-Z]{24,}\b"),                           28),
    ("stripe_publishable",       re.compile(rb"\bpk_(?:live|test)_[0-9a-zA-Z]{24,}\b"),                           28),
    ("private_key_pem",          re.compile(rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"), 30),
    ("twilio_sid",               re.compile(rb"\bAC[a-f0-9]{32}\b"),                                              34),
    ("sendgrid_api_key",         re.compile(rb"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),                  60),
    ("mailgun_api_key",          re.compile(rb"\bkey-[a-z0-9]{32}\b"),                                            36),
    ("generic_secret_assignment",re.compile(rb"(?i)(?:api[_-]?key|secret|password|passwd|pwd|token|bearer)[\s:=\"']{1,5}[A-Za-z0-9_\-/+=]{16,}"), 25),
    ("hardcoded_http_endpoint",  re.compile(rb"https?://[a-zA-Z0-9.\-]+(?::\d+)?(?:/[^\s\"'<>]*)?"),              10),
    ("ipv4_literal",             re.compile(rb"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"), 7),
]


# --- Tool 1: scan_secrets ---
_REPO_SECRET_RE: list[tuple[str, "re.Pattern[bytes]", int]] = [
    # .env file assignments
    ("dotenv_secret",            re.compile(rb"(?i)^\s*(?:DB_PASSWORD|SECRET_KEY|API_SECRET|AWS_SECRET|PRIVATE_KEY|DATABASE_URL|REDIS_URL|MONGO_URI|SMTP_PASSWORD|AUTH_TOKEN)\s*=\s*[^\s#]{8,}", re.MULTILINE), 15),
    # Azure tokens
    ("azure_client_secret",      re.compile(rb"(?i)azure[^\n]{0,30}(?:client.?secret|tenant.?id)[^a-z0-9]{0,5}[A-Za-z0-9_\-\.~]{20,}"), 30),
    # npm tokens
    ("npm_token",                re.compile(rb"//registry\.npmjs\.org/:_authToken=[A-Za-z0-9\-_]{36,}"), 50),
    # PyPI tokens
    ("pypi_token",               re.compile(rb"\bpypi-[A-Za-z0-9_\-]{60,}\b"), 64),
    # Heroku API key
    ("heroku_api_key",           re.compile(rb"(?i)heroku[^\n]{0,20}[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), 40),
    # Slack webhook
    ("slack_webhook",            re.compile(rb"https://hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[A-Za-z0-9]{20,}"), 60),
    # Discord webhook
    ("discord_webhook",          re.compile(rb"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{60,}"), 70),
    # Telegram bot token
    ("telegram_bot_token",       re.compile(rb"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), 44),
    # Connection strings (JDBC, MongoDB, PostgreSQL, MySQL, Redis)
    ("connection_string",        re.compile(rb"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"'<>]{10,}"), 15),
    # YAML/TOML secrets
    ("yaml_password_field",      re.compile(rb"(?i)^\s*(?:password|passwd|secret|api_key|auth_token|private_key)\s*[:=]\s*[^\s#\n]{8,}", re.MULTILINE), 15),
]


def _hunt_secrets_in_file(path: Path, include_http: bool = False, max_per_pattern: int = 25) -> list[dict]:
    try:
        data = path.read_bytes()
    except Exception:
        return []

    out: list[dict] = []
    lines = data.split(b"\n")
    
    for kind, rx, min_hit_len in _APK_SECRET_RE + _REPO_SECRET_RE:
        if not include_http and kind in {"hardcoded_http_endpoint", "ipv4_literal"}:
            continue
            
        seen: set[bytes] = set()
        n = 0
        for i, line in enumerate(lines):
            for m in rx.finditer(line):
                hit = m.group(0)
                if len(hit) < min_hit_len:
                    continue
                if hit in seen:
                    continue
                seen.add(hit)
                try:
                    preview = hit.decode("utf-8", errors="replace")
                except Exception:
                    preview = repr(hit)
                
                if len(preview) > 24 and kind not in {"hardcoded_http_endpoint", "ipv4_literal", "firebase_db_url", "firebase_app_url"}:
                    preview = preview[:8] + "…[redacted]…" + preview[-4:]
                
                out.append({
                    "file": path.name,
                    "kind": kind,
                    "match": preview[:200],
                    "line": i + 1
                })
                n += 1
                if n >= max_per_pattern:
                    break
            if n >= max_per_pattern:
                break
    return out
