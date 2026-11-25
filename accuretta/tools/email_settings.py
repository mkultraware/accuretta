# SPDX-License-Identifier: AGPL-3.0-only
"""The email account settings: reading and validating the mail account file the
email plugin uses.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import threading
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from pathlib import Path


from accuretta.core.email_paths import (  # noqa: F401  re-export: moved 2026-11-23
    EMAIL_PLUGIN_DIR,
    EMAIL_CONFIG_FILE,
)


_EMAIL_LOCK = threading.Lock()


_EMAIL_ADDR_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")


_EMAIL_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _email_config_raw() -> dict:
    data = load_json(EMAIL_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _email_port(value, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def email_settings() -> dict:
    """The email account as Settings shows it. Never includes the password."""
    cfg = _email_config_raw()
    sender = str(cfg.get("from") or "").strip()
    contacts = cfg.get("contacts") if isinstance(cfg.get("contacts"), dict) else {}
    identities = cfg.get("identities") if isinstance(cfg.get("identities"), list) else []
    return {
        "available": (EMAIL_PLUGIN_DIR / "email_send.py").is_file(),
        "configured": bool(sender and cfg.get("smtp_host") and cfg.get("password")),
        "password_set": bool(cfg.get("password")),
        "from": sender,
        "from_name": str(cfg.get("from_name") or ""),
        "identities": [str(a) for a in identities if a and str(a) != sender],
        "smtp_host": str(cfg.get("smtp_host") or ""),
        "smtp_port": _email_port(cfg.get("smtp_port"), 465),
        "imap_host": str(cfg.get("imap_host") or ""),
        "imap_port": _email_port(cfg.get("imap_port"), 993),
        "signature": str(cfg.get("signature") or ""),
        "style": str(cfg.get("style") or ""),
        "contacts": {str(k): str(v) for k, v in contacts.items()},
    }


def _email_text(value, field: str, limit: int, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field} must be text of at most {limit} characters")
    allowed = "\n\t" if multiline else ""
    if any(ord(ch) < 32 and ch not in allowed for ch in value.replace("\r\n", "\n")):
        raise ValueError(f"{field} contains characters that are not allowed")
    return value.replace("\r\n", "\n").strip()


def _email_address(value, field: str, required: bool = True) -> str:
    text = _email_text(value, field, 254)
    if not text and not required:
        return ""
    if not _EMAIL_ADDR_RE.fullmatch(text):
        raise ValueError(f"{field} is not an email address")
    return text


def update_email_settings(updates: dict) -> dict:
    """Validate and save the fields Settings sends. Keys it does not send, and
    keys only the scripts know (username, save_sent, sent_folder), are kept."""
    with _EMAIL_LOCK:
        cfg = _email_config_raw()
        if "from" in updates:
            cfg["from"] = _email_address(updates["from"], "Email address", required=False)
        if "from_name" in updates:
            cfg["from_name"] = _email_text(updates["from_name"], "Name", 80)
        if "identities" in updates:
            raw = updates["identities"]
            if not isinstance(raw, list) or len(raw) > 20:
                raise ValueError("Other addresses must be a list of at most 20")
            cfg["identities"] = [_email_address(a, "Other address") for a in raw]
        for key, label in (("smtp_host", "Outgoing server"), ("imap_host", "Incoming server")):
            if key in updates:
                host = _email_text(updates[key], label, 253)
                if host and not _EMAIL_HOST_RE.fullmatch(host):
                    raise ValueError(f"{label} must be a host name such as mail.example.com")
                cfg[key] = host
        for key, label in (("smtp_port", "Outgoing port"), ("imap_port", "Incoming port")):
            if key in updates:
                port = _email_port(updates[key], 0)
                if not port:
                    raise ValueError(f"{label} must be a number from 1 to 65535")
                cfg[key] = port
        if "smtp_port" in updates:
            # 465 is encrypted from the first byte; other ports upgrade after connecting.
            cfg["use_ssl"] = cfg["smtp_port"] == 465
        # An empty password field means "keep the saved one".
        if updates.get("password"):
            password = updates["password"]
            if not isinstance(password, str) or len(password) > 512 or any(ord(ch) < 32 for ch in password):
                raise ValueError("Password is not valid")
            cfg["password"] = password
        if "signature" in updates:
            cfg["signature"] = _email_text(updates["signature"], "Signature", 2000, multiline=True)
        if "style" in updates:
            cfg["style"] = _email_text(updates["style"], "Writing style", 2000, multiline=True)
        if "contacts" in updates:
            raw = updates["contacts"]
            if not isinstance(raw, dict) or len(raw) > 500:
                raise ValueError("Contacts must be a list of at most 500 people")
            contacts = {}
            for name, address in raw.items():
                clean = _email_text(name, "Contact name", 80)
                if not clean:
                    raise ValueError("A contact needs a name")
                contacts[clean] = _email_address(address, f"Address for {clean}")
            cfg["contacts"] = contacts
        EMAIL_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        save_json(EMAIL_CONFIG_FILE, cfg)
    return email_settings()


def _email_imap_host(cfg: dict) -> str:
    host = str(cfg.get("imap_host") or "").strip()
    if host:
        return host
    smtp = str(cfg.get("smtp_host") or "").strip()
    return "imap." + smtp[len("smtp."):] if smtp.startswith("smtp.") else smtp


def email_test_connection() -> dict:
    """Log in to the outgoing and incoming servers. Sends and reads nothing."""
    import imaplib
    import smtplib
    cfg = _email_config_raw()
    sender = str(cfg.get("from") or "").strip()
    host = str(cfg.get("smtp_host") or "").strip()
    if not (sender and host and cfg.get("password")):
        return {"ok": False, "message": "Fill in the address, password and outgoing server first."}
    user = str(cfg.get("username") or sender)
    port = _email_port(cfg.get("smtp_port"), 465)
    stage = f"outgoing server {host}"
    try:
        if cfg.get("use_ssl", port == 465):
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        with server:
            server.login(user, cfg["password"])
        imap = _email_imap_host(cfg)
        stage = f"incoming server {imap}"
        with imaplib.IMAP4_SSL(imap, _email_port(cfg.get("imap_port"), 993), timeout=20) as conn:
            conn.login(user, cfg["password"])
    except (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error):
        return {"ok": False, "message": f"The {stage} refused the login. Check the address and password."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not connect to the {stage}: {exc}"}
    return {"ok": True, "message": f"Signed in as {sender}. Sending and reading both work."}
