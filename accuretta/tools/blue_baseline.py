# SPDX-License-Identifier: AGPL-3.0-only
"""Blue-team network baselines: a stable signature of the network snapshot, saved and
compared per baseline.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import time
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from typing import Any


_BLUE_BASELINE_DIR = DATA / "blue_team_baselines"


def _blue_baseline_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")[:64]


def _network_snapshot_signature(snapshot: dict) -> dict:
    """Stable, compact state used for host drift comparison."""
    tcp = set()
    for row in snapshot.get("tcp_connections") or []:
        if not isinstance(row, dict):
            continue
        tcp.add("|".join(str(row.get(k) or "") for k in
                         ("process", "LocalAddress", "LocalPort", "RemoteAddress",
                          "RemotePort", "State")))
    udp = set()
    for row in snapshot.get("udp_listeners") or []:
        if not isinstance(row, dict):
            continue
        udp.add("|".join(str(row.get(k) or "") for k in
                         ("process", "LocalAddress", "LocalPort")))
    dns = set()
    for row in snapshot.get("recent_dns") or []:
        if isinstance(row, dict):
            # PowerShell field names vary by Windows release. Canonical JSON
            # remains deterministic and is capped before persistence.
            dns.add(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)[:500])
        elif row not in (None, ""):
            dns.add(str(row)[:500])
    processes = {str(row.get("process") or "") for row in (snapshot.get("process_details") or [])
                 if isinstance(row, dict) and row.get("process")}
    return {"captured": int(time.time()), "tcp": sorted(tcp), "udp": sorted(udp),
            "dns": sorted(dns), "processes": sorted(processes)}


def _network_baseline_apply(snapshot: dict, args: dict) -> None:
    """Optionally save/compare a host snapshot without polluting model prose."""
    current = _network_snapshot_signature(snapshot)
    compare_id = _blue_baseline_id(args.get("compare_baseline"))
    if compare_id:
        path = _BLUE_BASELINE_DIR / f"{compare_id}.json"
        old = load_json(path, None)
        if not isinstance(old, dict):
            snapshot["comparison_error"] = f"baseline not found: {compare_id}"
        else:
            diff = {"baseline_id": compare_id, "baseline_captured": old.get("captured")}
            changed = False
            for key in ("tcp", "udp", "dns", "processes"):
                old_set = set(old.get(key) or [])
                now_set = set(current.get(key) or [])
                added = sorted(now_set - old_set)
                removed = sorted(old_set - now_set)
                diff[f"added_{key}"] = added[:100]
                diff[f"removed_{key}"] = removed[:100]
                diff[f"{key}_delta_count"] = len(now_set) - len(old_set)
                changed = changed or bool(added or removed)
            diff["changed"] = changed
            snapshot["comparison"] = diff
    save_id = _blue_baseline_id(args.get("save_baseline"))
    if save_id:
        _BLUE_BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        save_json(_BLUE_BASELINE_DIR / f"{save_id}.json", current)
        snapshot["baseline_saved"] = {"id": save_id, "captured": current["captured"]}
