# SPDX-License-Identifier: AGPL-3.0-only
"""The check_syntax tool: Python, JavaScript, CSS, HTML and JSON checks, with a trusted
Node for the JavaScript ones.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from accuretta.core.paths import ROOT
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.core.workspace import normalize_path
from accuretta.security.child_env import _child_env
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import is_in_workspace
from accuretta.tools.html_syntax import _check_html_syntax
from pathlib import Path
from task_state import file_revision


def _trusted_node_binary() -> str:
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for root in filter(None, roots):
            candidate = os.path.abspath(os.path.join(root, "nodejs", "node.exe"))
            if os.path.isfile(candidate):
                return candidate
    current = os.path.normcase(os.path.abspath(os.getcwd()))
    workspace = os.path.normcase(os.path.abspath(str(ROOT)))
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or not os.path.isabs(raw_dir):
            continue
        directory = os.path.normcase(os.path.abspath(raw_dir))
        if directory == current or directory == workspace or directory.startswith(workspace + os.sep):
            continue
        candidate = os.path.join(directory, "node.exe" if os.name == "nt" else "node")
        if os.path.isfile(candidate):
            return candidate
    return ""


def _syntax_unchecked(reason: str) -> dict:
    return {"check_status": "unchecked", "not_executed": True, "reason": reason,
            "result": f"Syntax not checked: {reason}"}


def _check_css_syntax(file_path: str) -> dict:
    node_exe = _trusted_node_binary()
    if not node_exe:
        return _syntax_unchecked("A trusted Node.js executable is required for CSS parsing.")
    checker = ROOT / "css-syntax-check.cjs"
    if not checker.is_file() or not (ROOT / "assets/vendor/postcss/parse.cjs").is_file():
        return _syntax_unchecked("The bundled CSS parser is missing.")
    try:
        source = Path(file_path).read_text(encoding="utf-8-sig")
        environment = {key: value for key, value in _child_env().items()
                       if key.upper() not in {"NODE_OPTIONS", "NODE_PATH"}}
        completed = subprocess.run(
            [node_exe, str(checker), file_path], input=source, capture_output=True,
            text=True, encoding="utf-8", cwd=str(ROOT), env=environment,
            creationflags=_NO_WINDOW, timeout=30)
        if completed.returncode:
            return _syntax_unchecked("The CSS parser could not complete its check.")
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("check_status") not in {"passed", "failed"}:
            return _syntax_unchecked("The CSS parser returned an invalid result.")
        return result
    except subprocess.TimeoutExpired:
        return _syntax_unchecked("CSS parsing timed out after 30 seconds.")
    except (OSError, UnicodeError, ValueError) as error:
        return _syntax_unchecked(f"CSS parsing could not run: {error}")


def tool_check_syntax(args: dict) -> dict:
    path = normalize_path(args.get("path", ""))
    if is_data_path(path):
        return {"error": DATA_PATH_ERROR}
    if not is_in_workspace(path):
        return {"error": "Path is outside workspace bounds."}
    before = file_revision(path)
    result = _check_syntax_impl(args)
    if (not result.get("error") and not result.get("not_executed")
            and result.get("check_status") != "unchecked"
            and before and file_revision(path) == before):
        result["verification"] = {"sha256": before, "kind": "syntax"}
    return result


def _check_syntax_impl(args: dict) -> dict:
    file_path = normalize_path(args.get("path", ""))
    if not is_in_workspace(file_path):
        return {"error": "Path is outside workspace bounds."}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.css':
        return _check_css_syntax(file_path)
    if ext in ('.html', '.htm', '.xhtml'):
        return _check_html_syntax(file_path)
    if ext == '.py':
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                ast.parse(f.read(), filename=file_path)
            return {"result": "Syntax OK"}
        except SyntaxError as e:
            return {"error": f"SyntaxError at line {e.lineno}, offset {e.offset}: {e.msg}\n{e.text}"}
        except Exception as e:
            return {"error": str(e)}
            
    elif ext in ('.js', '.ts', '.jsx', '.tsx'):
        node_exe = _trusted_node_binary()
        if not node_exe:
            return {"error": "A trusted Node.js executable was not found outside the workspace."}
        try:
            res = subprocess.run([node_exe, '--check', file_path], capture_output=True, text=True,
                                 cwd=str(ROOT), env=_child_env(), creationflags=_NO_WINDOW,
                                 timeout=60)
            if res.returncode == 0:
                return {"result": "Syntax OK"}
            return {"error": res.stderr.strip() or "Syntax check failed"}
        except subprocess.TimeoutExpired:
            return {"error": "Node.js syntax check timed out after 60 seconds."}
    else:
        return _syntax_unchecked(f"Unsupported file type: {ext or '(no extension)'}.")
