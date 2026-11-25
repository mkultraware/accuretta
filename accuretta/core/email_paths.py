# SPDX-License-Identifier: AGPL-3.0-only
"""Where the email plugin and its account file live (the account file holds the mailbox password; the file tools refuse it).

Moved out of accuretta/tools/email_settings.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import os
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from pathlib import Path


# ---- email account (Settings > Email) ---------------------------------------
# The email skill's scripts read email_plugin/email_config.json. Settings edits
# that file through these helpers; the password goes in and never comes out.
EMAIL_PLUGIN_DIR = ROOT / "email_plugin"


# ACCURETTA_EMAIL_CONFIG moves the file (the scripts honour it too), so a test
# run never touches the real account.
EMAIL_CONFIG_FILE = Path(os.environ.get("ACCURETTA_EMAIL_CONFIG")
                         or USER_DIR / "email_plugin" / "email_config.json")
