# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team probing tools: encode/decode helper, the parameter fuzzer, the CORS probe
and raw TCP send.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import random
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.settings import get_settings
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.governor import _rt_gov_preflight
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.network_scope import _rt_connect_block
from accuretta.redteam.recon_fetch import _ScopeCheckedRedirect
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.recon_fetch import _rt_proxy_handler
from accuretta.redteam.request_headers import _rt_ua
from concurrent.futures import ThreadPoolExecutor


def tool_encode_decode(args: dict) -> dict:
    """AUTHORIZED PENTEST. Encode or decode a string with the common web-app
    schemes: base64 / base64url, URL percent-encoding, hex, and JWT segment
    decode (the Burp "Decoder" equivalent), so the model can inspect and forge
    tokens without doing base64 by hand. Pure string transform, no I/O. Gated
    behind red_team_enabled.

    args: text, scheme (base64|base64url|url|hex|jwt, default base64), operation
    (encode|decode, default encode). base64 encode returns both the standard and
    url-safe alphabets (unsigned cookies commonly use url-safe)."""
    # Module aliases the stdlib as `_b64` (import base64 as _b64), so a bare
    # `base64.` name isn't bound here — import it locally, as other tools do.
    import base64
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    text = args.get("text")
    if text is None:
        text = args.get("input") or args.get("value") or args.get("data") or ""
    if not isinstance(text, str):
        # A model may hand us a dict/list to encode (e.g. a forged cookie payload).
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))

    scheme = (args.get("scheme") or args.get("format") or "base64").lower().strip()
    op = (args.get("operation") or args.get("op") or "encode").lower().strip()
    # Forgive the common synonyms small models invent.
    scheme = {"b64": "base64", "base-64": "base64", "b64url": "base64url",
              "base64-url": "base64url", "urlsafe": "base64url", "percent": "url",
              "urlencode": "url", "url-encode": "url", "urlencoded": "url",
              "htmlentities": "html", "html-entities": "html", "htmlescape": "html",
              "rot-13": "rot13", "rot": "rot13", "unicode-escape": "unicode",
              "gzip": "gzip-b64", "gzipb64": "gzip-b64", "gzip_b64": "gzip-b64",
              "gzip-base64": "gzip-b64"}.get(scheme, scheme)
    op = {"enc": "encode", "dec": "decode"}.get(op, op)

    def _b64_decode(s: str) -> str:
        # Accept either alphabet and tolerate stripped padding/whitespace.
        s2 = "".join(s.split()).replace("-", "+").replace("_", "/")
        return base64.b64decode(s2 + "=" * (-len(s2) % 4)).decode("utf-8", "replace")

    try:
        if scheme in ("base64", "base64url"):
            if op == "decode":
                return {"scheme": scheme, "operation": "decode", "result": _b64_decode(text)}
            raw = text.encode("utf-8")
            std = base64.b64encode(raw).decode("ascii")
            urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
            return {"scheme": scheme, "operation": "encode",
                    "result": urlsafe if scheme == "base64url" else std,
                    "base64": std, "base64url": urlsafe}
        if scheme == "url":
            if op == "decode":
                return {"scheme": "url", "operation": "decode", "result": urllib.parse.unquote(text)}
            return {"scheme": "url", "operation": "encode", "result": urllib.parse.quote(text, safe="")}
        if scheme == "hex":
            if op == "decode":
                return {"scheme": "hex", "operation": "decode",
                        "result": bytes.fromhex("".join(text.split())).decode("utf-8", "replace")}
            return {"scheme": "hex", "operation": "encode", "result": text.encode("utf-8").hex()}
        if scheme == "html":
            import html as _html
            if op == "decode":
                return {"scheme": "html", "operation": "decode", "result": _html.unescape(text)}
            return {"scheme": "html", "operation": "encode", "result": _html.escape(text, quote=True)}
        if scheme == "rot13":
            # Self-inverse; letters only, everything else passes through.
            import codecs
            return {"scheme": "rot13", "operation": op, "result": codecs.encode(text, "rot_13")}
        if scheme == "unicode":
            if op == "decode":
                s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
                s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
                return {"scheme": "unicode", "operation": "decode", "result": s}
            return {"scheme": "unicode", "operation": "encode",
                    "result": "".join("\\u%04x" % ord(c) for c in text)}
        if scheme == "gzip-b64":
            import gzip
            if op == "decode":
                packed = "".join(text.split()).replace("-", "+").replace("_", "/")
                raw = base64.b64decode(packed + "=" * (-len(packed) % 4))
                return {"scheme": "gzip-b64", "operation": "decode",
                        "result": gzip.decompress(raw).decode("utf-8", "replace")}
            comp = gzip.compress(text.encode("utf-8"))
            return {"scheme": "gzip-b64", "operation": "encode",
                    "result": base64.b64encode(comp).decode("ascii"),
                    "base64url": base64.urlsafe_b64encode(comp).decode("ascii")}
        if scheme == "jwt":
            # Always a decode: split header.payload.signature and decode the two
            # base64url JSON segments. Signature is shown but NOT verified — the
            # point is to inspect/forge, not validate.
            parts = text.strip().split(".")
            if len(parts) < 2:
                return {"error": "not a JWT — expected header.payload.signature"}

            def _seg(seg: str):
                return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8", "replace"))

            return {"scheme": "jwt", "operation": "decode",
                    "header": _seg(parts[0]), "payload": _seg(parts[1]),
                    "signature": parts[2] if len(parts) > 2 else "",
                    "note": "signature NOT verified — inspect/forge only"}
        return {"error": f"unknown scheme '{scheme}' — use base64, base64url, url, hex, "
                          "html, unicode, rot13, gzip-b64, or jwt"}
    except Exception as e:
        return {"error": f"{scheme} {op} failed: {e}", "input_preview": text[:120]}


def tool_fuzz(args: dict) -> dict:
    """AUTHORIZED PENTEST. Iterate a request parameter over many values and report
    which responses DIFFER from the norm — the primitive for enumeration/IDOR
    (find the one object id that leaks), parameter/endpoint brute forcing, and
    access checks. Put FUZZ in the url where the value goes, OR pass `param`;
    supply `values` (list) or `range`=[start,end] (integers). Returns the outlier
    responses (status/size differ from the common baseline) so the interesting
    one is obvious. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    param = args.get("param")
    if "FUZZ" not in url and not param:
        return {"error": "put FUZZ in the url where the value goes, or pass `param`"}
    values = args.get("values")
    rng = args.get("range")
    if not values and isinstance(rng, list) and len(rng) == 2:
        try:
            values = [str(i) for i in range(int(rng[0]), int(rng[1]) + 1)]
        except Exception:
            return {"error": "range must be [start, end] integers"}
    if not values:
        return {"error": "provide `values` (list) or `range` [start, end]"}
    values = [str(v) for v in values][:300]
    _gov = _rt_gov_preflight(len(values))
    if _gov:
        return {"error": _gov, "url": url, "rate_limited": True}
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 12.0))

    def build(v):
        if "FUZZ" in url:
            return url.replace("FUZZ", urllib.parse.quote(v, safe=""))
        p = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        q = ([(k, (v if k == param else vv)) for k, vv in q]
             if any(k == param for k, _ in q) else q + [(param, v)])
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))

    def probe(v):
        r = _recon_fetch(build(v), timeout=timeout, max_bytes=4000)
        body = r.get("body", "") or ""
        return {"value": v, "status": r.get("status"), "size": len(body),
                "snippet": body[:140].replace("\n", " ")}

    from collections import Counter
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="fuzz") as ex:
        results = list(ex.map(probe, values))
    counts = Counter((r["status"], r["size"]) for r in results)
    modal = counts.most_common(1)[0][0] if counts else None
    distinct = [r for r in results if (r["status"], r["size"]) != modal]
    return {"url": url, "tested": len(results),
            "modal_response": ({"status": modal[0], "size": modal[1]} if modal else None),
            "distinct_count": len(distinct), "distinct": distinct[:50],
            "note": "the distinct responses differ from the common baseline — the outliers are "
                    "where the interesting data (another user's object, a valid id) lives."}


def tool_cors_probe(args: dict) -> dict:
    """AUTHORIZED PENTEST. Test a URL for CORS misconfiguration: sends requests
    with crafted Origin headers and checks whether the server REFLECTS the origin
    in Access-Control-Allow-Origin — especially alongside Access-Control-Allow-
    Credentials: true, the combination that lets an attacker-controlled site read
    authenticated responses. Also flags accepted 'null' origin and wildcard ACAO.
    Read-only. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    host = urllib.parse.urlparse(url).netloc.split(":")[0]
    rand = "".join(random.choice("abcdefghijklmnop") for _ in range(6))
    tests = [
        ("arbitrary origin", f"https://evil-{rand}.com"),
        ("null origin", "null"),
        ("target as subdomain of attacker", f"https://{host}.evil-{rand}.com"),
        ("attacker as subdomain of target", f"https://evil-{rand}.{host}"),
    ]

    _gov = _rt_gov_preflight(len(tests))
    if _gov:
        return {"error": _gov, "rate_limited": True, "url": url}

    def probe(origin):
        if _rt_gov_admit(_rt_url_host(url), "GET", url=url):
            return {}
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": _rt_ua(), "Origin": origin})
        _cors_handlers = [_ScopeCheckedRedirect(), urllib.request.HTTPSHandler(context=ctx)]
        _ph = _rt_proxy_handler()
        if _ph:
            _cors_handlers.append(_ph)
        opener = urllib.request.build_opener(*_cors_handlers)
        try:
            resp = opener.open(req, timeout=timeout)
            return {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            return {k.lower(): v for k, v in e.headers.items()}
        except Exception:
            return {}

    results = []
    for label, origin in tests:
        h = probe(origin)
        acao = h.get("access-control-allow-origin", "")
        acac = (h.get("access-control-allow-credentials", "") or "").strip().lower()
        vuln, severity, detail = False, "info", "no CORS reflection"
        if acao and acao == origin:
            vuln = True
            detail = "server reflects the attacker Origin into Access-Control-Allow-Origin"
            severity = "high" if acac == "true" else "medium"
        elif acao == "null" and origin == "null":
            vuln = True
            detail = "server accepts the 'null' origin"
            severity = "high" if acac == "true" else "medium"
        elif acao == "*":
            detail = "wildcard ACAO (credentials not sendable; low risk unless the data is sensitive)"
            severity = "low"
        results.append({"test": label, "origin_sent": origin, "acao": acao,
                        "allow_credentials": acac or "(unset)", "vulnerable": vuln,
                        "severity": severity, "detail": detail})

    any_vuln = any(r["vulnerable"] for r in results)
    return {"url": url, "vulnerable": any_vuln, "tests": results,
            "note": ("CORS misconfiguration: an attacker-controlled page can read this endpoint's "
                     "responses — critical if it returns authenticated/sensitive data. Confirm with "
                     "a credentialed request."
                     if any_vuln else "no exploitable CORS reflection detected.")}


def tool_tcp_send(args: dict) -> dict:
    """AUTHORIZED PENTEST. Open a raw TCP connection to host:port, send data, and
    read back whatever the service returns within a read window. This is a raw
    socket, NOT a browser: it speaks bytes only, with no rendering or JS. Use it
    to talk to non-HTTP services (redis, memcached, SMTP) or to submit a URL to a
    CTF / report-to-admin bot and read the console.log output it pipes back over
    the socket. For a bot that visits a page and waits, set read_timeout high
    enough (e.g. 15). Optionally wrap in TLS. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    host = (args.get("host") or args.get("target") or "").strip()
    port = args.get("port")
    if host and ":" in host and not port:  # accept host:port in one field
        host, _, port = host.rpartition(":")
    if not host:
        return {"error": "host required"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"error": "valid port required"}
    data = args.get("data")
    if data is None:
        data = args.get("payload") or ""
    send_bytes = (data if isinstance(data, str) else str(data)).encode("utf-8", "replace")
    use_tls = bool(args.get("tls"))
    connect_timeout = max(1.0, min(float(args.get("connect_timeout") or 6.0), 20.0))
    read_timeout = max(0.5, min(float(args.get("read_timeout") or 8.0), 40.0))
    max_bytes = 64 * 1024

    _refusal = _rt_connect_block(host, port)
    if _refusal:
        return {"error": _refusal, "scope_blocked": True}
    _refusal = _rt_gov_admit(host, "TCP", url=f"tcp://{host}:{port}")
    if _refusal:
        return {"error": _refusal, "rate_limited": True}
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(connect_timeout)
    try:
        s.connect((host, port))
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return {"error": f"connect to {host}:{port} failed: {e}"}
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        if send_bytes:
            s.sendall(send_bytes)
        # Read until the peer closes the connection or the read window elapses
        # (a visit-and-wait bot streams its output, then closes, so this returns
        # as soon as it finishes rather than always blocking the full window).
        chunks, total = [], 0
        deadline = time.time() + read_timeout
        s.settimeout(1.0)
        while time.time() < deadline and total < max_bytes:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not chunk:
                break  # peer closed
            chunks.append(chunk)
            total += len(chunk)
        text = b"".join(chunks).decode("utf-8", "replace")
        return {"host": host, "port": port, "tls": use_tls,
                "sent_bytes": len(send_bytes), "received_bytes": total,
                "response": text[:60000],
                "note": "raw socket response (no rendering, no JS). If you submitted a URL to a "
                        "bot, any console.log it emitted is in the response above."}
    except Exception as e:
        return {"error": f"tcp exchange failed: {e}", "host": host, "port": port}
    finally:
        try:
            s.close()
        except Exception:
            pass
