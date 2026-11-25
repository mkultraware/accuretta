# SPDX-License-Identifier: AGPL-3.0-only
"""The check_deps tool: reading a project's dependency manifests.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.workspace import normalize_path
from accuretta.security.path_guards import is_in_workspace
from accuretta.tools.deps import _parse_dependency_manifest
from pathlib import Path


# --- Tool 5: check_deps ---
_DEPENDENCY_MANIFEST_PRIORITY = (
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "Pipfile.lock", "poetry.lock", "uv.lock", "Cargo.lock", "composer.lock",
    "packages.lock.json", "gradle.lockfile", "requirements.txt", "go.mod",
    "package.json", "Cargo.toml",
)


def tool_check_deps(args: dict) -> dict:
    """Check exact, resolved dependency versions against OSV.dev."""
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    target = Path(normalize_path(raw_path))
    if not target.exists():
        return {"error": "path not found"}
    if target.is_dir():
        target = next((target / name for name in _DEPENDENCY_MANIFEST_PRIORITY
                       if (target / name).exists()), target)
        if target.is_dir():
            return {"error": f"No supported dependency manifest found in {target}"}
    if not is_in_workspace(str(target)):
        return {"error": "Path is outside workspace bounds."}
    manifest_name = target.name.lower()
    try:
        ecosystem, packages, unresolved = _parse_dependency_manifest(
            target, str(args.get("ecosystem") or ""))
    except Exception as e:
        return {"error": f"Failed parsing manifest: {e}"}
    if not packages:
        return {
            "error": "No exact resolved package versions found; use a lockfile or pin versions before querying OSV.",
            "path": str(target),
            "unresolved_declarations": unresolved[:200],
        }

    # Query OSV.dev batch API
    queries = []
    for p in packages:
        queries.append({
            "version": p["version"],
            "package": {"name": p["name"], "ecosystem": ecosystem}
        })
        
    try:
        req = urllib.request.Request(
            "https://api.osv.dev/v1/querybatch",
            data=json.dumps({"queries": queries}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"OSV.dev API request failed: {e}"}
        
    results = data.get("results", [])
    vulnerable_packages = []
    clean_packages = []
    vulnerable_count = 0
    ignored_vulns = set(args.get("ignore", []))
    
    for i, res in enumerate(results):
        pkg = packages[i]
        vulns = res.get("vulns", [])
        
        filtered_vulns = []
        for v in vulns:
            if v.get("id") not in ignored_vulns:
                filtered_vulns.append({
                    "id": v.get("id"),
                    "summary": v.get("summary", "No summary"),
                    "url": f"https://osv.dev/vulnerability/{v.get('id')}"
                })
                
        if filtered_vulns:
            vulnerable_count += 1
            vulnerable_packages.append({
                "name": pkg["name"],
                "version": pkg["version"],
                "vulns": filtered_vulns
            })
        else:
            clean_packages.append(pkg["name"])

    return {
        "path": str(target),
        "ecosystem": ecosystem,
        "manifest_type": manifest_name,
        "packages_checked": len(packages),
        "vulnerable_count": vulnerable_count,
        "packages": vulnerable_packages,
        "clean_packages": clean_packages,
        "unresolved_declarations": unresolved[:200],
        "evidence_quality": ("lockfile/exact versions" if not unresolved else
                             "exact versions checked; ranges/unresolved declarations were not guessed"),
        "risk_summary": (f"Found {vulnerable_count} vulnerable packages out of {len(packages)} "
                         f"exact versions checked; {len(unresolved)} unresolved declaration(s) skipped.")
    }
