# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team mission playbook: which step comes next for an engagement.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


# One concrete next move appended to a tool's result, so the model does not
# have to work out what the output is for. Keyed by tool; `pentest` says
# whether active testing is part of this engagement.
def _rt_next_step(name: str, result: dict, pentest: bool, engagement: str = "") -> str:
    if result.get("error") or result.get("scope_blocked") or result.get("rate_limited"):
        return ""
    if engagement == "access_control":
        # The two-account comparison has its own route; the injection-oriented
        # hints below would send it off-brief.
        if name == "recon_http_fingerprint":
            return "Run recon_map on this URL with identity 'A' to list what a signed-in account can reach."
        if name in ("recon_map", "recon_content_discovery"):
            return ("Run rt_access_check on each discovered URL that names one of account A's own "
                    "objects (an id in the path or query). Use real URLs, not {id} templates.")
        if name != "record_finding":
            return ""
    if engagement == "bug_bounty":
        # The pentest hints push toward fan-out probing and data extraction,
        # which bug bounty programs forbid; this route stays on minimal proof.
        if name == "recon_http_fingerprint":
            return ("List the app's pages, endpoints and parameters: recon_map if automated tools "
                    "are allowed, otherwise open the main pages with rt_browser or http_request.")
        if name in ("recon_map", "recon_content_discovery"):
            return ("Pick the endpoints that take input or name an object, and test them by hand "
                    "for the bug types the program pays for.")
        if name in ("recon_injection_probe", "batch_probe", "fuzz", "cors_probe", "scan_js_secrets"):
            return ("A hit is a candidate only. Reproduce it with http_request using the smallest "
                    "proof the program rules allow, send it a second time, send one harmless "
                    "control request, then record_finding with status validated and a cvss_vector.")
        if name == "record_finding" and not result.get("downgraded"):
            return "Continue with the next endpoint, or build the report with rt_generate_report when testing is done."
    if name in ("recon_subdomains", "recon_dns", "recon_web_archive"):
        return "Run recon_http_fingerprint on each in-scope host you found."
    if name in ("recon_port_scan", "sandbox_nmap"):
        return ("Run recon_http_fingerprint on each open web port and recon_open_services "
                "on the host for databases and caches.")
    if name == "recon_http_fingerprint":
        return "Run recon_map on this URL to list its pages, endpoints and parameters."
    if name == "recon_map":
        if pentest:
            return ("Pass the URLs that carry query parameters to batch_probe "
                    "(probe: sqli, then xss, ssti, lfi), and run recon_check_exposure on the base URL.")
        return "Run recon_check_exposure and scan_js_secrets on the base URL."
    if name == "recon_content_discovery":
        return "Run recon_check_exposure on the base URL, then record each real exposure with record_finding."
    if name in ("batch_probe", "recon_injection_probe", "sql_injection", "fuzz", "sandbox_sqlmap"):
        if not pentest:
            return "A hit is a candidate only. Record it with record_finding (status candidate)."
        return ("A hit is a candidate only. Reproduce it by hand with http_request, send it a second "
                "time, send one harmless control request, then record_finding with status validated. "
                "No hits: move to the next step.")
    if name in ("recon_check_exposure", "scan_js_secrets", "recon_cve_match", "recon_shodan",
                "recon_subdomain_takeover", "recon_tls_audit", "recon_open_services", "cors_probe"):
        return "Record each real issue with record_finding (status candidate), then continue with the next step."
    if name == "record_finding":
        if result.get("downgraded"):
            return ("Stored as a candidate. To validate it, call record_finding again with: "
                    "evidence_ids (the receipt ids of the two requests that worked), control_id "
                    "(the receipt id of the harmless request) and proof (exact text that is in "
                    "both working responses and not in the control).")
        return "Continue with the next step, or build the report with rt_generate_report when testing is done."
    return ""
