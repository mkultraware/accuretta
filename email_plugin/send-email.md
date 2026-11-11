---
name: "send-email"
description: "Send and read email from the user's own mail account over SMTP and IMAP. Use when the user asks to email, message or notify someone by email, or to check their inbox or sent mail. Always drafts first and sends only after the user confirms."
budget: 520
---

# send-email

Two scripts sit in the same folder as this skill file: `email_send.py` and `email_read.py`. That folder is `{{skill_dir}}`.
Run them with `python` and their full path, as in the commands below.

Everything personal lives in `email_config.json` in that folder: the account, mail servers, password, signature, contacts and writing style. Never open, print or edit that file yourself, because it holds the password. Use the commands below instead.

## First, learn who you are writing as

    python "{{skill_dir}}/email_send.py" --info

Prints the sender identities, the contacts, the signature and the user's writing-style notes. Write every email in that style. Run it once per chat before the first draft.

## Draft, then send

1. Build the draft. This sends nothing and prints exactly what would go out:

       python "{{skill_dir}}/email_send.py" --draft --to <name-or-address> --subject "..." --body "..."

2. Show the user the printed `email` code block, unchanged, in your reply. The app renders it as an email. Then ask whether to send it.
3. Only after the user confirms that specific draft, run the same command without `--draft`.
4. Confirm what went out: recipient, subject, one-line summary. The script also files a copy in the account's Sent folder and says so; pass on its note if that copy failed.

Rules:

- **Draft-first, always.** No proactive, scheduled or inferred sends. A request to email someone is a request for a draft; the send needs its own yes. If the user changes anything, show the new draft and ask again.
- `--to` takes a contact name or a full address. If the name is unknown or ambiguous, ask once. Never guess an address.
- `--from <address>` sends as another identity from `--info`.
- The script adds the signature. Do not write it into `--body`.
- When the user emails someone new, save them after the send: `python "{{skill_dir}}/email_send.py" --add-contact "Name" address`.
- A non-zero exit means it did not send. Read the error and fix the cause; do not retry blindly.
- `python "{{skill_dir}}/email_send.py" --check` tests the login without sending anything.

## Read

    python "{{skill_dir}}/email_read.py" --count 5
    python "{{skill_dir}}/email_read.py" --folder Sent --count 3
    python "{{skill_dir}}/email_read.py" --from <address>

Reading is always fine and needs no confirmation. Summarise unless the user asks for the message itself. To show a message as written, put it in an `email` code block: the `From`, `To`, `Date` and `Subject` lines, a blank line, then the body.
