# SPDX-License-Identifier: AGPL-3.0-only
"""The scan_secrets tool: secrets committed to files in the workspace, never Accuretta's
own data folder.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import request_approval
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _data_file_in_walk
from accuretta.security.path_guards import _is_data_dir
from accuretta.security.path_guards import is_blocked_path
from accuretta.tools.secret_hunt import _hunt_secrets_in_file
from pathlib import Path


def tool_scan_secrets(args: dict) -> dict:
    """Scan a file or directory for hardcoded secrets."""
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists():
        return {"error": "path not found"}
    if is_blocked_path(str(target)):
        return {"error": "path blocked"}
    if is_data_path(str(target)):
        return {"error": DATA_PATH_ERROR}
        
    exts = args.get("extensions")
    if not exts:
        exts = [
            ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", 
            ".toml", ".env", ".cfg", ".ini", ".xml", ".properties", ".tf", 
            ".sh", ".bat", ".ps1", ".go", ".rs", ".java", ".rb", ".php", 
            ".cs", ".config", ".sql", ".dockerfile", ".tfvars"
        ]
    exts = set(e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts)
    
    max_files = args.get("max_files", 500)
    max_bytes = args.get("max_file_bytes", 2 * 1024 * 1024)
    include_http = args.get("include_http_endpoints", False)
    
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build", ".next", ".nuxt"}
    
    aid = request_approval(
        "Secret scan",
        f"Scan {target} for hardcoded secrets",
        {"kind": "secret_scan", "target": str(target), "extensions": list(exts)}
    )
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    files_to_scan = []
    if target.is_file():
        files_to_scan.append(target)
    else:
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in skip_dirs
                       and not _is_data_dir(os.path.join(root, d))]
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in exts and not _data_file_in_walk(str(p)):
                    files_to_scan.append(p)
            if len(files_to_scan) >= max_files:
                break
                
    files_to_scan = files_to_scan[:max_files]
    
    findings = []
    files_with_findings = 0
    scanned = 0
    
    for p in files_to_scan:
        try:
            if p.stat().st_size > max_bytes:
                continue
            scanned += 1
            file_findings = _hunt_secrets_in_file(p, include_http=include_http)
            if file_findings:
                files_with_findings += 1
                for f in file_findings:
                    f["file"] = str(p.relative_to(target)) if target.is_dir() else p.name
                findings.extend(file_findings)
        except Exception:
            continue
            
    risk_counts = {}
    for f in findings:
        risk_counts[f["kind"]] = risk_counts.get(f["kind"], 0) + 1
        
    return {
        "path": str(target),
        "files_scanned": scanned,
        "files_with_findings": files_with_findings,
        "findings": findings,
        "risk_summary": {
            "total_secrets_found": len(findings),
            "breakdown": risk_counts
        }
    }
