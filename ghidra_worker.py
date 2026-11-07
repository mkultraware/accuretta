"""Ghidra analysis in its own process, for bridge.tool_ghidra_analyze.

Ghidra used to run inside the bridge through pyghidra/JPype. A JVM can't be
restarted in the same process, so its memory (Java takes up to a quarter of
RAM by default) stayed claimed until the app quit, and a timed-out analysis
kept running in a thread nobody could stop. Here the JVM gets an explicit heap
cap, the process exits after an idle spell or when the bridge goes away, and
the bridge can kill it on a timeout.

Protocol: one JSON object per line. The bridge writes requests to stdin; this
process answers on stdout, every line prefixed with MARK so stray console
output from Ghidra or Java can never be mistaken for an answer.
  started:   {"ready": true, "heap_mb": N}   or   {"fatal": "..."} (then exit)
  request:   {"id", "path", "function", "decompile", "max_strings", "max_functions"}
  answer:    {"id", "result": {...}}         or   {"id", "error": "..."}

Configuration comes from the environment (set by the bridge):
  ACCURETTA_GHIDRA_INSTALL   Ghidra install root ("" = pyghidra's own lookup)
  ACCURETTA_GHIDRA_HEAP_MB   JVM max heap in MiB
  ACCURETTA_GHIDRA_IDLE_S    exit after this many seconds without a request
  ACCURETTA_GHIDRA_PROJECTS  folder for the per-binary analysis projects

Importing this module is cheap (no pyghidra, no JVM): the bridge shares
DANGER_IMPORTS from here.
"""
import hashlib
import json
import os
import queue
import re
import sys
import threading
from pathlib import Path

MARK = "@@ACCURETTA-GHIDRA@@ "

# Common dangerous symbols to flag in import lists. Not exhaustive: the usual
# suspects for memory corruption, privilege issues and dynamic loading.
# Matched against the symbol's base name (GLIBC "memcpy@@GLIBC_2.14" -> memcpy).
DANGER_IMPORTS = {
    "strcpy", "strcat", "sprintf", "vsprintf", "gets", "scanf",
    "memcpy", "memmove", "strncpy", "strncat",
    "system", "popen", "exec", "execl", "execlp", "execle", "execv", "execvp", "execve",
    "fork", "vfork", "setuid", "setgid", "seteuid", "setegid",
    "dlopen", "dlsym", "LoadLibraryA", "LoadLibraryW", "GetProcAddress",
    "mprotect", "VirtualAlloc", "VirtualProtect", "WriteProcessMemory",
    "ptrace", "syscall",
}


def project_name_for(path: str) -> str:
    """Stable per-binary project name: re-analysing an unchanged file reuses
    its project (Ghidra skips analysis it already did); a changed file gets a
    fresh one."""
    p = Path(path)
    try:
        st = p.stat()
        stamp = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        stamp = str(p)
    key = hashlib.sha1(stamp.encode("utf-8", "replace")).hexdigest()[:10]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", p.name)[:48] or "binary"
    return f"{stem}_{key}"


def clear_stale_locks(project_dir: Path) -> int:
    """Remove a killed run's lock files so the project opens again. Only files
    named *.lock / *.lock~ directly in this project's own folder."""
    removed = 0
    try:
        for f in project_dir.iterdir():
            if f.is_file() and (f.name.endswith(".lock") or f.name.endswith(".lock~")):
                f.unlink()
                removed += 1
    except OSError:
        pass
    return removed


def collect(program, function_name: str, decompile: bool,
            max_strings: int, max_functions: int) -> dict:
    """Walk a loaded Ghidra Program and extract a JSON-friendly summary."""
    addr_space = program.getAddressFactory().getDefaultAddressSpace()
    fmt_info = {
        "format": str(program.getExecutableFormat()),
        "language": str(program.getLanguageID()),
        "compiler": str(program.getCompilerSpec().getCompilerSpecID()),
        "address_size_bits": int(addr_space.getSize()),
        "image_base": str(program.getImageBase()),
        "memory_size": int(program.getMemory().getSize()),
        "executable_md5": str(program.getExecutableMD5() or ""),
        "executable_sha256": str(program.getExecutableSHA256() or ""),
    }

    sym_tab = program.getSymbolTable()
    imports: list[str] = []
    seen_imports: set[str] = set()
    try:
        for sym in sym_tab.getExternalSymbols():
            n = str(sym.getName())
            if n and n not in seen_imports:
                seen_imports.add(n)
                imports.append(n)
            if len(imports) >= 1000:
                break
    except Exception:
        pass
    imports.sort()

    exports: list[dict] = []
    try:
        for sym in sym_tab.getSymbolIterator():
            try:
                if sym.isExternalEntryPoint():
                    exports.append({"name": str(sym.getName()), "addr": str(sym.getAddress())})
                    if len(exports) >= 500:
                        break
            except Exception:
                continue
    except Exception:
        pass

    fm = program.getFunctionManager()
    functions: list[dict] = []
    target_func = None
    try:
        for f in fm.getFunctions(True):
            entry = {
                "name": str(f.getName()),
                "addr": str(f.getEntryPoint()),
                "size": int(f.getBody().getNumAddresses()),
                "external": bool(f.isExternal()),
            }
            functions.append(entry)
            if function_name and target_func is None and entry["name"] == function_name:
                target_func = f
            if len(functions) >= max_functions and (target_func or not function_name):
                break
    except Exception:
        pass
    function_total = int(fm.getFunctionCount())

    strings: list[dict] = []
    try:
        listing = program.getListing()
        for d in listing.getDefinedData(True):
            try:
                if d.hasStringValue():
                    s_val = d.getValue()
                    if s_val is None:
                        continue
                    s_str = str(s_val)
                    if len(s_str) >= 4:
                        strings.append({"addr": str(d.getAddress()), "s": s_str[:240]})
                        if len(strings) >= max_strings:
                            break
            except Exception:
                continue
    except Exception:
        pass

    decompiled = None
    if function_name and decompile:
        if target_func is None:
            try:
                for f in fm.getFunctions(True):
                    if str(f.getName()) == function_name:
                        target_func = f
                        break
            except Exception:
                pass
        if target_func is None:
            decompiled = f"(function not found: {function_name})"
        else:
            try:
                from ghidra.app.decompiler import DecompInterface  # type: ignore
                from ghidra.util.task import ConsoleTaskMonitor    # type: ignore
                dec = DecompInterface()
                try:
                    dec.openProgram(program)
                    res = dec.decompileFunction(target_func, 60, ConsoleTaskMonitor())
                    if res.decompileCompleted():
                        decompiled = str(res.getDecompiledFunction().getC())
                    else:
                        decompiled = f"(decompile failed: {res.getErrorMessage()})"
                finally:
                    dec.dispose()
            except Exception as e:
                decompiled = f"(decompile crashed: {type(e).__name__}: {e})"

    risk: list[dict] = []
    for imp in imports:
        base = imp.split("@", 1)[0]
        if base in DANGER_IMPORTS:
            risk.append({"kind": "dangerous_import", "name": imp, "base": base})

    return {
        "ok": True,
        **fmt_info,
        "function_count": function_total,
        "function_sample": functions[:max_functions],
        "import_count": len(imports),
        "imports": imports[:300],
        "export_count": len(exports),
        "exports": exports[:200],
        "string_count": len(strings),
        "strings": strings,
        "decompiled": decompiled,
        "risk_summary": risk,
    }


def _analyze(pyghidra, projects: Path, req: dict) -> dict:
    path = str(req.get("path") or "")
    name = project_name_for(path)
    args = dict(project_location=str(projects), project_name=name, analyze=True)

    def run():
        with pyghidra.open_program(path, **args) as flat_api:
            return collect(flat_api.getCurrentProgram(), str(req.get("function") or ""),
                           bool(req.get("decompile")), int(req.get("max_strings") or 200),
                           int(req.get("max_functions") or 200))

    try:
        return run()
    except Exception as e:
        # A run the bridge killed (timeout) leaves its project locked.
        if "lock" in str(e).lower() and clear_stale_locks(projects / name):
            return run()
        raise


def main() -> int:
    # Our answers go through a private copy of stdout; anything Ghidra, Java or
    # a library prints lands on stderr (the bridge's log) instead.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr

    def say(obj: dict) -> None:
        out.write(MARK + json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        out.flush()

    heap_mb = max(512, int(os.environ.get("ACCURETTA_GHIDRA_HEAP_MB") or 4096))
    idle_s = max(30.0, float(os.environ.get("ACCURETTA_GHIDRA_IDLE_S") or 300))
    install = (os.environ.get("ACCURETTA_GHIDRA_INSTALL") or "").strip()
    projects = Path(os.environ.get("ACCURETTA_GHIDRA_PROJECTS") or Path.home() / ".accuretta-ghidra")
    try:
        projects.mkdir(parents=True, exist_ok=True)
        import pyghidra  # type: ignore
        from pyghidra.launcher import HeadlessPyGhidraLauncher  # type: ignore
        launcher = HeadlessPyGhidraLauncher(install_dir=Path(install)) if install else HeadlessPyGhidraLauncher()
        launcher.add_vmargs(f"-Xmx{heap_mb}m")
        launcher.start()
    except Exception as e:
        say({"fatal": f"Ghidra did not start: {type(e).__name__}: {e}"})
        return 1
    try:
        from java.lang import Runtime  # type: ignore
        applied_mb = int(Runtime.getRuntime().maxMemory() // (1024 * 1024))
    except Exception:
        applied_mb = None
    # heap_mb is what was asked for; applied_heap_mb is what the JVM took.
    say({"ready": True, "heap_mb": heap_mb, "applied_heap_mb": applied_mb})

    lines: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            lines.put(line)
        lines.put(None)                       # bridge closed the pipe (or died)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        try:
            line = lines.get(timeout=idle_s)
        except queue.Empty:
            return 0                          # idle: hand the memory back
        if line is None:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            say({"id": req.get("id"), "result": _analyze(pyghidra, projects, req)})
        except Exception as e:
            say({"id": req.get("id"), "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    sys.exit(main())
