# SPDX-License-Identifier: AGPL-3.0-only
"""The parse_evtx tool: reading Windows event log files.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import datetime
import json
from accuretta.core.optional_imports import _Evtx
from accuretta.core.optional_imports import _HAVE_EVTX_PY
from accuretta.core.optional_imports import _HAVE_EVTX_RUST
from accuretta.core.optional_imports import _PyEvtxParser
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import request_approval
from pathlib import Path


# --- Tool 2: parse_evtx ---
_EVTX_EVENT_LABELS = {
    1102: "Audit log cleared",
    4624: "Logon success",
    4625: "Logon failure",
    4634: "Logoff",
    4648: "Explicit credential logon",
    4672: "Special privileges assigned",
    4688: "Process created",
    4689: "Process exited",
    4697: "Service installed",
    4698: "Scheduled task created",
    4699: "Scheduled task deleted",
    4700: "Scheduled task enabled",
    4702: "Scheduled task updated",
    4720: "User account created",
    4722: "User account enabled",
    4725: "User account disabled",
    4726: "User account deleted",
    4728: "Member added to security group",
    4732: "Member added to local group",
    4738: "User account changed",
    4740: "Account locked out",
    4756: "Member added to universal group",
    4776: "Credential validation",
    5140: "Network share accessed",
    5156: "Windows Filtering Platform connection",
    7034: "Service crashed",
    7036: "Service state changed",
    7040: "Service start type changed",
    7045: "Service installed (System log)",
    4104: "PowerShell ScriptBlock",
}


def tool_parse_evtx(args: dict) -> dict:
    """Parse Windows .evtx event log files."""
    if not _HAVE_EVTX_RUST and not _HAVE_EVTX_PY:
        return {"error": "evtx parser not available. install with: pip install evtx"}
        
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists() or not target.is_file():
        return {"error": "file not found"}
        
    event_ids = set(args.get("event_ids", []))
    limit = min(args.get("limit", 500), 5000)
    keywords = (args.get("keywords") or "").lower()
    since_str = args.get("since")
    since_dt = None
    if since_str:
        try:
            since_dt = datetime.datetime.fromisoformat(since_str.replace('Z', '+00:00'))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass

    aid = request_approval(
        "Parse event log", f"Read {target}",
        {"kind": "parse_event_log", "target": str(target)})
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    events = []
    total_records = 0
    filtered_records = 0
    parser_used = "evtx (rust)" if _HAVE_EVTX_RUST else "python-evtx"
    
    try:
        if _HAVE_EVTX_RUST:
            parser = _PyEvtxParser(str(target))
            for record in parser.records_json():
                total_records += 1
                try:
                    event = json.loads(record["data"])
                    sys_node = event.get("Event", {}).get("System", {})
                    eid = sys_node.get("EventID", 0)
                    if isinstance(eid, dict):
                        eid = eid.get("#text", 0)
                    eid = int(eid)
                    
                    if event_ids and eid not in event_ids:
                        continue
                        
                    time_created_node = sys_node.get("TimeCreated", {})
                    time_str = time_created_node.get("#attributes", {}).get("SystemTime") or time_created_node.get("SystemTime")
                    
                    if since_dt and time_str:
                        try:
                            evt_dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                            if evt_dt.tzinfo is None:
                                evt_dt = evt_dt.replace(tzinfo=datetime.timezone.utc)
                            if evt_dt < since_dt:
                                continue
                        except Exception:
                            pass
                            
                    raw_data = json.dumps(event).lower()
                    if keywords and keywords not in raw_data:
                        continue
                        
                    event_data = event.get("Event", {}).get("EventData", {})
                    
                    filtered_records += 1
                    if len(events) < limit:
                        events.append({
                            "record_id": sys_node.get("EventRecordID"),
                            "event_id": eid,
                            "label": _EVTX_EVENT_LABELS.get(eid, "Unknown"),
                            "timestamp": time_str,
                            "computer": sys_node.get("Computer"),
                            "channel": sys_node.get("Channel"),
                            "provider": sys_node.get("Provider", {}).get("#attributes", {}).get("Name") or sys_node.get("Provider", {}).get("Name"),
                            "data": event_data
                        })
                except Exception:
                    continue
        else:
            with _Evtx(str(target)) as log:
                for record in log.records():
                    total_records += 1
                    try:
                        node = record.lxml()
                        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                        
                        eid_node = node.find('.//e:System/e:EventID', namespaces=ns)
                        if eid_node is None:
                            continue
                        eid = int(eid_node.text)
                        
                        if event_ids and eid not in event_ids:
                            continue
                            
                        time_node = node.find('.//e:System/e:TimeCreated', namespaces=ns)
                        time_str = time_node.get('SystemTime') if time_node is not None else None
                        
                        if since_dt and time_str:
                            try:
                                evt_dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                                if evt_dt.tzinfo is None:
                                    evt_dt = evt_dt.replace(tzinfo=datetime.timezone.utc)
                                if evt_dt < since_dt:
                                    continue
                            except Exception:
                                pass
                                
                        raw_data = record.xml().lower()
                        if keywords and keywords not in raw_data:
                            continue
                            
                        event_data = {}
                        for data in node.findall('.//e:EventData/e:Data', namespaces=ns) + node.findall('.//e:UserData/e:Data', namespaces=ns):
                            event_data[data.get('Name') or 'Value'] = data.text
                            
                        filtered_records += 1
                        if len(events) < limit:
                            computer_node = node.find('.//e:System/e:Computer', namespaces=ns)
                            channel_node = node.find('.//e:System/e:Channel', namespaces=ns)
                            provider_node = node.find('.//e:System/e:Provider', namespaces=ns)
                            record_id_node = node.find('.//e:System/e:EventRecordID', namespaces=ns)
                            
                            events.append({
                                "record_id": record_id_node.text if record_id_node is not None else None,
                                "event_id": eid,
                                "label": _EVTX_EVENT_LABELS.get(eid, "Unknown"),
                                "timestamp": time_str,
                                "computer": computer_node.text if computer_node is not None else None,
                                "channel": channel_node.text if channel_node is not None else None,
                                "provider": provider_node.get('Name') if provider_node is not None else None,
                                "data": event_data
                            })
                    except Exception:
                        continue
    except Exception as e:
        return {"error": f"parsing failed: {e}"}

    event_counts = {}
    flagged = []
    high_interest_ids = {1102, 4697, 7045, 4720, 4726, 4104}
    
    for e in events:
        eid = e["event_id"]
        event_counts[eid] = event_counts.get(eid, 0) + 1
        if eid in high_interest_ids:
            flagged.append(e)

    return {
        "path": str(target),
        "parser": parser_used,
        "total_records": total_records,
        "filtered_records": filtered_records,
        "events": events,
        "summary": {
            "event_id_counts": event_counts,
            "security_highlights": flagged
        }
    }
