# SPDX-License-Identifier: AGPL-3.0-only
"""Native binary analysis: PE and ELF inspection, disassembly with capstone, and Ghidra
analysis in its own worker process.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ghidra_worker
import os
import time
from accuretta.core.optional_imports import ELFFile
from accuretta.core.optional_imports import _ELFFile
from accuretta.core.optional_imports import _HAVE_CAPSTONE
from accuretta.core.optional_imports import _HAVE_ELFTOOLS
from accuretta.core.optional_imports import _HAVE_PEFILE
from accuretta.core.optional_imports import _HAVE_PYELFTOOLS
from accuretta.core.optional_imports import _HAVE_PYGHIDRA
from accuretta.core.optional_imports import _capstone
from accuretta.core.optional_imports import _pefile
from accuretta.core.paths import DATA
from accuretta.core.settings import get_settings
from accuretta.runtime.system_ram import _ghidra_heap_mb
from accuretta.security.approvals import request_approval
from accuretta.security.path_guards import _fw_check_path
from accuretta.tools.binary_triage import _detect_format
from accuretta.tools.binary_triage import _shannon_entropy
from accuretta.tools.ghidra_worker import _GHIDRA_WORKER
from pathlib import Path


# Capstone arch/mode mapping. Values resolved lazily so we don't fail
# import on systems without capstone.
def _capstone_md(arch: str, mode: str):
    if not _HAVE_CAPSTONE:
        return None, "capstone not installed"
    arch = (arch or "").lower()
    mode = (mode or "").lower()
    cs = _capstone
    arch_map = {
        "x86": (cs.CS_ARCH_X86, cs.CS_MODE_32),
        "x64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "x86_64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "amd64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "arm": (cs.CS_ARCH_ARM, cs.CS_MODE_ARM),
        "thumb": (cs.CS_ARCH_ARM, cs.CS_MODE_THUMB),
        "arm64": (cs.CS_ARCH_ARM64, cs.CS_MODE_ARM),
        "aarch64": (cs.CS_ARCH_ARM64, cs.CS_MODE_ARM),
        "mips": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS32),
        "mips32": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS32),
        "mips64": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS64),
        "ppc": (cs.CS_ARCH_PPC, cs.CS_MODE_32),
        "ppc64": (cs.CS_ARCH_PPC, cs.CS_MODE_64),
    }
    if arch not in arch_map:
        return None, f"unsupported arch: {arch}. Try: x86, x64, arm, thumb, arm64, mips, mips64, ppc, ppc64."
    cs_arch, cs_mode = arch_map[arch]
    # endianness override for MIPS/ARM/PPC
    if mode in ("le", "little"):
        cs_mode |= cs.CS_MODE_LITTLE_ENDIAN
    elif mode in ("be", "big"):
        cs_mode |= cs.CS_MODE_BIG_ENDIAN
    try:
        md = cs.Cs(cs_arch, cs_mode)
        md.detail = False
        return md, None
    except Exception as e:
        return None, f"capstone init failed: {e}"


def tool_disasm_at(args: dict) -> dict:
    """Disassemble N instructions at a file offset. Supports x86/x64/arm/
    thumb/arm64/mips/mips32/mips64/ppc/ppc64. Auto-detects arch/endianness
    when target is an ELF and arch arg is omitted. Use to inspect a function
    near an offset surfaced by binwalk_scan or a string xref."""
    if not _HAVE_CAPSTONE:
        return {"error": "capstone not installed. pip install capstone"}
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    offset = max(0, int(args.get("offset") or 0))
    count = max(1, min(int(args.get("count") or 32), 256))
    bytes_to_read = max(16, min(int(args.get("max_bytes") or 4096), 16 * 1024))
    arch = (args.get("arch") or "").strip()
    mode = (args.get("mode") or "").strip()
    base_addr = int(args.get("address") or offset)

    try:
        # auto-detect from ELF if arch not provided
        if not arch and _HAVE_ELFTOOLS:
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        f.seek(0)
                        elf = ELFFile(f)
                        em = elf.header["e_machine"]
                        ei = elf.header["e_ident"]["EI_DATA"]
                        cls = elf.header["e_ident"]["EI_CLASS"]
                        endian = "le" if "LSB" in ei else "be"
                        m = {
                            "EM_X86_64": ("x64", endian),
                            "EM_386": ("x86", endian),
                            "EM_ARM": ("arm", endian),
                            "EM_AARCH64": ("arm64", endian),
                            "EM_MIPS": ("mips64" if "ELFCLASS64" in cls else "mips32", endian),
                            "EM_PPC": ("ppc", endian),
                            "EM_PPC64": ("ppc64", endian),
                        }
                        if em in m:
                            arch = arch or m[em][0]
                            mode = mode or m[em][1]
            except Exception:
                pass

        if not arch:
            return {"error": "could not detect architecture; pass arch (e.g. mips, arm, x64)."}

        md, merr = _capstone_md(arch, mode)
        if merr:
            return {"error": merr}

        with open(path, "rb") as f:
            f.seek(offset)
            blob = f.read(bytes_to_read)
        if not blob:
            return {"error": f"no bytes at offset 0x{offset:x}"}

        instrs: list[dict] = []
        for ins in md.disasm(blob, base_addr):
            instrs.append({
                "address": f"0x{ins.address:x}",
                "bytes": ins.bytes.hex(),
                "mnemonic": ins.mnemonic,
                "op_str": ins.op_str,
            })
            if len(instrs) >= count:
                break

        return {
            "path": path,
            "arch": arch,
            "mode": mode or "default",
            "offset": offset,
            "address": f"0x{base_addr:x}",
            "instruction_count": len(instrs),
            "bytes_read": len(blob),
            "instructions": instrs,
        }
    except Exception as e:
        return {"error": str(e)}


# ---- Ghidra (native binary) analysis -------------------------------------
# Dangerous imports to flag (shared with the worker; see ghidra_worker.py).
_GHIDRA_DANGER_IMPORTS = ghidra_worker.DANGER_IMPORTS


def _resolve_ghidra_install_dir() -> str | None:
    """Find a Ghidra install root. Settings override > $GHIDRA_INSTALL_DIR > common locations."""
    s = get_settings()
    custom = (s.get("ghidra_path") or "").strip()
    if custom and os.path.isdir(custom):
        return custom
    env = os.environ.get("GHIDRA_INSTALL_DIR", "").strip()
    if env and os.path.isdir(env):
        return env
    # Common install spots: Program Files, or unzipped into the home folder.
    # Match anything starting with `ghidra_`, newest name first.
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")), Path.home()):
        try:
            found = sorted((c for c in base.iterdir()
                            if c.is_dir() and c.name.lower().startswith("ghidra_")
                            and (c / "support").is_dir()),
                           key=lambda c: c.name.lower(), reverse=True)
        except OSError:
            continue
        if found:
            return str(found[0])
    return None


def _ghidra_idle_s(settings: dict | None = None) -> int:
    """Seconds the Ghidra worker stays up after its last analysis."""
    s = settings if settings is not None else get_settings()
    try:
        minutes = float(s.get("ghidra_idle_minutes") or 5)
    except (TypeError, ValueError):
        minutes = 5.0
    return int(max(0.5, minutes) * 60)


def tool_ghidra_analyze(args: dict) -> dict:
    """Static analysis of a native binary using Ghidra, in its own process
    (ghidra_worker.py) with a capped heap. Loads the file, runs auto-analysis,
    and returns format/imports/exports/strings/functions plus an optional
    decompilation of a named function. The first call starts Ghidra (~10-20s);
    later calls reuse it until it has been idle for a few minutes, and an
    unchanged binary reuses its saved analysis."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    if not _HAVE_PYGHIDRA:
        return {"error": "pyghidra not installed. Run: pip install pyghidra"}

    function_name = (args.get("function") or "").strip()
    decompile = bool(args.get("decompile"))
    max_strings = max(10, min(int(args.get("max_strings") or 200), 2000))
    max_functions = max(10, min(int(args.get("max_functions") or 200), 5000))
    timeout = max(30, min(int(args.get("timeout") or 240), 1800))
    settings = get_settings()
    heap_mb = _ghidra_heap_mb(settings)

    # Approval gate: auto-analysis can be heavy on RAM/CPU for huge binaries.
    approval = request_approval(
        title="Analyze with Ghidra",
        command=f'ghidra_analyze "{Path(path).name}"' + (
            f" decompile {function_name}" if (function_name and decompile) else ""
        ),
        details={
            "kind": "ghidra_analyze",
            "source": path,
            "function": function_name,
            "decompile": decompile,
            "timeout_s": timeout,
            "memory_limit_gb": round(heap_mb / 1024, 1),
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied ghidra analysis ({approval.get('status')})"}

    t0 = time.time()
    out = _GHIDRA_WORKER.analyze(
        {"path": path, "function": function_name, "decompile": decompile,
         "max_strings": max_strings, "max_functions": max_functions},
        timeout,
        {"ACCURETTA_GHIDRA_INSTALL": _resolve_ghidra_install_dir() or "",
         "ACCURETTA_GHIDRA_HEAP_MB": str(heap_mb),
         "ACCURETTA_GHIDRA_IDLE_S": str(_ghidra_idle_s(settings)),
         "ACCURETTA_GHIDRA_PROJECTS": str(DATA / "ghidra_projects")},
        heap_mb,
    )
    if out.get("error") and not out.get("ok"):
        return out
    out["elapsed_s"] = round(time.time() - t0, 2)
    out["source"] = path
    return out


# PE machine type → architecture name. Limited to the values pefile actually
# emits. Anything missing falls through to a hex string so the user still gets
# a useful answer instead of "unknown".
_PE_MACHINE = {
    0x014c: "i386",
    0x0200: "ia64",
    0x8664: "x86_64",
    0x01c0: "ARM",
    0x01c4: "ARMv7-Thumb",
    0xaa64: "ARM64",
    0x0ebc: "EFI byte code",
    0x0162: "MIPS R3000",
    0x0166: "MIPS R4000",
    0x01f0: "PowerPC",
    0x01f1: "PowerPC FP",
    0x0266: "MIPS16",
    0x0366: "MIPS-FPU",
    0x0466: "MIPS16-FPU",
    0x5032: "RISCV32",
    0x5064: "RISCV64",
}


# Section-name fingerprints for popular packers. Match prefix because most
# packers append numbers/hex (".upx0", ".upx1", ".vmp0", ".themida0", ...).
_PACKER_SECTION_HINTS = (
    (".upx", "UPX"),
    ("upx", "UPX"),
    (".vmp", "VMProtect"),
    (".themida", "Themida"),
    (".enigma", "Enigma Protector"),
    (".aspack", "ASPack"),
    (".adata", "ASPack"),
    (".pec", "PECompact"),
    (".petite", "Petite"),
    (".mpress", "MPRESS"),
    (".nsp", "NsPack"),
    (".y0da", "yoda"),
    (".taz", "PESpin"),
)


def _inspect_pe(path: str) -> dict:
    """pefile-based triage. Architecture, sections (with entropy + R/W/X),
    imports per DLL, exports, Authenticode signature presence, imphash, and a
    packer hint based on section names."""
    if not _HAVE_PEFILE:
        return {"error": "pefile not installed. Run: pip install pefile"}
    try:
        pe = _pefile.PE(path, fast_load=False)
    except Exception as e:
        return {"error": f"pefile parse failed: {type(e).__name__}: {e}"}

    fh = pe.FILE_HEADER
    machine = getattr(fh, "Machine", 0)
    arch = _PE_MACHINE.get(machine, f"0x{machine:04x}")
    is_dll = bool(getattr(fh, "Characteristics", 0) & 0x2000)

    ts = int(getattr(fh, "TimeDateStamp", 0) or 0)
    try:
        from datetime import datetime, timezone
        ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    except Exception:
        ts_iso = None

    sections = []
    packers = set()
    for s in pe.sections:
        try:
            name = s.Name.rstrip(b"\x00").decode("utf-8", "replace")
        except Exception:
            name = repr(s.Name)
        ch = int(getattr(s, "Characteristics", 0) or 0)
        flags = ""
        flags += "R" if ch & 0x40000000 else "-"
        flags += "W" if ch & 0x80000000 else "-"
        flags += "X" if ch & 0x20000000 else "-"
        try:
            ent = round(float(s.get_entropy()), 3)
        except Exception:
            ent = 0.0
        sections.append({
            "name": name,
            "vsize": int(getattr(s, "Misc_VirtualSize", 0) or 0),
            "rsize": int(getattr(s, "SizeOfRawData", 0) or 0),
            "vaddr": f"0x{int(getattr(s, 'VirtualAddress', 0) or 0):08x}",
            "flags": flags,
            "entropy": ent,
        })
        low = name.lower()
        for pfx, label in _PACKER_SECTION_HINTS:
            if low.startswith(pfx):
                packers.add(label)
                break

    imports: dict[str, list[str]] = {}
    danger_hits: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            try:
                dll = entry.dll.decode("utf-8", "replace")
            except Exception:
                dll = repr(entry.dll)
            sym_list: list[str] = []
            for imp in entry.imports:
                if not imp.name:
                    if imp.ordinal is not None:
                        sym_list.append(f"#{imp.ordinal}")
                    continue
                try:
                    sym = imp.name.decode("utf-8", "replace")
                except Exception:
                    sym = repr(imp.name)
                sym_list.append(sym)
                base = sym.split("@", 1)[0]
                if base in _GHIDRA_DANGER_IMPORTS:
                    danger_hits.append(f"{dll}:{sym}")
            imports[dll] = sym_list

    exports: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if sym.name:
                try:
                    exports.append(sym.name.decode("utf-8", "replace"))
                except Exception:
                    exports.append(repr(sym.name))
            elif sym.ordinal is not None:
                exports.append(f"#{sym.ordinal}")

    # Authenticode signature lives in DATA_DIRECTORY[4] (IMAGE_DIRECTORY_ENTRY_SECURITY).
    sig_size = 0
    sig_present = False
    try:
        sec_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[4]
        sig_size = int(getattr(sec_dir, "Size", 0) or 0)
        sig_present = sig_size > 0
    except Exception:
        pass

    try:
        imphash = pe.get_imphash() or ""
    except Exception:
        imphash = ""

    out = {
        "format": "PE32+" if machine == 0x8664 else "PE32",
        "arch": arch,
        "is_dll": is_dll,
        "compile_timestamp": ts,
        "compile_time_iso": ts_iso,
        "section_count": len(sections),
        "sections": sections,
        "import_dll_count": len(imports),
        "import_total": sum(len(v) for v in imports.values()),
        "imports": {k: v[:200] for k, v in list(imports.items())[:50]},
        "export_count": len(exports),
        "exports": exports[:200],
        "signed": sig_present,
        "signature_size": sig_size,
        "imphash": imphash,
        "packer_hints": sorted(packers),
        "dangerous_imports": sorted(set(danger_hits))[:50],
    }
    try:
        pe.close()
    except Exception:
        pass
    return out


_ELF_MACHINE = {
    0x03: "i386", 0x3E: "x86_64", 0x28: "ARM", 0xB7: "AArch64",
    0x08: "MIPS", 0x14: "PowerPC", 0x15: "PowerPC64",
    0xF3: "RISC-V", 0x16: "S390", 0x32: "IA-64",
}


def _inspect_elf(path: str) -> dict:
    """pyelftools-based triage. Falls back to a minimal stdlib header parse if
    pyelftools isn't installed, so the user still gets *something* useful."""
    if _HAVE_PYELFTOOLS:
        try:
            with open(path, "rb") as f:
                ef = _ELFFile(f)
                hdr = ef.header
                ei_class = int(hdr["e_ident"]["EI_CLASS"])  # 1=32, 2=64
                ei_data = str(hdr["e_ident"]["EI_DATA"])
                emach = int(hdr["e_machine"]) if isinstance(hdr["e_machine"], int) else 0
                arch = _ELF_MACHINE.get(emach, str(hdr["e_machine"]))
                etype = str(hdr["e_type"])

                sections = []
                for s in ef.iter_sections():
                    try:
                        flags = int(s["sh_flags"])
                    except Exception:
                        flags = 0
                    sf = ""
                    sf += "A" if flags & 0x2 else "-"
                    sf += "W" if flags & 0x1 else "-"
                    sf += "X" if flags & 0x4 else "-"
                    sections.append({
                        "name": s.name,
                        "size": int(s["sh_size"]),
                        "addr": f"0x{int(s['sh_addr']):08x}",
                        "flags": sf,
                    })

                needed: list[str] = []
                imports: list[str] = []
                danger_hits: list[str] = []
                dyn = ef.get_section_by_name(".dynamic")
                if dyn is not None:
                    try:
                        for tag in dyn.iter_tags():
                            if getattr(tag, "entry", None) and tag.entry.d_tag == "DT_NEEDED":
                                needed.append(tag.needed)
                    except Exception:
                        pass
                dynsym = ef.get_section_by_name(".dynsym")
                if dynsym is not None:
                    try:
                        for sym in dynsym.iter_symbols():
                            if not sym.name:
                                continue
                            # imports are SHN_UNDEF entries (referenced, not defined).
                            try:
                                if sym["st_shndx"] == "SHN_UNDEF":
                                    imports.append(sym.name)
                                    base = sym.name.split("@", 1)[0]
                                    if base in _GHIDRA_DANGER_IMPORTS:
                                        danger_hits.append(sym.name)
                            except Exception:
                                continue
                    except Exception:
                        pass

                return {
                    "format": "ELF64" if ei_class == 2 else "ELF32",
                    "arch": arch,
                    "endian": "little" if "LSB" in ei_data else "big",
                    "type": etype,
                    "section_count": len(sections),
                    "sections": sections[:80],
                    "needed": needed[:80],
                    "import_count": len(imports),
                    "imports": imports[:300],
                    "dangerous_imports": sorted(set(danger_hits))[:50],
                }
        except Exception as e:
            return {"error": f"pyelftools parse failed: {type(e).__name__}: {e}"}

    # Fallback: parse just the ELF header by hand. No imports/sections, but at
    # least format/arch are useful.
    try:
        import struct
        with open(path, "rb") as f:
            ident = f.read(16)
            rest = f.read(20)
        if ident[:4] != b"\x7fELF":
            return {"error": "not an ELF file"}
        ei_class = ident[4]
        ei_data = ident[5]
        endian = "<" if ei_data == 1 else ">"
        e_type, e_machine = struct.unpack(endian + "HH", rest[:4])
        return {
            "format": "ELF64" if ei_class == 2 else "ELF32",
            "arch": _ELF_MACHINE.get(e_machine, f"0x{e_machine:04x}"),
            "endian": "little" if ei_data == 1 else "big",
            "type": f"0x{e_type:04x}",
            "note": "pyelftools not installed; install with: pip install pyelftools",
        }
    except Exception as e:
        return {"error": f"ELF header parse failed: {type(e).__name__}: {e}"}


def tool_binary_inspect(args: dict) -> dict:
    """Fast triage for native binaries. Returns format/arch/sections/imports/
    exports/entropy + packer + signing hints. Use this BEFORE ghidra_analyze
    to decide whether a deeper look is worth ~30s of analysis time."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    try:
        size = os.path.getsize(path)
    except OSError as e:
        return {"error": f"stat failed: {e}"}

    # Cap on bytes used for entropy + magic detection. Magic needs only the first
    # 4 bytes; entropy is sampled on huge installers.
    SAMPLE_CAP = 16 * 1024 * 1024
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(0)
            if size <= SAMPLE_CAP:
                whole = f.read()
            else:
                # Sample head + middle + tail so packed-section gradients don't
                # average out to a flat number.
                third = SAMPLE_CAP // 3
                a = f.read(third)
                f.seek(size // 2)
                b = f.read(third)
                f.seek(max(0, size - third))
                c = f.read(third)
                whole = a + b + c
    except OSError as e:
        return {"error": f"read failed: {e}"}

    fmt = _detect_format(head)
    file_ent = _shannon_entropy(whole)

    import hashlib
    md5 = hashlib.md5(whole).hexdigest() if size <= SAMPLE_CAP else None
    sha1 = hashlib.sha1(whole).hexdigest() if size <= SAMPLE_CAP else None
    sha256 = hashlib.sha256(whole).hexdigest() if size <= SAMPLE_CAP else None

    base: dict = {
        "path": path,
        "size": size,
        "format": fmt,
        "file_entropy": file_ent,
        "md5": md5,
        "sha1": sha1,
        "sha256": sha256,
        "sampled": size > SAMPLE_CAP,
    }

    if fmt == "PE":
        base["details"] = _inspect_pe(path)
    elif fmt == "ELF":
        base["details"] = _inspect_elf(path)
    elif fmt in ("MACHO", "MACHO_FAT"):
        base["details"] = {
            "note": "Mach-O detailed parsing not implemented yet; format detected via magic bytes.",
        }
    else:
        base["details"] = {"note": "unrecognized format (no MZ/ELF/Mach-O magic)."}

    # Risk summary the model can render directly.
    risks: list[str] = []
    det = base.get("details") or {}
    if isinstance(det, dict):
        if det.get("dangerous_imports"):
            risks.append(f"dangerous imports: {', '.join(det['dangerous_imports'][:8])}")
        if det.get("packer_hints"):
            risks.append(f"packer hint: {', '.join(det['packer_hints'])}")
        if fmt == "PE" and det.get("signed") is False:
            risks.append("unsigned PE")
        for s in (det.get("sections") or [])[:20]:
            if isinstance(s, dict) and s.get("entropy", 0) >= 7.0 and s.get("name"):
                risks.append(f"high-entropy section {s['name']} ({s['entropy']})")
    if file_ent >= 7.5:
        risks.append(f"whole-file entropy {file_ent} (likely packed/encrypted)")
    base["risk_summary"] = risks
    return base
