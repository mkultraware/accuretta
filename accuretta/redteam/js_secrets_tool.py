# SPDX-License-Identifier: AGPL-3.0-only
"""The scan_js_secrets tool: secrets in a target's frontend code, reported per source
and remembered so the credential guard can recognize them later.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import contextvars
import threading
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.settings import get_settings
from accuretta.redteam.frontend_endpoints import _extract_frontend_endpoints
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.network_scope import _rt_scope_block
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.recon_map import _frontend_resource_urls
from accuretta.redteam.source_maps import _reconstruct_source_maps
from accuretta.redteam.frontend_secrets import _scan_frontend_source
from concurrent.futures import ThreadPoolExecutor


_RT_DISCOVERED_SECRET_LOCK = threading.Lock()


_RT_DISCOVERED_SECRET_VALUES: dict[str, tuple[str, ...]] = {}


def _rt_secret_guard_id(chat: dict | None) -> str:
    mission = _rt_mission_get(chat) if isinstance(chat, dict) else None
    if not mission:
        return ""
    return str(mission.get("authorization_id") or chat.get("id") or "").strip()


def _rt_remember_frontend_secret_values(chat: dict | None, records: list[dict]) -> int:
    """Keep discovered values in process memory so request tools cannot reuse them."""
    guard_id = _rt_secret_guard_id(chat)
    if not guard_id:
        return 0
    values = {
        str(record.get("_value") or "")
        for record in records
        if isinstance(record, dict) and isinstance(record.get("_value"), str)
        and record.get("_value")
    }
    if not values:
        return 0
    with _RT_DISCOVERED_SECRET_LOCK:
        existing = set(_RT_DISCOVERED_SECRET_VALUES.get(guard_id, ()))
        existing.update(values)
        _RT_DISCOVERED_SECRET_VALUES[guard_id] = tuple(sorted(existing, key=len, reverse=True)[:250])
        while len(_RT_DISCOVERED_SECRET_VALUES) > 128:
            _RT_DISCOVERED_SECRET_VALUES.pop(next(iter(_RT_DISCOVERED_SECRET_VALUES)))
        return len(_RT_DISCOVERED_SECRET_VALUES[guard_id])


def _frontend_secret_record_for_report(record: dict) -> dict:
    reported = {key: value for key, value in record.items() if key != "_value"}
    raw_value = record.get("_value")
    if isinstance(raw_value, str) and raw_value:
        reported["value"] = raw_value
    return reported


def tool_scan_js_secrets(args: dict) -> dict:
    """Passive frontend audit (the Frontend Secrets engagement): over the page,
    its linked/lazy JS, and source maps it fetches once, it audits for secrets,
    maps referenced endpoints/API routes as attack surface (never calling them),
    and reconstructs original sources from any source maps. Shared by Red Team
    and its standalone UI. Read-only — no endpoint is probed, no credential
    submitted."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 10.0), 25.0))
    max_files = max(1, min(int(args.get("max_files") or 60), 200))
    max_depth = max(0, min(int(args.get("crawl_depth") or 2), 4))
    max_bytes = max(128000, min(int(args.get("max_bytes_per_file") or 1500000), 4000000))
    include_candidates = bool(args.get("include_candidates"))
    include_endpoints = args.get("include_endpoints", True) is not False
    reconstruct_sources = args.get("reconstruct_sources", True) is not False
    max_reconstructed_bytes = max(0, min(int(args.get("max_reconstructed_bytes") or 40000), 200000))

    first = _recon_fetch(url, timeout=timeout, max_bytes=max_bytes)
    if "error" in first:
        return {"error": f"fetch failed: {first['error']}", "url": url}

    sources: list[tuple[str, str, str]] = [(url, "html", first.get("body", "") or "")]
    queue = _frontend_resource_urls(url, sources[0][2], 0)
    queued = {item[0] for item in queue}
    visited = {url}
    skipped_out_of_scope: list[str] = []
    fetch_errors: list[dict] = []
    bytes_scanned = len(sources[0][2].encode("utf-8", "replace"))

    def fetch_resource(item: tuple[str, str, int]) -> tuple[str, str, int, dict | None, str]:
        resource_url, kind, depth = item
        block = _rt_scope_block("scan_js_secrets", {"url": resource_url})
        if block:
            return resource_url, kind, depth, None, block
        response = _recon_fetch(resource_url, timeout=timeout, max_bytes=max_bytes)
        return resource_url, kind, depth, response, ""

    # Fetch each discovery wave concurrently but keep the breadth/depth caps
    # deterministic; a webpack build can expose dozens of chunks, and serial
    # 10-second timeouts on a dead CDN would add up.
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="js-secret-audit") as executor:
        while queue and len(sources) - 1 < max_files:
            remaining = max_files - (len(sources) - 1)
            batch: list[tuple[str, str, int]] = []
            while queue and len(batch) < min(6, remaining):
                item = queue.pop(0)
                if item[0] in visited or item[2] > max_depth:
                    continue
                visited.add(item[0])
                batch.append(item)
            if not batch:
                continue
            futures = [executor.submit(contextvars.copy_context().run, fetch_resource, item)
                       for item in batch]
            for item, future in zip(batch, futures):
                try:
                    resource_url, kind, depth, response, block = future.result()
                except Exception as exc:
                    fetch_errors.append({"url": item[0],
                                         "error": f"{type(exc).__name__}: {exc}"[:180]})
                    continue
                if block:
                    skipped_out_of_scope.append(resource_url)
                    continue
                if not response or response.get("error"):
                    error = str((response or {}).get("error") or "empty response")[:180]
                    fetch_errors.append({"url": resource_url, "error": error})
                    continue
                body = response.get("body", "") or ""
                if not body:
                    continue
                sources.append((resource_url, kind, body))
                bytes_scanned += len(body.encode("utf-8", "replace"))
                if depth < max_depth:
                    for child in _frontend_resource_urls(resource_url, body, depth):
                        if child[0] not in visited and child[0] not in queued:
                            queue.append(child)
                            queued.add(child[0])

    findings: list[dict] = []
    candidates: list[dict] = []
    public_ids: list[dict] = []
    seen_values: set[str] = set()
    for source_url, kind, text in sources:
        source_findings, source_candidates, source_public = _scan_frontend_source(source_url, text)
        for collection, records in (
            (findings, source_findings), (candidates, source_candidates), (public_ids, source_public)):
            for record in records:
                identity = str(record.get("fingerprint") or record.get("preview"))
                if identity not in seen_values:
                    seen_values.add(identity)
                    record["resource_type"] = kind
                    collection.append(record)

    protected_value_count = _rt_remember_frontend_secret_values(
        _rt_context_chat(), findings + candidates + public_ids)

    # Passive surface extraction over the SAME already-fetched bytes — no new
    # requests. Reconstructed source is itself frontend code, so it is mined
    # for endpoints too; secrets inside a .map are already caught above because
    # its raw text was scanned as a source.
    target_host = urllib.parse.urlparse(url).netloc.split("@")[-1].split(":")[0].lower()
    endpoints: list[dict] = []
    reconstruction: dict | None = None
    if include_endpoints:
        ep_seen: set = set()
        for source_url, kind, text in sources:
            if kind == "source_map":
                continue                 # JSON; handled via reconstruction below
            _extract_frontend_endpoints(source_url, text, target_host, ep_seen, endpoints)
    source_map_sources = [(u, t) for (u, k, t) in sources if k == "source_map"]
    if reconstruct_sources and source_map_sources:
        reconstruction = _reconstruct_source_maps(source_map_sources, max_reconstructed_bytes)
        if include_endpoints:
            for item in reconstruction.get("contents", []):
                _extract_frontend_endpoints(item["path"], item["content"],
                                            target_host, ep_seen, endpoints)
    endpoint_hosts = set()
    for e in endpoints:
        if e["type"] in ("absolute_url", "websocket") and e.get("scope") != "same_origin":
            try:
                v = e["value"]
                netloc = urllib.parse.urlparse(v if "://" in v else "https:" + v).netloc
                if netloc:
                    endpoint_hosts.add(netloc)
            except Exception:
                pass

    visible_records = findings + (candidates if include_candidates else [])
    reported = [_frontend_secret_record_for_report(record) for record in visible_records[:200]]
    reported_public_ids = [
        _frontend_secret_record_for_report(record) for record in public_ids[:50]
    ]
    resource_counts = {
        "html": sum(1 for _url, kind, _text in sources if kind == "html"),
        "javascript": sum(1 for _url, kind, _text in sources if kind == "javascript"),
        "source_maps": sum(1 for _url, kind, _text in sources if kind == "source_map"),
    }
    return {
        "url": url,
        "resources_scanned": resource_counts,
        "files_scanned": len(sources),
        "bytes_scanned": bytes_scanned,
        "skipped_out_of_scope": skipped_out_of_scope[:100],
        "fetch_errors": fetch_errors[:50],
        "finding_count": len(reported),
        "verified_structure_count": sum(
            1 for item in reported if item.get("confidence") == "verified_structure"),
        "high_confidence_count": sum(
            1 for item in reported if item.get("confidence") == "high_confidence"),
        "candidate_count": len(candidates),
        "candidates_included": include_candidates,
        "public_identifier_count": len(public_ids),
        "public_identifiers": reported_public_ids,
        "findings": reported,
        "endpoint_count": len(endpoints),
        "endpoints": endpoints[:500],
        "endpoint_external_hosts": sorted(endpoint_hosts)[:100],
        "source_map_reconstruction": reconstruction,
        "provider_validation_performed": False,
        "credential_submission_guard_active": protected_value_count > 0,
        "note": (
            "Provider-shaped credentials and structurally verified private keys are returned by default. "
            "Exact matched values are included for reporting. Public client identifiers are separated and "
            "uncertain candidates are hidden. No discovered credential was submitted to a provider "
            "or used for a liveness check. Endpoints are extracted from the fetched code as attack "
            "surface only — none were requested; source_map_reconstruction recovers original files the "
            "server published in its .map artifacts. All passive: no request was made beyond fetching "
            "the page's own linked assets."
        ),
    }
