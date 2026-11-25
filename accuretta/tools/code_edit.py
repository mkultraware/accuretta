# SPDX-License-Identifier: AGPL-3.0-only
"""Code edits with checks: replace_ast_node, the linter run after an edit, and marking
files written from web pages.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import time
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import request_approval
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.web_guard import _web_copied
from accuretta.security.web_guard import _web_derived_paths
from accuretta.security.web_taint import _recent_web_taint
from accuretta.security.web_taint import _web_taint_lock
from pathlib import Path


def _note_web_derived_file(path: str, content: str) -> None:
    """Remember a file whose content came from a web page, so running it later
    counts as running page code."""
    if path and _recent_web_taint() and _web_copied(content, min_len=24):
        with _web_taint_lock:
            _web_derived_paths[os.path.normcase(os.path.abspath(str(path)))] = time.time()


def _run_linter(path: str) -> str:
    """Run a fast linter on the file. Returns error message if failed, empty string if passed."""
    import subprocess
    ext = os.path.splitext(path)[1].lower()
    
    if ext == ".py":
        try:
            res = subprocess.run(
                ["flake8", "--isolated", "--select=E9,F821,F822,F823", path],
                capture_output=True,
                text=True,
                timeout=5,
                env=_child_env(keep=_ENV_TOOLS),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if res.returncode == 0:
                return ""
            if res.stdout.strip():
                return f"flake8 syntax/name error:\n{res.stdout.strip()}"
        except Exception:
            pass
        # flake8 missing or inconclusive: an in-process compile still catches a real
        # syntax error, so a broken edit doesn't come back looking clean.
        try:
            compile(Path(path).read_text(encoding="utf-8", errors="replace"), path, "exec")
        except SyntaxError as e:
            return f"python syntax error: {e.msg} (line {e.lineno})"
        except Exception:
            pass
            
    elif ext in [".js", ".jsx", ".ts", ".tsx"]:
        dirpath = os.path.dirname(path)
        has_pkg = False
        while dirpath and dirpath != os.path.dirname(dirpath):
            if os.path.isfile(os.path.join(dirpath, "package.json")):
                has_pkg = True
                break
            dirpath = os.path.dirname(dirpath)
            
        if has_pkg:
            try:
                cmd = ["npx", "--no", "eslint", path]
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=_child_env(keep=_ENV_TOOLS),
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
                if res.returncode != 0:
                    return f"eslint error:\n{res.stdout.strip()}"
            except Exception:
                pass

    elif ext == ".json":
        try:
            json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        except ValueError as e:
            return f"invalid JSON: {e}"
        except Exception:
            pass

    elif ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except Exception:
            return ""
        try:
            yaml.safe_load(Path(path).read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            return f"invalid YAML: {e}"
    return ""


def _with_code_check(result: dict, findings: str) -> dict:
    if findings:
        result["code_check"] = {"status": "issues_found", "details": findings,
                                "introduced_by_edit": "unknown"}
        result["verification_guidance"] = (
            "The file change was saved. The code check found issues; whether they predate "
            "this edit is unknown. Compare with the previous version, address issues caused "
            "by this change, and report unresolved issues. Do not repeat the write just "
            "because the check found issues."
        )
    return result


def tool_replace_ast_node(args: dict) -> dict:
    """Surgical AST-based replacement. Provide path, node_type, node_name, and new_text."""
    path = normalize_path(args.get("path") or "")
    node_type = args.get("node_type") or ""
    node_name = args.get("node_name") or ""
    new_text = args.get("new_text", "")
    
    if not path or not node_type or not node_name or not new_text:
        return {"error": "Missing required arguments: path, node_type, node_name, new_text"}
    if is_data_path(path, write=True):
        return {"error": DATA_PATH_ERROR}

    ext = os.path.splitext(path)[1].lower()
    try:
        import tree_sitter
    except ImportError:
        return {"error": "tree-sitter is not installed. Please pip install tree-sitter"}
        
    lang_module = None
    if ext == ".py":
        try:
            import tree_sitter_python
            lang_module = tree_sitter_python.language()
        except ImportError:
            pass
    elif ext in [".js", ".jsx"]:
        try:
            import tree_sitter_javascript
            lang_module = tree_sitter_javascript.language()
        except ImportError:
            pass
    elif ext in [".ts", ".tsx"]:
        try:
            import tree_sitter_typescript
            lang_module = tree_sitter_typescript.language_typescript() if ext == ".ts" else tree_sitter_typescript.language_tsx()
        except ImportError:
            pass
            
    if not lang_module:
        return {"error": f"AST editing not supported for file extension {ext} or tree-sitter plugin missing"}
        
    try:
        lang = tree_sitter.Language(lang_module)
        parser = tree_sitter.Parser(lang)
    except Exception as e:
        return {"error": f"Failed to initialize tree-sitter parser: {e}"}
    
    try:
        source_bytes = Path(path).read_bytes()
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}
        
    tree = parser.parse(source_bytes)
    
    found_node = None
    def traverse(node):
        nonlocal found_node
        if found_node:
            return
        if node.type == node_type:
            name_node = node.child_by_field_name('name')
            if name_node and name_node.text.decode('utf8') == node_name:
                found_node = node
                return
            for child in node.children:
                if child.type == 'identifier' and child.text.decode('utf8') == node_name:
                    found_node = node
                    return
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    
    if not found_node:
        return {"error": f"Could not find node of type '{node_type}' with name '{node_name}'"}
        
    start = found_node.start_byte
    end = found_node.end_byte
    
    new_bytes = source_bytes[:start] + new_text.encode('utf8') + source_bytes[end:]
    
    _note_web_derived_file(path, new_text)
    approval = request_approval(
        title="AST Replace",
        command=f'Replace {node_type} {node_name} in {os.path.basename(path)}',
        details={"kind": "replace_ast_node", "path": path, "preview": new_text[:200] + "..."},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied edit ({approval.get('status')})"}
        
    Path(path).write_bytes(new_bytes)
    
    lint_err = _run_linter(path)
    return _with_code_check({"ok": True, "path": path, "replaced_node": node_name}, lint_err)
