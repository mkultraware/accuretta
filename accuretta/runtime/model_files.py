# SPDX-License-Identifier: AGPL-3.0-only
"""Finding model files: GGUF scans, and the vision projector and speculative
draft that belong to a model.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.paths import ROOT
from accuretta.core.safe_fs import safe_exists
from accuretta.core.safe_fs import safe_is_dir
from accuretta.core.safe_fs import safe_rglob_files
from accuretta.core.settings import _normalized_mmproj_mode
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from pathlib import Path


def _resolve_mmproj_for_tune(model_path: str, s: dict) -> str:
    """The mmproj (if any) a load of `model_path` would boot with, so
    auto_tune can reserve its VRAM. Mirrors the spawn path exactly."""
    mode = _normalized_mmproj_mode(s or {})
    if mode == "off":
        return ""
    mp = ((s or {}).get("mmproj_path") or "").strip()
    if mode == "manual":
        return mp if mp and safe_exists(mp) else ""
    if mode == "auto":
        try:
            return find_mmproj_for(model_path) or ""
        except Exception:
            return ""
    return ""


def _resolve_spec_draft_for_tune(model_path: str, s: dict) -> str:
    """The DFlash/DSpark draft GGUF a load of `model_path` would boot with, so
    auto_tune can reserve its VRAM. Mirrors the spawn path: only DFlash/DSpark
    use a side model; draft-mtp and n-gram draft from the main model itself.
    Empty when the decoder is explicitly unloaded (spec_draft_mode=off)."""
    if str((s or {}).get("spec_draft_mode") or "auto").strip().lower() == "off":
        return ""
    strategy = str((s or {}).get("spec_strategy") or "").strip().lower()
    if strategy not in {"dflash", "dspark"}:
        return ""
    configured = str((s or {}).get("spec_draft_model") or "").strip()
    if configured and safe_exists(configured):
        return configured
    # No explicit path: fall back to the metadata-validated pair on disk, which
    # is what the UI auto-fill selects for this model.
    paired = find_spec_draft_for(model_path) if model_path else {}
    if paired.get("matched") and paired.get("path") and safe_exists(paired["path"]):
        return paired["path"]
    return ""


def _gguf_model_identities(path: str | Path, keys: dict | None = None) -> set[str]:
    """Return normalized model identities from GGUF metadata, including mmproj files."""
    try:
        if keys is None:
            meta = read_gguf_metadata(str(path))
            keys = meta.get("keys") if isinstance(meta, dict) else {}
        if not isinstance(keys, dict):
            return set()
        values = []
        for key, value in keys.items():
            lower = str(key).lower()
            if (lower in {"general.name", "general.basename"}
                    or (lower.startswith("general.base_model.") and lower.endswith(".name"))):
                if isinstance(value, str) and value.strip():
                    values.append(value)
        return {re.sub(r"[^a-z0-9]+", "", value.lower()) for value in values
                if len(re.sub(r"[^a-z0-9]+", "", value.lower())) >= 5}
    except Exception:
        return set()


def find_mmproj_for(model_path: str) -> str:
    """Look for a vision projector .gguf sitting next to the chosen model.
    Heuristic: scan the model's directory (and one level up) for files whose
    name contains 'mmproj' or 'mm-proj'. Returns the absolute path of the
    closest match, or '' when nothing plausible is on disk.
    Common upstream naming: `mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf`,
    `model.mmproj.gguf`, `mm-projector.gguf`."""
    if not model_path:
        return ""
    mp = Path(model_path)
    if not safe_exists(mp):
        return ""
    candidates: list[Path] = []
    search_dirs = [mp.parent]
    if mp.parent.parent and mp.parent.parent != mp.parent:
        search_dirs.append(mp.parent.parent)
    seen: set[str] = set()
    for d in search_dirs:
        try:
            for p in d.glob("*.gguf"):
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                lname = p.name.lower()
                if "mmproj" in lname or "mm-proj" in lname or "mm_proj" in lname:
                    candidates.append(p)
        except Exception:
            continue
    if not candidates:
        return ""
    # Generic upstream names such as mmproj-F16.gguf carry the real pairing in
    # GGUF metadata. Prefer an exact metadata identity match before filename
    # heuristics; this is both safer and more useful in a crowded flat folder.
    model_ids = _gguf_model_identities(mp)
    if model_ids:
        metadata_matches = [p for p in candidates if model_ids & _gguf_model_identities(p)]
        if metadata_matches:
            metadata_matches.sort(key=lambda p: (p.parent != mp.parent, len(p.name)))
            return str(metadata_matches[0].resolve())
    # Prefer same-directory matches; then prefer names that share a token
    # with the model file (e.g. "qwen2.5-vl-7b" shows up in both).
    model_stem = mp.stem.lower()
    def score(p: Path) -> tuple[int, int, int]:
        same_dir = 0 if p.parent == mp.parent else 1
        # crude shared-substring score
        shared = 0
        for tok in re.split(r"[-_.]", model_stem):
            if len(tok) >= 3 and tok in p.stem.lower():
                shared += 1
        return (same_dir, -shared, len(p.name))
    candidates.sort(key=score)
    best = candidates[0]
    best_same_dir, best_neg_shared, _ = score(best)
    
    # If the projector shares at least one token with the model, it's a safe match.
    if best_neg_shared < 0:
        return str(best.resolve())
        
    # A projector sharing no name tokens (e.g. generic "mmproj-BF16.gguf") is only
    # auto-loaded when it is the only other .gguf in the directory; attaching a
    # generic projector to every model in a crowded folder causes crashes.
    try:
        ggufs_in_dir = list(mp.parent.glob("*.gguf"))
        if len(ggufs_in_dir) <= 2:
            return str(best.resolve())
    except Exception:
        pass
        
    print(f"[llama] skipped generic projector {best.name} for {mp.name} (too ambiguous in a crowded directory)")
    return ""


def _is_mmproj_name(name: str) -> bool:
    """A vision projector (mmproj) is a .gguf that belongs to a multimodal model
    — it's loaded via --mmproj alongside the real model, not runnable on its own.
    Match the common naming so these stay out of the model picker."""
    l = (name or "").lower()
    return "mmproj" in l or "mm-proj" in l or "mm_proj" in l


_DRAFT_NAME_RE = re.compile(r"(^|[^a-z0-9])(dflash|dspark|spec[-_]?draft|draft)(?![a-z])",
                            re.IGNORECASE)


_SPEC_FINGERPRINT_CACHE: dict[tuple[str, int, int], dict] = {}


_SPEC_FINGERPRINT_CACHE_MAX = 64


def _is_draft_name(name: str) -> bool:
    """Filename hint that a .gguf is a speculative draft rather than a chat model."""
    return bool(_DRAFT_NAME_RE.search((name or "").lower()))


def _spec_fingerprint(path: str | Path) -> dict:
    """Cheap metadata fingerprint used to pair a draft with its target model.

    DFlash/DSpark GGUFs ship `general.basename` / `general.base_model.*.name`
    alongside the target's tokenizer identity (same vocab size, merges and
    tokenizer model). Cache keyed by path + size + mtime so repeated UI scans
    don't re-read GGUF headers."""
    try:
        p = Path(path)
        st = p.stat()
    except Exception:
        return {}
    key = (str(p), int(st.st_size), int(st.st_mtime))
    cached = _SPEC_FINGERPRINT_CACHE.get(key)
    if cached is not None:
        return cached
    meta = read_gguf_metadata(str(p))
    keys = meta.get("keys") if isinstance(meta.get("keys"), dict) else {}

    def array_len(name: str) -> int:
        value = keys.get(name)
        if isinstance(value, dict) and isinstance(value.get("__array_len__"), int):
            return int(value["__array_len__"])
        return 0

    out = {
        "path": str(p),
        "name": p.name,
        "size": int(st.st_size),
        "ok": bool(meta.get("ok")),
        "architecture": str(meta.get("architecture") or "").lower(),
        "vocab": array_len("tokenizer.ggml.tokens"),
        "tokenizer": str(keys.get("tokenizer.ggml.model") or "").lower(),
        "embedding_length": int(meta.get("embedding_length") or 0),
        "block_count": int(meta.get("block_count") or 0),
        "identities": _gguf_model_identities(p, keys),
        "is_draft": str(meta.get("architecture") or "").lower() in {"dflash", "dspark"},
    }
    if len(_SPEC_FINGERPRINT_CACHE) >= _SPEC_FINGERPRINT_CACHE_MAX:
        _SPEC_FINGERPRINT_CACHE.pop(next(iter(_SPEC_FINGERPRINT_CACHE)))
    _SPEC_FINGERPRINT_CACHE[key] = out
    return out


def _identity_match(target_ids: set[str], draft_ids: set[str]) -> tuple[int, list[str]]:
    """Normalized base-model identities: exact intersection is decisive;
    containment catches publishers that suffix the identity differently."""
    exact = sorted(target_ids & draft_ids)
    if exact:
        return 100, exact
    contained: list[str] = []
    for target in target_ids:
        for draft in draft_ids:
            if not target or not draft:
                continue
            if (target in draft or draft in target) and min(len(target), len(draft)) >= 6:
                contained.append(draft if len(draft) >= len(target) else target)
    if contained:
        return 60, sorted(set(contained))
    return 0, []


def spec_draft_match(model_path: str, draft_path: str) -> dict:
    """Compare a draft GGUF against a target model. Returns
    {matched, score, reasons, draft}. A draft is trusted when:
      * both sides name the same base model (identity intersection), or
      * neither side carries identities but tokenizer vocab, tokenizer model
        and embedding length all match.
    Positive evidence of a different base model always rejects the pair."""
    target = _spec_fingerprint(model_path) if model_path else {}
    draft = _spec_fingerprint(draft_path)
    if not draft:
        return {"matched": False, "score": 0, "reasons": ["draft file unreadable"], "draft": {}}
    reasons: list[str] = []
    score = 0
    ids_score, ids = _identity_match(target.get("identities") or set(), draft.get("identities") or set())
    if ids_score:
        score += ids_score
        reasons.append("base model match: " + ", ".join(ids))
    if target.get("vocab") and target["vocab"] == draft.get("vocab"):
        score += 40
        reasons.append(f"tokenizer vocab {draft['vocab']:,}")
    if target.get("tokenizer") and target["tokenizer"] == draft.get("tokenizer"):
        score += 10
        reasons.append(f"tokenizer {draft['tokenizer']}")
    if target.get("embedding_length") and target["embedding_length"] == draft.get("embedding_length"):
        score += 20
        reasons.append(f"embedding {draft['embedding_length']}")

    matched = score >= 60
    identity_known = bool(target.get("identities")) and bool(draft.get("identities"))
    if identity_known and not ids_score:
        matched = False
        reasons.append("different base model")
    if draft.get("ok") and not draft.get("is_draft"):
        # Readable metadata wins over the filename: a full model that happens
        # to be named "…draft…" is not a loadable speculative companion.
        matched = False
        reasons.append("not a DFlash/DSpark draft")
    if not reasons:
        reasons.append("no shared tokenizer or base-model identity")
    return {"matched": matched, "score": score, "reasons": reasons, "draft": draft}


def _draft_candidates(model_path: str) -> list[Path]:
    """Draft-named .gguf files next to the model (same dir, then one level up),
    plus the configured models directory."""
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_dir(folder: Path) -> None:
        try:
            for p in folder.glob("*.gguf"):
                key = str(p).lower()
                if key in seen:
                    continue
                seen.add(key)
                if _is_draft_name(p.name):
                    candidates.append(p)
        except Exception:
            return

    if model_path:
        mp = Path(model_path)
        if safe_exists(mp):
            add_dir(mp.parent)
            if mp.parent.parent and mp.parent.parent != mp.parent:
                add_dir(mp.parent.parent)
    mdir = str(get_settings().get("models_dir") or "").strip()
    if mdir and safe_exists(mdir):
        add_dir(Path(mdir))
    return candidates


def find_spec_draft_for(model_path: str) -> dict:
    """Best **validated** DFlash/DSpark draft for a model, or {} when there is
    none. Auto-tune and the spawn path use this strict result: a mismatched or
    unverified draft must never be enabled silently. Unverified candidates are
    surfaced by list_spec_drafts() for the settings picker instead."""
    if not model_path or not safe_exists(model_path):
        return {}
    scored: list[tuple[int, int, int, Path, dict]] = []
    for candidate in _draft_candidates(model_path):
        if str(candidate) == str(model_path):
            continue
        match = spec_draft_match(model_path, str(candidate))
        if match["matched"]:
            same_dir = 0 if candidate.parent == Path(model_path).parent else 1
            scored.append((match["score"], same_dir, len(candidate.name), candidate, match))
    if not scored:
        return {}
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    _, _, _, best, match = scored[0]
    return {"path": str(best.resolve()), **match}


def list_spec_drafts(model_path: str = "") -> list[dict]:
    """Draft GGUFs available for the settings picker, best match first."""
    out: list[dict] = []
    seen: set[str] = set()
    for candidate in _draft_candidates(model_path):
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        fingerprint = _spec_fingerprint(candidate)
        match = spec_draft_match(model_path, str(candidate)) if model_path else {
            "matched": False, "score": 0, "reasons": []}
        out.append({
            "path": str(candidate.resolve()),
            "name": fingerprint.get("name") or candidate.name,
            "size": fingerprint.get("size", 0),
            "architecture": fingerprint.get("architecture", ""),
            "base": ", ".join(sorted(fingerprint.get("identities") or []))[:160],
            "matched": bool(match.get("matched")),
            "score": int(match.get("score", 0)),
            "reasons": match.get("reasons", []),
        })
    out.sort(key=lambda row: (-row["matched"], -row["score"], row["name"].lower()))
    return out


def scan_gguf_dir(root: str, include_drafts: bool = False) -> list[dict]:
    """List selectable .gguf models under root (recursive). Vision projectors
    (mmproj) and speculative drafts (DFlash/DSpark) are excluded — both are
    companions of another model, not something you can load on their own.
    Returns [{name,path,size,modified_at}]."""
    if not root:
        return []
    try:
        files = safe_rglob_files(root, "*.gguf", max_depth=6)
    except Exception:
        return []
    out = []
    for p in files:
        if _is_mmproj_name(p.name):
            continue
        if not include_drafts and _is_draft_name(p.name):
            continue
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "path": str(p.resolve()),
                "size": st.st_size,
                "modified_at": int(st.st_mtime),
            })
        except Exception:
            continue
    out.sort(key=lambda m: m["name"].lower())
    return out


def find_all_gguf_files() -> list[dict]:
    """Scan common locations for .gguf files to offer frictionless onboarding.
    Returns list of dicts: [{name, path, size, modified_at}].
    """
    s = get_settings()
    mdir = (s.get("models_dir") or "").strip()
    
    search_roots = []
    
    # 1. Configured models_dir
    if mdir and safe_exists(mdir) and safe_is_dir(mdir):
        search_roots.append(Path(mdir))
        
    # 2. Home directories
    home = Path.home()
    for sub in ["models", "Downloads", "Desktop", "Documents"]:
        search_roots.append(home / sub)
        
    # 3. LM Studio locations
    search_roots.append(home / ".lmstudio" / "models")
    search_roots.append(home / ".cache" / "lm-studio" / "models")
    
    # 4. HuggingFace hub cache
    search_roots.append(home / ".cache" / "huggingface" / "hub")
    
    # 5. Ollama models
    search_roots.append(home / ".ollama" / "models")
    
    # 6. AccuTEST workspace (ROOT) and sibling folders (ROOT.parent)
    search_roots.append(ROOT)
    if ROOT.parent and ROOT.parent != ROOT:
        search_roots.append(ROOT.parent)
        
    # 7. Top-level folders named MODELS or models on all available drives
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if safe_exists(drive):
                # Smart candidate directories on non-OS/other drives
                for folder in [
                    "MODELS", "models", 
                    "llama.cpp", "llama", 
                    "LM Studio", "LM-Studio", "lmstudio", 
                    "Ollama", "ollama", 
                    "Downloads", "Documents", "Desktop"
                ]:
                    cand = drive / folder
                    if safe_exists(cand) and safe_is_dir(cand):
                        search_roots.append(cand)
    else:
        for folder in ["/models", "/MODELS"]:
            if safe_exists(folder) and safe_is_dir(folder):
                search_roots.append(Path(folder))

    # De-duplicate and validate search roots
    seen_roots = set()
    valid_roots = []
    for r in search_roots:
        try:
            r_abs = str(r.resolve())
            if r_abs not in seen_roots and safe_exists(r) and safe_is_dir(r):
                seen_roots.add(r_abs)
                valid_roots.append(r)
        except Exception:
            continue
            
    # Scan for *.gguf in all valid roots
    all_files = []
    seen_files = set()
    
    for root in valid_roots:
        try:
            found = safe_rglob_files(root, "*.gguf", max_depth=6)
            for p in found:
                if _is_mmproj_name(p.name):
                    continue   # vision projector, not a selectable model
                p_abs = str(p.resolve())
                if p_abs not in seen_files:
                    seen_files.add(p_abs)
                    try:
                        st = p.stat()
                        all_files.append({
                            "name": p.name,
                            "path": p_abs,
                            "size": st.st_size,
                            "modified_at": int(st.st_mtime),
                        })
                    except Exception:
                        continue
        except Exception:
            continue
            
    # Sort models by modified_at descending (newest first)
    all_files.sort(key=lambda m: (-m["modified_at"], m["name"].lower()))
    return all_files
