# SPDX-License-Identifier: AGPL-3.0-only
"""Read dependency manifests (requirements.txt, package.json, pyproject, Cargo, go.mod, ...).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _parse_dependency_manifest(target: Path, ecosystem_hint: str = "") -> tuple[str, list[dict], list[str]]:
    """Return ecosystem, exact resolved packages, and unresolved declarations.

    OSV version queries require an exact version; treating ``^1.2`` or ``>=1.2``
    as if 1.2 were installed gives false positives and negatives, so ranges are
    reported as unresolved and never queried."""
    import tomllib
    name = target.name.lower()
    content = target.read_text(encoding="utf-8")
    packages: list[dict] = []
    unresolved: list[str] = []
    ecosystem = ecosystem_hint or ""

    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        ecosystem = "npm"
        data = json.loads(content)
        for key, meta in (data.get("packages") or {}).items():
            if not key or "node_modules/" not in key or not isinstance(meta, dict):
                continue
            pkg_name = meta.get("name") or key.rsplit("node_modules/", 1)[-1]
            version = str(meta.get("version") or "").strip()
            if pkg_name and version:
                packages.append({"name": pkg_name, "version": version})
        if not packages:  # package-lock v1
            def walk(deps: dict):
                for pkg_name, meta in (deps or {}).items():
                    if not isinstance(meta, dict):
                        continue
                    version = str(meta.get("version") or "").strip()
                    if version:
                        packages.append({"name": pkg_name, "version": version})
                    walk(meta.get("dependencies") or {})
            walk(data.get("dependencies") or {})
    elif name == "pnpm-lock.yaml":
        ecosystem = "npm"
        # Dependency snapshot keys contain the resolved version even when the
        # importer declaration is a range. Supports pnpm lockfile v6-v9 shapes.
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith(("lockfileVersion", "settings", "importers", "packages:", "snapshots:")):
                continue
            key_match = re.match(r"^['\"]?(.+?)['\"]?\s*:\s*(?:\{\})?\s*$", line)
            if not key_match:
                continue
            key = key_match.group(1).split("(", 1)[0].lstrip("/")
            match = re.match(r"(?P<pkg>@[^/]+/[^@]+|[^@/][^@]*)@(?P<ver>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$", key)
            if match:
                packages.append({"name": match.group("pkg"), "version": match.group("ver")})
    elif name == "yarn.lock":
        ecosystem = "npm"
        current_pkg = ""
        for raw in content.splitlines():
            if raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
                selector = raw.rstrip()[:-1].split(",", 1)[0].strip().strip("'\"")
                if selector.startswith("@"):
                    at = selector.find("@", 1)
                else:
                    at = selector.find("@")
                current_pkg = selector[:at] if at > 0 else ""
                continue
            m = re.match(r"\s+(?:version\s+['\"]([^'\"]+)['\"]|resolution:\s*['\"](?:@?[^'\"]+@)?npm:([^'\"]+)['\"])", raw)
            version = (m.group(1) or m.group(2)) if m else ""
            if current_pkg and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version or ""):
                packages.append({"name": current_pkg, "version": version})
    elif name == "package.json":
        ecosystem = "npm"
        data = json.loads(content)
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for pkg_name, raw in deps.items():
            version = str(raw).strip()
            if re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
                packages.append({"name": pkg_name, "version": version.lstrip("v")})
            else:
                unresolved.append(f"{pkg_name}: {version}")
    elif name in ("pipfile.lock",):
        ecosystem = "PyPI"
        data = json.loads(content)
        for section in ("default", "develop"):
            for pkg_name, meta in (data.get(section) or {}).items():
                raw = meta.get("version") if isinstance(meta, dict) else meta
                version = str(raw or "").strip()
                if version.startswith("=="):
                    packages.append({"name": pkg_name, "version": version[2:]})
                else:
                    unresolved.append(f"{pkg_name}: {version or 'unresolved'}")
    elif name in ("poetry.lock", "uv.lock"):
        ecosystem = "PyPI"
        data = tomllib.loads(content)
        for meta in data.get("package") or []:
            if isinstance(meta, dict) and meta.get("name") and meta.get("version"):
                packages.append({"name": str(meta["name"]), "version": str(meta["version"])})
    elif name == "requirements.txt" or ecosystem_hint.lower() == "pypi":
        ecosystem = "PyPI"
        for raw in content.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith(("-r", "--", "git+", "http:" , "https:")):
                continue
            match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*={2,3}\s*([^\s;]+)", line)
            if match:
                packages.append({"name": match.group(1), "version": match.group(2)})
            else:
                unresolved.append(line[:160])
    elif name == "go.mod" or ecosystem_hint.lower() == "go":
        ecosystem = "Go"
        for match in re.finditer(r"(?:^|\n)\s*(?:require\s+)?([A-Za-z0-9_.\-/]+)\s+v([^\s)]+)", content):
            # go.mod is a minimum-version declaration, not necessarily the
            # selected build list after MVS. Never present it as installed fact.
            unresolved.append(f"{match.group(1)}: v{match.group(2)} (go.mod declaration; run `go list -m all` for resolved versions)")
    elif name == "cargo.lock":
        ecosystem = "crates.io"
        data = tomllib.loads(content)
        for meta in data.get("package") or []:
            if isinstance(meta, dict) and meta.get("name") and meta.get("version"):
                packages.append({"name": str(meta["name"]), "version": str(meta["version"])})
    elif name == "cargo.toml" or ecosystem_hint.lower() == "crates.io":
        ecosystem = "crates.io"
        data = tomllib.loads(content)
        for section, deps in data.items():
            if "dependencies" not in str(section).lower() or not isinstance(deps, dict):
                continue
            for pkg_name, raw in deps.items():
                version = str(raw.get("version") or "") if isinstance(raw, dict) else str(raw)
                if re.fullmatch(r"=?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
                    packages.append({"name": pkg_name, "version": version.lstrip("=")})
                else:
                    unresolved.append(f"{pkg_name}: {version or 'path/git dependency'}")
    elif name == "composer.lock":
        ecosystem = "Packagist"
        data = json.loads(content)
        for section in ("packages", "packages-dev"):
            for meta in data.get(section) or []:
                if not isinstance(meta, dict):
                    continue
                pkg_name = str(meta.get("name") or "").strip()
                version = str(meta.get("version") or "").strip().lstrip("v")
                if pkg_name and version:
                    packages.append({"name": pkg_name, "version": version})
    elif name == "packages.lock.json":
        ecosystem = "NuGet"
        data = json.loads(content)
        for framework in (data.get("dependencies") or {}).values():
            if not isinstance(framework, dict):
                continue
            for pkg_name, meta in framework.items():
                version = str((meta or {}).get("resolved") or "") if isinstance(meta, dict) else ""
                if pkg_name and version:
                    packages.append({"name": pkg_name, "version": version})
    elif name == "gradle.lockfile":
        ecosystem = "Maven"
        for raw in content.splitlines():
            line = raw.split("#", 1)[0].strip()
            match = re.match(r"([^:=\s]+):([^:=\s]+):([^=\s]+)=", line)
            if match:
                packages.append({"name": f"{match.group(1)}:{match.group(2)}",
                                 "version": match.group(3)})
    else:
        raise ValueError(f"Unsupported manifest type: {name}")

    unique = {(p["name"], p["version"]): p for p in packages}
    return ecosystem, list(unique.values()), list(dict.fromkeys(unresolved))
