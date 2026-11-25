# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP audit helpers: normalise a target URL and read a TLS certificate.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _recon_prep_url(raw: str) -> str:
    """Normalize a target into a full HTTP(S) URL for the recon HTTP tools,
    preserving an explicit scheme, port and path. Without a scheme: http for
    localhost / *.local / private IPs / an explicit non-443 port (internal
    services are usually plain HTTP), https otherwise, so a bare `127.0.0.1` or
    `host:8080` target doesn't fail a TLS handshake against an HTTP port."""
    import ipaddress
    raw = (raw or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    hostport = raw.split("/", 1)[0]
    rest = raw[len(hostport):]  # path/query, if any
    host = hostport.split(":", 1)[0]
    port = hostport.split(":", 1)[1] if hostport.count(":") == 1 else ""
    is_local = host in ("localhost", "") or host.endswith(".local")
    if not is_local:
        try:
            ip = ipaddress.ip_address(host)
            is_local = ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            pass
    scheme = "http" if (is_local or (port and port != "443")) else "https"
    return f"{scheme}://{hostport}{rest}"


def _recon_cert_from_der(der: bytes) -> dict | None:
    """Parse a DER cert with `cryptography` if available; else None."""
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
    except Exception:
        return None
    try:
        c = x509.load_der_x509_certificate(der, default_backend())
        try:
            cn = c.subject.rfc4514_string()
        except Exception:
            cn = ""
        try:
            issuer = c.issuer.rfc4514_string()
        except Exception:
            issuer = ""
        sans = []
        try:
            ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans = ext.value.get_values_for_type(x509.DNSName)
        except Exception:
            pass
        return {
            "subject": cn, "issuer": issuer,
            "not_before": c.not_valid_before_utc.isoformat(),
            "not_after": c.not_valid_after_utc.isoformat(),
            "san": sans[:50],
            "self_signed": (cn == issuer and cn != ""),
        }
    except Exception:
        return None
