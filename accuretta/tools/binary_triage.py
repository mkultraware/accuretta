# SPDX-License-Identifier: AGPL-3.0-only
"""Binary triage helpers: entropy, file format detection (PE/ELF/Mach-O), YARA string matches.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _shannon_entropy(data: bytes) -> float:
    """Standard Shannon entropy in bits/byte (0-8). High entropy (>7.0) on
    a PE section is a strong hint that it's packed or encrypted."""
    if not data:
        return 0.0
    from collections import Counter
    import math
    counts = Counter(data)
    total = len(data)
    ent = 0.0
    for c in counts.values():
        p = c / total
        ent -= p * math.log2(p)
    return round(ent, 3)


def _detect_format(head: bytes) -> str:
    """Magic-byte sniff. Returns one of: PE, ELF, MACHO, MACHO_FAT, UNKNOWN."""
    if len(head) < 4:
        return "UNKNOWN"
    if head[:2] == b"MZ":
        return "PE"
    if head[:4] == b"\x7fELF":
        return "ELF"
    # Mach-O 32/64 little + big endian, plus FAT (universal).
    macho = {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    }
    if head[:4] in macho:
        return "MACHO"
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        # Note: Java .class also uses CAFEBABE — but at offset 0 with magic,
        # it's followed by a u2 minor version; Mach-O FAT has nfat_arch. We
        # don't disambiguate further here; the caller will fall through to
        # unknown format if neither pefile nor pyelftools accepts the file.
        return "MACHO_FAT"
    return "UNKNOWN"


def _yara_string_to_dict(item) -> dict:
    """Adapt to both new (yara-python >=4.3 StringMatch object) and old (tuple)
    match string formats so the tool keeps working across versions."""
    # New API: StringMatch(identifier, instances=[StringMatchInstance(offset, matched_data, ...)])
    try:
        ident = getattr(item, "identifier", None)
        if ident is not None:
            instances = []
            for inst in getattr(item, "instances", []) or []:
                try:
                    matched = getattr(inst, "matched_data", b"") or b""
                    if isinstance(matched, bytes):
                        try:
                            mtxt = matched.decode("utf-8", "replace")
                        except Exception:
                            mtxt = matched.hex()
                    else:
                        mtxt = str(matched)
                    instances.append({
                        "offset": int(getattr(inst, "offset", 0) or 0),
                        "match": mtxt[:200],
                    })
                except Exception:
                    continue
            return {"identifier": ident, "instances": instances[:10]}
    except Exception:
        pass
    # Old tuple API: (offset, identifier, matched_bytes)
    try:
        off, ident, matched = item
        if isinstance(matched, bytes):
            try:
                mtxt = matched.decode("utf-8", "replace")
            except Exception:
                mtxt = matched.hex()
        else:
            mtxt = str(matched)
        return {"identifier": ident, "instances": [{"offset": int(off), "match": mtxt[:200]}]}
    except Exception:
        return {"identifier": str(item), "instances": []}
