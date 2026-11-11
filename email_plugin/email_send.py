"""Send an email over SMTP from the account in email_config.json.

Usage:
    python email_send.py --info
        Who you send as, the contacts, signature and writing style. No password.
    python email_send.py --draft --to <name-or-address> --subject "..." --body "..."
        Print the exact email that would go out, as an ```email block. Sends nothing.
    python email_send.py --to <name-or-address> --subject "..." --body "..."
        Send it. Add --from <address> to send as another identity.
    python email_send.py --add-contact "Name" address
    python email_send.py --check
        Log in to the mail server and report. Sends nothing.

A copy of each sent email is filed in the account's Sent folder.

--to accepts a contact name (matched against "contacts") or a full address.
Exits non-zero with a message on failure.
"""
import argparse
import json
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import email_common as common


def build_message(cfg, to, subject, body, sender=None):
    sender = sender or cfg["from"]
    sig = (cfg.get("signature") or "").strip()
    if sig:
        body = body.rstrip() + "\n\n" + sig
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "", sender))
    msg["To"] = to
    msg["Subject"] = subject
    # Stamped here so the copy filed in Sent is identical to what was sent.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    msg.set_content(body)
    return msg


def connect(cfg, sender):
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port") or 465)
    # Port 465 is TLS from the first byte; 587 and 25 start plain and upgrade.
    if cfg.get("use_ssl", port == 465):
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    server.login(common.login_name(cfg, sender), cfg["password"])
    return server


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Send an email over SMTP")
    ap.add_argument("--to", help="contact name or full email address")
    ap.add_argument("--subject")
    ap.add_argument("--body")
    ap.add_argument("--from", dest="sender", default=None,
                    help="send as another identity listed by --info")
    ap.add_argument("--draft", action="store_true",
                    help="print the email instead of sending it")
    ap.add_argument("--info", action="store_true",
                    help="print identities, contacts, signature and style")
    ap.add_argument("--add-contact", nargs=2, metavar=("NAME", "ADDRESS"))
    ap.add_argument("--check", action="store_true",
                    help="log in to the mail server without sending")
    a = ap.parse_args()

    if a.info:
        print(json.dumps(common.public_info(common.load(require_password=False)),
                         indent=2, ensure_ascii=False))
        return

    if a.add_contact:
        name, address = (part.strip() for part in a.add_contact)
        if "@" not in address:
            sys.exit(f"'{address}' is not an email address")
        cfg = common.load(require_password=False)
        cfg.setdefault("contacts", {})[name] = address
        common.save(cfg)
        print(f"saved contact {name} -> {address}")
        return

    if a.check:
        cfg = common.load()
        sender = a.sender or cfg["from"]
        with connect(cfg, sender):
            pass
        print(f"login ok: {sender} at {cfg['smtp_host']}")
        if common.should_save_sent(cfg):
            with common.open_imap(cfg, sender) as conn:
                folder = common.find_sent_folder(conn, cfg).strip('"')
            print(f"sent copies go to: {folder}")
        else:
            print("sent copies: left to the mail host")
        return

    if not (a.to and a.subject is not None and a.body is not None):
        ap.error("--to, --subject and --body are required to draft or send")

    cfg = common.load(require_password=not a.draft)
    sender = a.sender or cfg["from"]
    if sender not in common.identities(cfg):
        sys.exit(f"'{sender}' is not one of your identities: {common.identities(cfg)}")
    to = common.resolve_recipient(cfg, a.to)
    msg = build_message(cfg, to, a.subject, a.body, sender)

    if a.draft:
        fence = "`" * 3
        print(f"{fence}email\nFrom: {msg['From']}\nTo: {msg['To']}\n"
              f"Subject: {msg['Subject']}\n\n{msg.get_content().rstrip()}\n{fence}")
        print("\nDRAFT ONLY. Nothing was sent.")
        return

    with connect(cfg, sender) as server:
        server.send_message(msg)
    print(f"sent to {to}")

    # The email is out. A failed copy must never read as a failed send.
    if common.should_save_sent(cfg):
        try:
            print(f"copy saved to {common.save_to_sent(cfg, sender, msg)}")
        except Exception as exc:
            print(f"note: sent, but no copy was saved to the Sent folder ({exc})")


if __name__ == "__main__":
    main()
