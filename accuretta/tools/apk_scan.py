# SPDX-License-Identifier: AGPL-3.0-only
"""The scan_apk tool: an Android app's manifest, permissions, exported components and
embedded secrets.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.optional_imports import _AndroidAPK
from accuretta.core.optional_imports import _HAVE_ANDROGUARD
from accuretta.security.path_guards import _fw_check_path
from accuretta.tools.secret_hunt import _APK_SECRET_RE
from pathlib import Path


# Permissions Android marks as "dangerous" or otherwise interesting. We use
# a static list rather than querying androguard at runtime — covers the bulk
# of real-world risk surface without a 1000-entry dump.
_APK_DANGEROUS_PERMS = {
    "android.permission.READ_CONTACTS",
    "android.permission.WRITE_CONTACTS",
    "android.permission.READ_CALENDAR",
    "android.permission.WRITE_CALENDAR",
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.READ_PHONE_STATE",
    "android.permission.READ_PHONE_NUMBERS",
    "android.permission.READ_CALL_LOG",
    "android.permission.WRITE_CALL_LOG",
    "android.permission.PROCESS_OUTGOING_CALLS",
    "android.permission.READ_SMS",
    "android.permission.SEND_SMS",
    "android.permission.RECEIVE_SMS",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.MANAGE_EXTERNAL_STORAGE",
    "android.permission.SYSTEM_ALERT_WINDOW",
    "android.permission.REQUEST_INSTALL_PACKAGES",
    "android.permission.PACKAGE_USAGE_STATS",
    "android.permission.BIND_ACCESSIBILITY_SERVICE",
    "android.permission.BIND_DEVICE_ADMIN",
    "android.permission.QUERY_ALL_PACKAGES",
    "android.permission.READ_LOGS",
    "android.permission.GET_ACCOUNTS",
    "android.permission.AUTHENTICATE_ACCOUNTS",
    "android.permission.USE_CREDENTIALS",
    "android.permission.MANAGE_ACCOUNTS",
    "android.permission.WRITE_SETTINGS",
    "android.permission.WRITE_SECURE_SETTINGS",
    "android.permission.MOUNT_UNMOUNT_FILESYSTEMS",
    "android.permission.INSTALL_PACKAGES",
    "android.permission.DELETE_PACKAGES",
    "android.permission.CLEAR_APP_USER_DATA",
    "android.permission.RECEIVE_BOOT_COMPLETED",
    "android.permission.WAKE_LOCK",
    "android.permission.DISABLE_KEYGUARD",
    "android.permission.SYSTEM_OVERLAY_WINDOW",
}


def _apk_hunt_secrets(label: str, data: bytes, max_per_pattern: int = 25) -> list[dict]:
    """Run all _APK_SECRET_RE patterns over a blob and return findings.
    Each finding includes a redacted preview so the model sees enough context
    to judge severity without us echoing every hardcoded token verbatim."""
    out: list[dict] = []
    for kind, rx, min_hit_len in _APK_SECRET_RE:
        seen: set[bytes] = set()
        n = 0
        for m in rx.finditer(data):
            hit = m.group(0)
            if len(hit) < min_hit_len:
                continue
            if hit in seen:
                continue
            seen.add(hit)
            try:
                preview = hit.decode("utf-8", errors="replace")
            except Exception:
                preview = repr(hit)
            # Redact the middle of long literals — keep first 8 + last 4.
            if len(preview) > 24 and kind not in {"hardcoded_http_endpoint", "ipv4_literal", "firebase_db_url", "firebase_app_url"}:
                preview = preview[:8] + "…[redacted]…" + preview[-4:]
            out.append({"source": label, "kind": kind, "match": preview[:200]})
            n += 1
            if n >= max_per_pattern:
                break
    return out


def tool_scan_apk(args: dict) -> dict:
    """Phase-1 APK static scan. Returns a structured report covering:
    package metadata, signing certs, requested permissions (flagged for
    'dangerous' ones), exported components, dex/native lib inventory,
    and a secret-pattern hunt over DEX + .so + raw resource files. Pure
    Python — uses androguard if available, falls back to zipfile-only mode
    for basic file inventory if not."""
    import zipfile as _zip

    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    max_secret_findings = max(10, min(int(args.get("max_secret_findings") or 200), 2000))
    max_string_bytes = max(64 * 1024, min(int(args.get("max_string_bytes") or 8 * 1024 * 1024),
                                          64 * 1024 * 1024))
    deep = bool(args.get("deep"))  # if true, scan every file in the APK, not just dex/so

    out: dict = {
        "path": path,
        "size": os.path.getsize(path),
        "androguard_available": _HAVE_ANDROGUARD,
        "manifest": {},
        "signing": {},
        "permissions": {"all": [], "dangerous": []},
        "components": {"activities": [], "services": [], "receivers": [], "providers": []},
        "exported_components": [],
        "dex_files": [],
        "native_libs": [],
        "assets": [],
        "secret_findings": [],
        "warnings": [],
        "notes": [],
    }

    # --- step 1: ZIP inventory (always works, no androguard needed) -----
    try:
        with _zip.ZipFile(path) as z:
            names = z.namelist()
    except _zip.BadZipFile:
        return {"error": "not a valid APK (bad ZIP signature)"}

    out["dex_files"] = [n for n in names if n.endswith(".dex")]
    out["native_libs"] = [n for n in names if n.startswith("lib/") and n.endswith(".so")]
    out["assets"] = [n for n in names if n.startswith("assets/")][:200]
    out["file_count"] = len(names)

    # --- step 2: manifest + signing via androguard ----------------------
    if _HAVE_ANDROGUARD:
        try:
            apk = _AndroidAPK(path)
            out["manifest"] = {
                "package": apk.get_package(),
                "version_name": apk.get_androidversion_name(),
                "version_code": apk.get_androidversion_code(),
                "min_sdk": apk.get_min_sdk_version(),
                "target_sdk": apk.get_target_sdk_version(),
                "main_activity": apk.get_main_activity(),
                "app_name": apk.get_app_name(),
                "is_debuggable": bool(apk.get_element("application", "debuggable") == "true"),
                "allows_backup": (apk.get_element("application", "allowBackup") != "false"),
                "uses_cleartext_traffic": (apk.get_element("application", "usesCleartextTraffic") == "true"),
            }
            perms = list(apk.get_permissions() or [])
            out["permissions"]["all"] = sorted(perms)
            out["permissions"]["dangerous"] = sorted(p for p in perms if p in _APK_DANGEROUS_PERMS)
            out["components"]["activities"] = list(apk.get_activities() or [])[:200]
            out["components"]["services"] = list(apk.get_services() or [])[:200]
            out["components"]["receivers"] = list(apk.get_receivers() or [])[:200]
            out["components"]["providers"] = list(apk.get_providers() or [])[:200]
            try:
                # Each kind has an exported flag we can pull via androguard's
                # get_element. Compile a flat exported list with kind tags.
                exported: list[dict] = []
                for kind, items in (
                    ("activity", apk.get_activities() or []),
                    ("service", apk.get_services() or []),
                    ("receiver", apk.get_receivers() or []),
                    ("provider", apk.get_providers() or []),
                ):
                    for name in items:
                        is_exp = apk.get_element(kind, "exported", name=name) == "true"
                        if is_exp:
                            exported.append({"kind": kind, "name": name})
                out["exported_components"] = exported
            except Exception as e:
                out["warnings"].append(f"exported-component scan failed: {e}")
            # Signing certs — list issuers + sha256 fingerprints, no key bytes.
            try:
                certs = apk.get_certificates() or []
                sig_v1 = apk.get_signature_names() or []
                out["signing"] = {
                    "v1_signature_files": list(sig_v1)[:10],
                    "is_signed_v1": apk.is_signed_v1() if hasattr(apk, "is_signed_v1") else None,
                    "is_signed_v2": apk.is_signed_v2() if hasattr(apk, "is_signed_v2") else None,
                    "is_signed_v3": apk.is_signed_v3() if hasattr(apk, "is_signed_v3") else None,
                    "certificates": [
                        {
                            "subject": str(getattr(c, "subject", "")),
                            "issuer": str(getattr(c, "issuer", "")),
                            "sha256": (getattr(c, "sha256_fingerprint", "") or "").lower(),
                        }
                        for c in certs[:5]
                    ],
                }
            except Exception as e:
                out["warnings"].append(f"signing parse failed: {e}")
        except Exception as e:
            out["warnings"].append(f"androguard manifest parse failed: {e}")
    else:
        out["notes"].append(
            "androguard not installed — manifest/permissions/components skipped. "
            "Run `pip install androguard` to enable full scan."
        )

    # --- step 3: secret hunt over DEX + .so + (optionally) everything ---
    targets = list(out["dex_files"]) + list(out["native_libs"])
    if deep:
        # everything text-ish or unidentified — skip giant pngs/jpgs/webps/mp4/zip-in-zip
        skip_ext = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mkv", ".webm", ".ogg", ".mp3", ".wav"}
        for n in names:
            if n in targets:
                continue
            ext = Path(n).suffix.lower()
            if ext in skip_ext:
                continue
            targets.append(n)

    bytes_scanned = 0
    findings: list[dict] = []
    try:
        with _zip.ZipFile(path) as z:
            for n in targets:
                try:
                    with z.open(n) as f:
                        chunk = f.read(max_string_bytes - bytes_scanned if max_string_bytes > bytes_scanned else 0)
                except KeyError:
                    continue
                if not chunk:
                    continue
                bytes_scanned += len(chunk)
                # Hunt over the raw blob first (catches embedded JSON, manifest fragments)
                hits = _apk_hunt_secrets(n, chunk)
                if hits:
                    findings.extend(hits)
                if len(findings) >= max_secret_findings or bytes_scanned >= max_string_bytes:
                    break
    except Exception as e:
        out["warnings"].append(f"secret hunt failed: {e}")

    # Dedupe (source, kind, match) tuples — same string in two dex files is noise
    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for f in findings:
        key = (f["kind"], f["match"])  # source-agnostic dedupe; first source wins
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(f)
    out["secret_findings"] = deduped[:max_secret_findings]
    out["secret_findings_total"] = len(findings)
    out["bytes_scanned"] = bytes_scanned
    out["scan_truncated"] = bytes_scanned >= max_string_bytes

    # --- step 4: surface a quick risk summary the model can lead with ---
    risk: list[str] = []
    m = out["manifest"]
    if m.get("is_debuggable"):
        risk.append("debuggable=true (production APKs should never ship this)")
    if m.get("allows_backup"):
        risk.append("allowBackup not disabled (data extractable via adb backup)")
    if m.get("uses_cleartext_traffic"):
        risk.append("usesCleartextTraffic=true (HTTP plaintext permitted)")
    if out["permissions"]["dangerous"]:
        risk.append(f"{len(out['permissions']['dangerous'])} dangerous permissions requested")
    if out["exported_components"]:
        risk.append(f"{len(out['exported_components'])} exported components (check intent filters)")
    secret_kinds = {f["kind"] for f in out["secret_findings"]
                    if f["kind"] not in {"hardcoded_http_endpoint", "ipv4_literal"}}
    if secret_kinds:
        risk.append(f"possible secrets: {', '.join(sorted(secret_kinds))}")
    out["risk_summary"] = risk

    return out
