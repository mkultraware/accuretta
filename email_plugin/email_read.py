"""Read email over IMAP from the account in email_config.json.

Usage:
    python email_read.py                    # latest 1 message from INBOX
    python email_read.py --count 5          # latest 5
    python email_read.py --folder Sent      # any IMAP folder
    python email_read.py --from <address>   # another identity listed by email_send.py --info

Prints each message as From, To, Date, Subject, a blank line, then the text
body (truncated).
"""
import argparse
import email
import html as htmllib
import re
import sys
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

import email_common as common

MAX_BODY = 2000


def decode(value):
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def html_to_text(raw):
    raw = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = htmllib.unescape(raw)
    return re.sub(r"[ \t]+", " ", raw)


def text_body(msg):
    plain, html_parts = [], []

    def collect(part):
        payload = part.get_payload(decode=True)
        if not payload:
            return
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            html_parts.append(text)

    if msg.is_multipart():
        for part in msg.walk():
            collect(part)
    else:
        collect(msg)

    body = "\n".join(p for p in plain if p.strip())
    if not body.strip() and html_parts:
        body = html_to_text("\n".join(html_parts))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + f"\n... [truncated, {len(body)} chars total]"
    return body


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Read email over IMAP")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--from", dest="identity", default=None)
    a = ap.parse_args()

    cfg = common.load()
    user = a.identity or cfg["from"]
    with common.open_imap(cfg, user) as m:
        status, data = m.select(a.folder)
        if status != "OK":
            sys.exit(f"could not open {a.folder}: {data}")
        total = int(data[0])
        if total == 0:
            print(f"{a.folder} is empty")
            return
        start = max(1, total - a.count + 1)
        status, data = m.fetch(f"{start}:{total}", "(RFC822)")
        if status != "OK":
            sys.exit(f"fetch failed: {data}")

    # Python 3.14 imaplib: each fetched message is one 2-tuple
    # (b'N (RFC822 {size}', b'<full message>'), followed by a bare b')'.
    msgs = [item[1] for item in data
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)]

    for i, raw in enumerate(msgs):
        msg = email.message_from_bytes(raw)
        when = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        print(f"--- [{start + i}] ---")
        print(f"From: {decode(msg['From'])}")
        print(f"To: {decode(msg['To'])}")
        print(f"Date: {when.astimezone().strftime('%d %b %Y, %H:%M') if when else 'unknown'}")
        print(f"Subject: {decode(msg['Subject'])}")
        print()
        print(text_body(msg) or "(no text body)")
        print()


if __name__ == "__main__":
    main()
