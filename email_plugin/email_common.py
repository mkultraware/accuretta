"""Shared config handling for email_send.py and email_read.py.

Everything personal (account, servers, password, signature, contacts, writing
style) lives in email_config.json next to these scripts. Nothing personal
belongs in the scripts or in send-email.md. See email_config.example.json.

Optional keys the example leaves out:
    "username"     log in with this instead of the sending address
    "save_sent"    true/false: keep a copy of each sent email in the Sent folder
                   (default: on, except for hosts that already do it themselves)
    "sent_folder"  name of that folder, when the server does not mark one
"""
import imaplib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Settings > Email in Accuretta writes this file. ACCURETTA_EMAIL_CONFIG moves it.
CFG = Path(os.environ.get("ACCURETTA_EMAIL_CONFIG") or HERE / "email_config.json")
EXAMPLE = "email_config.example.json"


def load(require_password: bool = True) -> dict:
    if not CFG.exists():
        sys.exit("Email is not set up yet. Fill it in under Settings > Email in Accuretta "
                 f"(or copy {EXAMPLE} to {CFG.name} next to the scripts).")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    for key in ("from", "smtp_host"):
        if not cfg.get(key):
            sys.exit(f'{CFG.name}: "{key}" is not set')
    if require_password and not cfg.get("password"):
        sys.exit(f"{CFG.name} has no password set yet")
    return cfg


def save(cfg: dict) -> None:
    tmp = CFG.with_name(CFG.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CFG)


def identities(cfg: dict) -> list[str]:
    """The default address first, then any extra addresses the account can send as."""
    out = [cfg["from"]]
    for addr in cfg.get("identities") or []:
        if addr and addr not in out:
            out.append(addr)
    return out


def login_name(cfg: dict, sender: str) -> str:
    """Most hosts log in with the sending address; "username" overrides that."""
    return cfg.get("username") or sender


def imap_host(cfg: dict) -> str:
    host = cfg.get("imap_host")
    if host:
        return host
    smtp = cfg["smtp_host"]
    return "imap." + smtp[len("smtp."):] if smtp.startswith("smtp.") else smtp


# These hosts file a copy in Sent themselves for mail sent over SMTP, so a
# second copy from us would show up as a duplicate.
_SELF_SAVING_HOSTS = ("gmail.com", "googlemail.com", "office365.com", "outlook.com")


def should_save_sent(cfg: dict) -> bool:
    if "save_sent" in cfg:
        return bool(cfg["save_sent"])
    return not cfg["smtp_host"].lower().endswith(_SELF_SAVING_HOSTS)


_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


def find_sent_folder(conn, cfg: dict) -> str:
    """The server's Sent folder: the configured one, else the folder the server
    marks as its Sent folder, else a common name that exists, else "Sent"."""
    if cfg.get("sent_folder"):
        return _quote_folder(cfg["sent_folder"])
    status, rows = conn.list()
    names, flagged = [], ""
    for row in rows if status == "OK" else []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
        m = _LIST_LINE.search(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        names.append(name)
        if r"\sent" in m.group("flags").lower() and not flagged:
            flagged = name
    if flagged:
        return flagged
    by_plain = {n.strip('"').lower(): n for n in names}
    for guess in ("sent", "sent items", "sent mail", "sent messages", "inbox.sent"):
        if guess in by_plain:
            return by_plain[guess]
    return "Sent"


def _quote_folder(name: str) -> str:
    name = name.strip()
    if name.startswith('"') or not re.search(r'[\s"\\]', name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_imap(cfg: dict, user: str):
    conn = imaplib.IMAP4_SSL(imap_host(cfg), int(cfg.get("imap_port") or 993), timeout=30)
    conn.login(login_name(cfg, user), cfg["password"])
    return conn


def save_to_sent(cfg: dict, sender: str, msg) -> str:
    """File a copy of a sent message, marked read. Returns the folder name."""
    with open_imap(cfg, sender) as conn:
        folder = find_sent_folder(conn, cfg)
        status, data = conn.append(folder, r"\Seen", imaplib.Time2Internaldate(time.time()),
                                   msg.as_bytes())
        if status != "OK":
            raise RuntimeError(f"server refused the copy: {data}")
    return folder.strip('"')


def resolve_recipient(cfg: dict, to: str) -> str:
    """A full address passes through; a name is looked up in "contacts"."""
    to = to.strip()
    if "@" in to:
        return to
    contacts = cfg.get("contacts") or {}
    matches = {k: v for k, v in contacts.items() if to.lower() in k.lower()}
    if len(matches) != 1:
        sys.exit(f"contact '{to}' is ambiguous or unknown. known: {sorted(contacts)}")
    return next(iter(matches.values()))


def public_info(cfg: dict) -> dict:
    """What the assistant may see. Never includes the password or server login."""
    return {
        "from_name": cfg.get("from_name") or "",
        "identities": identities(cfg),
        "signature": cfg.get("signature") or "",
        "style": cfg.get("style") or "",
        "contacts": cfg.get("contacts") or {},
        "password_set": bool(cfg.get("password")),
    }
