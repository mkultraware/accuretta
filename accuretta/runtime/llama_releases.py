# SPDX-License-Identifier: AGPL-3.0-only
"""Finding the newest complete official llama.cpp Windows release for a backend.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


_LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"


_LLAMA_DOWNLOAD_PREFIXES = (
    "https://github.com/ggml-org/llama.cpp/releases/download/",
    "https://github.com/ggerganov/llama.cpp/releases/download/",
)


def _latest_llama_release_assets(build_type: str) -> tuple[str, list[dict]]:
    """Pick the newest complete official Windows x64 release for this backend."""
    kind = str(build_type or "CPU").strip().upper()
    if kind not in {"CUDA", "VULKAN", "CPU"}:
        kind = "CPU"
    request = urllib.request.Request(
        _LLAMA_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AccurettaBootstrapper/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        releases = json.loads(response.read().decode("utf-8"))
    if not isinstance(releases, list):
        raise RuntimeError("GitHub returned an invalid llama.cpp release list")

    for release in releases:
        tag = str(release.get("tag_name") or "").strip()
        # llama.cpp currently marks its normal numbered b-builds as GitHub
        # prereleases. The signed b-tag and exact official assets are the useful
        # trust boundary here; drafts remain excluded.
        if not re.fullmatch(r"b\d+", tag) or release.get("draft"):
            continue
        assets = [a for a in release.get("assets") or [] if isinstance(a, dict)]
        by_name = {str(a.get("name") or ""): a for a in assets}
        names: list[str] = []
        if kind == "CPU":
            wanted = f"llama-{tag}-bin-win-cpu-x64.zip"
            if wanted in by_name:
                names = [wanted]
        elif kind == "VULKAN":
            wanted = f"llama-{tag}-bin-win-vulkan-x64.zip"
            if wanted in by_name:
                names = [wanted]
        else:
            main = sorted(n for n in by_name if re.fullmatch(
                rf"llama-{re.escape(tag)}-bin-win-cuda-12\.\d+-x64\.zip", n))
            runtime = sorted(n for n in by_name if re.fullmatch(
                r"cudart-llama-bin-win-cuda-12\.\d+-x64\.zip", n))
            if main and runtime:
                # Prefer the newest CUDA 12.x package while retaining broad
                # driver compatibility. Main and runtime versions must match.
                for candidate in reversed(main):
                    version = re.search(r"cuda-(12\.\d+)-x64", candidate).group(1)
                    support = f"cudart-llama-bin-win-cuda-{version}-x64.zip"
                    if support in by_name:
                        names = [candidate, support]
                        break
        if not names:
            continue

        selected: list[dict] = []
        for name in names:
            asset = by_name[name]
            url = str(asset.get("browser_download_url") or "")
            size = int(asset.get("size") or 0)
            if not url.startswith(_LLAMA_DOWNLOAD_PREFIXES):
                raise RuntimeError(f"refusing non-official llama.cpp asset URL: {name}")
            if size <= 0 or size > 2_500_000_000:
                raise RuntimeError(f"llama.cpp asset has an invalid size: {name}")
            selected.append({
                "name": name, "url": url, "size": size,
                "digest": str(asset.get("digest") or "").lower(),
            })
        return tag, selected
    raise RuntimeError(f"no complete current Windows {kind} llama.cpp release was found")
