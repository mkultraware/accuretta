# SPDX-License-Identifier: AGPL-3.0-only
"""The TLS context used for public data sources, made once.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ssl


# Passive lookups go to public services (dns.google, crt.sh, Cert Spotter,
# rdap.org, the Internet Archive, OSV), never to the target. Python 3.13+ turns
# on VERIFY_X509_STRICT, which rejects any chain holding a CA certificate whose
# basicConstraints isn't marked critical. archive.org still serves GoDaddy's old
# cross-signed chain with one, so every archive query failed, while browsers,
# which don't apply that rule, loaded it fine. This context drops only that
# flag: the chain must still reach a trusted root, and hostname and dates are
# still checked. Target-facing tools and the TLS audit keep their own contexts.
_PUBLIC_SOURCE_TLS: ssl.SSLContext | None = None


def _public_source_tls() -> ssl.SSLContext:
    global _PUBLIC_SOURCE_TLS
    if _PUBLIC_SOURCE_TLS is None:
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        _PUBLIC_SOURCE_TLS = ctx
    return _PUBLIC_SOURCE_TLS
