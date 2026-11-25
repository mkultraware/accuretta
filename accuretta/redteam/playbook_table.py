# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team playbooks and the tools each one uses.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


# ---- mission playbook --------------------------------------------------------
# Rules tell a model what not to do; a small model also needs the order of
# work. Each mode gets a short numbered route naming the tool for each step.
# The bridge tracks which steps have been started from the tools actually
# called, so the block always shows where the run is and what comes next.
_RT_PLAYBOOKS: dict[str, list[dict]] = {
    "passive_osint": [
        {"do": "Who owns it", "tools": ("recon_rdap", "recon_dns")},
        {"do": "What hosts exist", "tools": ("recon_subdomains", "recon_web_archive")},
        {"do": "What is exposed, by public record", "tools": ("recon_shodan", "recon_cve_match")},
        {"do": "Write down each issue as a candidate", "tools": ("record_finding",)},
        {"do": "Answer the user, naming the public source behind each claim", "tools": ()},
    ],
    "recon": [
        {"do": "Find the hosts and open ports", "tools": ("recon_subdomains", "recon_dns", "recon_port_scan")},
        {"do": "Identify each web service", "tools": ("recon_http_fingerprint", "recon_tls_audit")},
        {"do": "List pages, endpoints and parameters", "tools": ("recon_map", "recon_content_discovery")},
        {"do": "Check for leaks and known-vulnerable versions",
         "tools": ("recon_check_exposure", "scan_js_secrets", "recon_cve_match")},
        {"do": "Write down each issue as a candidate", "tools": ("record_finding",)},
        {"do": "Build the report", "tools": ("rt_generate_report",)},
    ],
    "pentest": [
        {"do": "Identify the service, then list every endpoint and parameter",
         "tools": ("recon_http_fingerprint", "recon_map")},
        {"do": "Sweep the listed URLs for easy wins",
         "tools": ("batch_probe", "recon_check_exposure", "scan_js_secrets")},
        {"do": "Work each hit by hand until it is real or dead",
         "tools": ("http_request", "sql_injection", "fuzz", "jwt_tool", "rt_browser")},
        {"do": "Confirm: send the working request a second time, send one harmless control "
               "request, then record_finding with status validated and those receipt ids",
         "tools": ("record_finding",)},
        {"do": "Build the report", "tools": ("rt_generate_report",)},
    ],
    "access_control": [
        {"do": "List the pages and API endpoints while signed in as account A (pass identity A)",
         "tools": ("recon_map",)},
        {"do": "Send each URL that names one of account A's own objects as every account",
         "tools": ("rt_access_check",)},
        {"do": "Confirm a candidate: run the same rt_access_check a second time, then "
               "record_finding with status validated and the receipt ids it gives you",
         "tools": ("record_finding",)},
        {"do": "Build the report", "tools": ("rt_generate_report",)},
    ],
    "bug_bounty": [
        {"do": "Identify the in-scope web app, then list its pages, endpoints and parameters",
         "tools": ("recon_http_fingerprint", "recon_map", "rt_browser")},
        {"do": "Look for the bug types the program pays for, by hand and at a gentle pace",
         "tools": ("http_request", "recon_injection_probe", "cors_probe", "scan_js_secrets")},
        {"do": "Prove each one with the smallest proof the program allows: send it twice, send one "
               "harmless control request, then record_finding with status validated, those receipt "
               "ids and a cvss_vector scored on what the proof shows",
         "tools": ("record_finding",)},
        {"do": "Build the report: one per bug, even when it appears on several sites",
         "tools": ("rt_generate_report",)},
    ],
}


_RT_PLAYBOOK_TOOLS = frozenset(t for steps in _RT_PLAYBOOKS.values() for s in steps for t in s["tools"])
