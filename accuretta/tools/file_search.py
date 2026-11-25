# SPDX-License-Identifier: AGPL-3.0-only
"""Searching the workspace: grep, find files, find references and symbols, and the
project map.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from accuretta.core.ignore_rules import is_ignored
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _data_file_in_walk
from accuretta.security.path_guards import _fw_check_path
from accuretta.security.path_guards import _is_data_dir
from accuretta.security.path_guards import is_in_workspace
from pathlib import Path


def tool_find_references(args: dict) -> dict:
    """Find deterministic references to a symbol using AST."""
    symbol = args.get("symbol") or ""
    if not symbol:
        return {"error": "Missing symbol string"}
        
    import tree_sitter
    try:
        import tree_sitter_python
        lang_py = tree_sitter.Language(tree_sitter_python.language())
    except ImportError:
        lang_py = None
        
    try:
        import tree_sitter_javascript
        lang_js = tree_sitter.Language(tree_sitter_javascript.language())
    except ImportError:
        lang_js = None
        
    try:
        import tree_sitter_typescript
        lang_ts = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        lang_tsx = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    except ImportError:
        lang_ts = None
        lang_tsx = None
        
    hits = []
    folders = get_workspace().get("folders", [])
    if not folders:
        return {"error": "No workspace folders configured"}
        
    target_bytes = symbol.encode('utf8')
        
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d))
                       and not _is_data_dir(os.path.join(root, d))]
            for fname in files:
                fpath = os.path.join(root, fname)
                if is_ignored(fpath) or _data_file_in_walk(fpath):
                    continue
                    
                ext = os.path.splitext(fname)[1].lower()
                lang = None
                if ext == ".py":
                    lang = lang_py
                elif ext in [".js", ".jsx"]:
                    lang = lang_js
                elif ext == ".ts":
                    lang = lang_ts
                elif ext == ".tsx":
                    lang = lang_tsx
                    
                if not lang:
                    continue
                    
                try:
                    content = Path(fpath).read_bytes()
                except Exception:
                    continue
                    
                if target_bytes not in content:
                    continue
                    
                try:
                    parser = tree_sitter.Parser(lang)
                    tree = parser.parse(content)
                except Exception:
                    continue
                
                code_lines = content.decode('utf8', errors='replace').splitlines()
                
                def traverse(node):
                    if len(node.children) == 0 and node.text == target_bytes:
                        line_start = node.start_point[0]
                        if line_start < len(code_lines):
                            hits.append({
                                "file": fpath,
                                "line": line_start + 1,
                                "code": code_lines[line_start].strip()
                            })
                    for child in node.children:
                        traverse(child)
                traverse(tree.root_node)
                
    unique_hits = []
    seen = set()
    for h in hits:
        k = (h["file"], h["line"])
        if k not in seen:
            seen.add(k)
            unique_hits.append(h)
            
    return {"symbol": symbol, "matches": unique_hits[:100]}


# ---- symbol index (find_symbol) -------------------------------------------
# Reuses the tree-sitter grammars already loaded for AST edits. Answers "where is
# X DEFINED" (find_references answers "where is X used") plus single-file outline.
# In-memory mtime cache so repeat lookups don't reparse. No embeddings, no server.
_SYMBOL_DEF_TYPES = {
    "py": {"function_definition": "function", "class_definition": "class"},
    "js": {"function_declaration": "function", "generator_function_declaration": "function",
           "class_declaration": "class", "method_definition": "method"},
    "ts": {"function_declaration": "function", "generator_function_declaration": "function",
           "class_declaration": "class", "abstract_class_declaration": "class",
           "method_definition": "method", "interface_declaration": "interface",
           "type_alias_declaration": "type", "enum_declaration": "enum"},
}


_SYMBOL_CACHE: dict = {}   # fpath -> (mtime, [ {name,kind,line,sig} ])


def _symbols_in_file(fpath: str, lang, kinds: dict) -> list:
    import tree_sitter
    try:
        mtime = os.path.getmtime(fpath)
    except Exception:
        return []
    cached = _SYMBOL_CACHE.get(fpath)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        content = Path(fpath).read_bytes()
        parser = tree_sitter.Parser(lang)
        tree = parser.parse(content)
    except Exception:
        return []
    syms: list = []
    def name_of(node):
        n = node.child_by_field_name("name")
        if n is not None:
            try:
                return n.text.decode("utf8", "replace")
            except Exception:
                return ""
        return ""
    def first_line(node):
        try:
            t = node.text.decode("utf8", "replace")
        except Exception:
            return ""
        return (t.splitlines()[0].strip() if t else "")[:120]
    def visit(node):
        kind = kinds.get(node.type)
        if kind:
            nm = name_of(node)
            if nm:
                syms.append({"name": nm, "kind": kind, "line": node.start_point[0] + 1, "sig": first_line(node)})
        elif node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type in ("arrow_function", "function", "function_expression"):
                nm = name_of(node)
                if nm:
                    syms.append({"name": nm, "kind": "function", "line": node.start_point[0] + 1, "sig": first_line(node)})
        for c in node.children:
            visit(c)
    visit(tree.root_node)
    _SYMBOL_CACHE[fpath] = (mtime, syms)
    return syms


def tool_find_symbol(args: dict) -> dict:
    """Where a symbol is DEFINED across the workspace (with signature), or outline
    one file's definitions. Complements find_references (which finds uses)."""
    name = (args.get("name") or "").strip()
    only_path = normalize_path(args.get("path") or "") if args.get("path") else ""
    if not name and not only_path:
        return {"error": "pass name (symbol to locate) and/or path (file to outline)"}

    import tree_sitter
    langs = {}
    try:
        import tree_sitter_python
        langs["py"] = (tree_sitter.Language(tree_sitter_python.language()), _SYMBOL_DEF_TYPES["py"])
    except Exception:
        pass
    try:
        import tree_sitter_javascript
        langs["js"] = (tree_sitter.Language(tree_sitter_javascript.language()), _SYMBOL_DEF_TYPES["js"])
    except Exception:
        pass
    try:
        import tree_sitter_typescript
        langs["ts"] = (tree_sitter.Language(tree_sitter_typescript.language_typescript()), _SYMBOL_DEF_TYPES["ts"])
        langs["tsx"] = (tree_sitter.Language(tree_sitter_typescript.language_tsx()), _SYMBOL_DEF_TYPES["ts"])
    except Exception:
        pass
    if not langs:
        return {"error": "tree-sitter language grammars not installed"}

    def lang_for(ext):
        if ext == ".py":
            return langs.get("py")
        if ext in (".js", ".jsx"):
            return langs.get("js")
        if ext == ".ts":
            return langs.get("ts")
        if ext == ".tsx":
            return langs.get("tsx")
        return None

    if only_path:
        if is_data_path(only_path):
            return {"error": DATA_PATH_ERROR}
        lf = lang_for(os.path.splitext(only_path)[1].lower())
        if not lf:
            return {"error": f"unsupported file type for outline: {only_path}"}
        syms = _symbols_in_file(only_path, lf[0], lf[1])
        if name:
            syms = [s for s in syms if s["name"] == name]
        return {"path": only_path, "symbols": syms[:200]}

    folders = get_workspace().get("folders", [])
    if not folders:
        return {"error": "No workspace folders configured"}
    target = name.encode("utf8")
    hits = []
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d))
                       and not _is_data_dir(os.path.join(root, d))]
            for fname in files:
                fpath = os.path.join(root, fname)
                if is_ignored(fpath) or _data_file_in_walk(fpath):
                    continue
                lf = lang_for(os.path.splitext(fname)[1].lower())
                if not lf:
                    continue
                try:
                    if target not in Path(fpath).read_bytes():
                        continue
                except Exception:
                    continue
                for s in _symbols_in_file(fpath, lf[0], lf[1]):
                    if s["name"] == name:
                        hits.append({"file": fpath, "line": s["line"], "kind": s["kind"], "sig": s["sig"]})
            if len(hits) >= 100:
                break
    return {"symbol": name, "definitions": hits[:100]}


def tool_find_files(args: dict) -> dict:
    """Recursive glob under a directory with optional size cap. Good for
    triaging extracted firmware roots without reading everything."""
    root, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}", "expected_type": "directory",
                "actual_type": "file" if os.path.isfile(root) else "unknown"}
    pattern = (args.get("pattern") or "*").strip() or "*"
    max_size = int(args.get("max_size") or 0)  # 0 = no cap
    max_results = max(1, min(int(args.get("max_results") or 500), 5000))
    try:
        matches: list[dict] = []
        for p in Path(root).rglob(pattern):
            if not p.is_file() or is_data_path(str(p)):
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if max_size and sz > max_size:
                continue
            matches.append({"path": str(p), "size": sz})
            if len(matches) >= max_results:
                break
        return {"root": root, "count": len(matches), "matches": matches}
    except Exception as e:
        return {"error": str(e)}


# Skip these directories during recursive grep — they explode result counts
# without surfacing useful matches in firmware-analysis contexts.
_GREP_SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".tox", "dist", "build",
}


# Files larger than this (per file) are skipped. Avoids wasting cycles
# grepping multi-MB binaries where strings_dump is the right tool.
_GREP_MAX_FILE_BYTES = 4 * 1024 * 1024


def _grep_candidates(root: str):
    """The file itself, or every file under a directory minus noise folders."""
    if os.path.isfile(root):
        yield root
        return
    for cur_dir, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _GREP_SKIP_DIRS
                       and not _is_data_dir(os.path.join(cur_dir, d))]
        for fn in filenames:
            fp = os.path.join(cur_dir, fn)
            if not _data_file_in_walk(fp):
                yield fp


def tool_grep_files(args: dict) -> dict:
    """Regex search in one file or across a directory tree. Returns matches
    with file path, line number and the matched line (truncated), optionally
    with surrounding lines, or only the matching files. Skips binary files
    (null-byte heuristic) and obvious noise dirs (.git, node_modules)."""
    root, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    pattern = args.get("pattern") or ""
    if not pattern:
        return {"error": "missing pattern"}
    glob_pat = (args.get("glob") or "").strip() or None
    case_insensitive = bool(args.get("case_insensitive"))
    max_matches = max(1, min(int(args.get("max_matches") or 200), 2000))
    max_files = max(1, min(int(args.get("max_files") or 5000), 50000))
    context = max(0, min(int(args.get("context") or 0), 5))
    files_only = bool(args.get("files_only"))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"bad regex: {e}"}

    matches: list[dict] = []
    file_hits: list[dict] = []
    files_scanned = 0
    files_skipped_binary = 0
    files_skipped_size = 0
    truncated = False

    try:
        for fp in _grep_candidates(root):
            if files_scanned >= max_files:
                truncated = True
                break
            if glob_pat:
                fn = os.path.basename(fp)
                try:
                    if not Path(fp).match(glob_pat) and not fnmatch.fnmatch(fn, glob_pat):
                        continue
                except Exception:
                    if not fnmatch.fnmatch(fn, glob_pat):
                        continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if st.st_size > _GREP_MAX_FILE_BYTES:
                files_skipped_size += 1
                continue
            # null-byte heuristic on first 4KB to skip binaries
            try:
                with open(fp, "rb") as f:
                    sniff = f.read(4096)
                if b"\x00" in sniff:
                    files_skipped_binary += 1
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except Exception:
                continue
            files_scanned += 1
            hits = [i for i, line in enumerate(lines) if rx.search(line)]
            if not hits:
                continue
            if files_only:
                file_hits.append({"path": fp, "count": len(hits)})
                if len(file_hits) >= max_matches:
                    truncated = True
                    break
                continue
            for i in hits:
                match = {"path": fp, "line": i + 1, "text": lines[i][:400]}
                if context:
                    match["before"] = [line[:400] for line in lines[max(0, i - context):i]]
                    match["after"] = [line[:400] for line in lines[i + 1:i + 1 + context]]
                matches.append(match)
                if len(matches) >= max_matches:
                    truncated = True
                    break
            if truncated:
                break
        result = {
            "root": root,
            "pattern": pattern,
            "match_count": sum(h["count"] for h in file_hits) if files_only else len(matches),
            "files_scanned": files_scanned,
            "files_skipped_binary": files_skipped_binary,
            "files_skipped_size": files_skipped_size,
            "truncated": truncated,
            "matches": matches,
        }
        if files_only:
            result["files"] = file_hits
        return result
    except Exception as e:
        return {"error": str(e)}


_PROJECT_MAP_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules", "vendor",
    ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "dist", "build", "coverage", ".next", ".nuxt", "target",
}


_PROJECT_MAP_IMPORTANT = {
    "readme.md", "package.json", "pyproject.toml", "requirements.txt", "setup.py",
    "cargo.toml", "go.mod", "pom.xml", "build.gradle", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "makefile", "justfile",
    "tsconfig.json", "vite.config.js", "vite.config.ts", "next.config.js",
    "pytest.ini", "tox.ini", ".github", "src", "app", "tests", "test",
}


def tool_project_map(args: dict) -> dict:
    """Return a compact, deterministic map of a repository in one call."""
    root = Path(normalize_path(args.get("path") or ""))
    if not root.exists() or not root.is_dir():
        return {"error": "project directory not found"}
    if not is_in_workspace(str(root)):
        return {"error": "Path is outside workspace bounds."}
    try:
        max_files = max(100, min(50000, int(args.get("max_files") or 15000)))
    except (TypeError, ValueError):
        max_files = 15000

    ext_counts: dict[str, int] = {}
    dir_counts: dict[str, int] = {}
    files: list[tuple[Path, int]] = []
    important: list[str] = []
    tests: list[str] = []
    truncated = False
    for current, dirs, names in os.walk(root):
        current_p = Path(current)
        dirs[:] = [d for d in dirs
                   if d.lower() not in _PROJECT_MAP_SKIP_DIRS
                   and not is_ignored(str(current_p / d))
                   and not _is_data_dir(str(current_p / d))]
        for name in names:
            path = current_p / name
            if is_ignored(str(path)):
                continue
            try:
                rel = path.relative_to(root).as_posix()
                size = path.stat().st_size
            except Exception:
                continue
            files.append((path, size))
            top = rel.split("/", 1)[0]
            dir_counts[top] = dir_counts.get(top, 0) + 1
            ext = path.suffix.lower() or "[no extension]"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            low = name.lower()
            if (low in _PROJECT_MAP_IMPORTANT or top.lower() in _PROJECT_MAP_IMPORTANT
                    or rel.lower() in _PROJECT_MAP_IMPORTANT):
                if len(important) < 80:
                    important.append(rel)
            if (low.startswith("test_") or low.endswith(("_test.py", ".test.js", ".spec.js",
                                                          ".test.ts", ".spec.ts"))
                    or "/tests/" in "/" + rel.lower() + "/"):
                if len(tests) < 80:
                    tests.append(rel)
            if len(files) >= max_files:
                truncated = True
                break
        if truncated:
            break

    likely_commands: list[str] = []
    package = root / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
            for name in ("test", "lint", "typecheck", "check", "build"):
                if name in scripts:
                    likely_commands.append(f"npm run {name}")
        except Exception:
            pass
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or tests:
        if any(p.suffix.lower() == ".py" for p, _ in files):
            likely_commands.append("python -m pytest")
    if (root / "Cargo.toml").exists():
        likely_commands += ["cargo test", "cargo clippy"]
    if (root / "go.mod").exists():
        likely_commands.append("go test ./...")
    if (root / "pom.xml").exists():
        likely_commands.append("mvn test")

    source_exts = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
                   ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
                   ".kt", ".kts", ".html", ".css", ".scss"}
    largest = sorted(((p, size) for p, size in files if p.suffix.lower() in source_exts),
                     key=lambda item: item[1], reverse=True)[:20]
    return {
        "root": str(root),
        "files_scanned": len(files),
        "truncated": truncated,
        "languages_by_file_count": dict(sorted(ext_counts.items(), key=lambda x: x[1], reverse=True)[:20]),
        "top_level": dict(sorted(dir_counts.items(), key=lambda x: x[1], reverse=True)[:30]),
        "important_paths": sorted(dict.fromkeys(important))[:80],
        "test_paths": sorted(dict.fromkeys(tests))[:80],
        "largest_source_files": [
            {"path": p.relative_to(root).as_posix(), "bytes": size} for p, size in largest
        ],
        "likely_verification_commands": list(dict.fromkeys(likely_commands)),
        "next": ("Read the README and relevant manifest, then use read_skeleton on the largest or "
                 "task-relevant source files. Verify changes with check_syntax/run_tests."),
    }
