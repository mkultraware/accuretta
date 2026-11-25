"""Environment-leak canary for the programs the bridge starts.

    python tests/canary_test.py

Puts ACCURETTA_CANARY=canary-7f3a91 and a few look-alike variables (a fake API
key, a proxy with and without a login, an SSH agent socket, a dev-tool path)
into this process's environment, starts children the way the bridge does and
reports LEAK or CLEAN for each:

  control      a plain child started with no env= (must LEAK: proves the canary)
  shell tool   tool_run_powershell, end to end (approval auto-granted)
  python tool  tool_run_python, end to end (approval auto-granted)
  MCP server   MCPClient.start() against a minimal real MCP server
  Ghidra       _GhidraWorker._start() with a stand-in worker

Run directly, it also compares what everyday tools see before and after the
fix (git identity, npx, node, pip, PowerShell modules, nvidia-smi), boots the
real Ghidra worker when Ghidra is installed, checks the data-folder block,
and checks that Soft mode asks before a shell command touches the data folder.
pytest collects this file too (*_test.py), so the suite keeps guarding it.

Only variable NAMES are ever printed, never values.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    # Same isolation conftest.py gives the suite: never the real data folder.
    os.environ.setdefault("ACCURETTA_DATA_DIR", tempfile.mkdtemp(prefix="accuretta-canary-"))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import bridge  # noqa: E402
from _patching import bridge_patch

CANARY = "ACCURETTA_CANARY"
FAKES = {
    CANARY: "canary-7f3a91",
    "ACCURETTA_FAKE_API_KEY": "fake-key-not-real",
    "HTTP_PROXY": "http://proxy.invalid:3128",
    "HTTPS_PROXY": "http://canary:fakepass@proxy.invalid:3128",
    "SSH_AUTH_SOCK": "canary-agent.sock",
    "GOPATH": str(Path(tempfile.gettempdir()) / "canary-gopath"),
}
# Every name a probe reports on. Probes print names only.
WATCHED = sorted(set(FAKES) | {"CANARY_CONFIGURED", "ACCURETTA_GHIDRA_HEAP_MB",
                               "SYSTEMROOT", "PATH"})
NEVER = {CANARY, "ACCURETTA_FAKE_API_KEY"}
MARK = "@@CANARY@@"
WINDOWS = os.name == "nt"

# target -> (names that must be absent, names that must be present)
EXPECT = {
    "shell tool": (NEVER, {"PATH", "HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "GOPATH"}
                   | ({"SYSTEMROOT"} if WINDOWS else set())),
    "python tool": (NEVER, {"PATH", "HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "GOPATH"}
                    | ({"SYSTEMROOT"} if WINDOWS else set())),
    "MCP server": (NEVER | {"HTTPS_PROXY", "SSH_AUTH_SOCK", "GOPATH"},
                   {"PATH", "HTTP_PROXY", "CANARY_CONFIGURED"} | ({"SYSTEMROOT"} if WINDOWS else set())),
    "Ghidra worker": (NEVER | {"SSH_AUTH_SOCK", "HTTPS_PROXY"},
                      {"PATH", "GOPATH", "ACCURETTA_GHIDRA_HEAP_MB"} | ({"SYSTEMROOT"} if WINDOWS else set())),
}

PY_PROBE = (f"import os; names = {WATCHED!r}; "
            f"print({MARK!r} + ','.join(n for n in names if n in os.environ))")


@contextlib.contextmanager
def fake_environment():
    saved = {k: os.environ.get(k) for k in FAKES}
    os.environ.update(FAKES)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seen(text: str) -> set[str]:
    for line in (text or "").splitlines():
        if line.strip().startswith(MARK):
            return {n for n in line.strip()[len(MARK):].split(",") if n}
    raise AssertionError("probe printed no result")


@contextlib.contextmanager
def _headless():
    """Grant approvals and keep execution events off any UI."""
    with bridge_patch("request_approval", return_value={"decision": "approve"}), \
            bridge_patch("_emit_execution_event"), \
            bridge_patch("get_workspace", return_value={"folders": []}):
        yield


# ---- probes -----------------------------------------------------------------

def probe_control() -> set[str]:
    out = subprocess.run([sys.executable, "-c", PY_PROBE], capture_output=True, text=True,
                         timeout=60)
    return _seen(out.stdout)


def probe_shell_tool() -> set[str]:
    names = ",".join(f"'{n}'" for n in WATCHED)
    command = (f"'{MARK}' + ((@({names}) | Where-Object {{ Test-Path ('env:' + $_) }}) -join ',')")
    with _headless():
        result = bridge.tool_run_powershell({"command": command, "timeout": 60})
    return _seen(result.get("stdout", ""))


def probe_python_tool() -> set[str]:
    with _headless():
        result = bridge.tool_run_python({"code": PY_PROBE, "timeout": 60})
    return _seen(result.get("stdout", ""))


MCP_SERVER = f"""
import json, os, sys
NAMES = {WATCHED!r}
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("method") == "initialize":
        seen = [n for n in NAMES if n in os.environ]
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": msg["id"], "result": {{
            "protocolVersion": "2024-11-05", "capabilities": {{}},
            "serverInfo": {{"name": "canary", "version": "1"}}, "seen": seen}}}}) + "\\n")
        sys.stdout.flush()
"""


def probe_mcp_server() -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        server = Path(tmp) / "canary_mcp_server.py"
        server.write_text(MCP_SERVER, encoding="utf-8")
        client = bridge.MCPClient("canary", sys.executable, [str(server)],
                                  {"CANARY_CONFIGURED": "1"})
        try:
            response = client.start() or {}
        finally:
            client.stop()
    seen = (response.get("result") or {}).get("seen")
    assert seen is not None, f"MCP probe server gave no answer: {response.get('error')}"
    return set(seen)


GHIDRA_STAND_IN = f"""
import json, os, sys
MARK = "@@ACCURETTA-GHIDRA@@ "
seen = [n for n in {WATCHED!r} if n in os.environ]
print(MARK + json.dumps({{"ready": True}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(MARK + json.dumps({{"id": req["id"], "result": {{"seen": seen}}}}), flush=True)
"""


def _ghidra_env_extra(heap_mb: int) -> dict:
    return {"ACCURETTA_GHIDRA_INSTALL": bridge._resolve_ghidra_install_dir() or "",
            "ACCURETTA_GHIDRA_HEAP_MB": str(heap_mb),
            "ACCURETTA_GHIDRA_IDLE_S": "60",
            "ACCURETTA_GHIDRA_PROJECTS": str(bridge.DATA / "ghidra_projects")}


def probe_ghidra_worker() -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        stand_in = Path(tmp) / "ghidra_stand_in.py"
        stand_in.write_text(GHIDRA_STAND_IN, encoding="utf-8")
        worker = bridge._GhidraWorker(cmd=[sys.executable, "-u", str(stand_in)])
        try:
            result = worker.analyze({"path": "canary"}, 60, _ghidra_env_extra(1024), 1024)
        finally:
            worker.stop()
    assert "seen" in result, f"Ghidra stand-in gave no answer: {result.get('error')}"
    return set(result["seen"])


PROBES = {
    "shell tool": probe_shell_tool,
    "python tool": probe_python_tool,
    "MCP server": probe_mcp_server,
    "Ghidra worker": probe_ghidra_worker,
}


def verdict(target: str, seen: set[str]) -> tuple[str, list[str]]:
    absent, present = EXPECT[target]
    leaked = sorted(seen & absent)
    missing = sorted(present - seen)
    notes = []
    if leaked:
        notes.append("got " + ", ".join(leaked))
    if missing:
        notes.append("missing " + ", ".join(missing))
    if leaked:
        return "LEAK", notes
    return ("CLEAN" if not missing else "CLEAN but BROKEN"), notes


# ---- pytest ------------------------------------------------------------------

def test_control_child_sees_the_canary():
    with fake_environment():
        assert CANARY in probe_control()


@pytest.mark.parametrize("target", list(PROBES))
def test_child_does_not_leak(target):
    if target in ("shell tool", "python tool") and not WINDOWS:
        pytest.skip("the host shell tool is Windows PowerShell")
    with fake_environment():
        seen = PROBES[target]()
    status, notes = verdict(target, seen)
    assert status == "CLEAN", f"{target}: {status} ({'; '.join(notes)})"


def test_every_launch_in_bridge_sets_a_filtered_env():
    """A new subprocess call without env=, or one that copies os.environ,
    fails here before it can ship. Every file of the app: code moved out of
    bridge.py into accuretta/ is checked the same way."""
    from _source import app_files
    bad = []
    for path in app_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"subprocess", "_sp"}):
                continue
            env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
            if env is None or "os.environ" in ast.unparse(env):
                bad.append(f"{where}:{node.lineno}")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {"_git_env", "_wsl_env",
                                                                   "_ghidra_worker_env"}:
                if "os.environ.copy" in ast.unparse(node) or "dict(os.environ)" in ast.unparse(node):
                    bad.append(f"{where}:{node.lineno}")
    assert not bad, f"lines starting a program with the full environment: {bad}"


def _data_layout(tmp: Path) -> tuple[Path, Path, Path]:
    data = tmp / "project" / "data"
    (data / "reports").mkdir(parents=True)
    secret = data / "settings.json"
    secret.write_text('{"discord_bot_token": "canary-fake-token"}', encoding="utf-8")
    report = data / "reports" / "report-1.md"
    report.write_text("# canary report\ncanary-fake-token is not here", encoding="utf-8")
    (tmp / "project" / "notes.txt").write_text("discord_bot_token mention", encoding="utf-8")
    return data, secret, report


def check_data_block(tmp: Path) -> list[str]:
    """Returns the failed checks (empty = all good)."""
    data, secret, report = _data_layout(tmp)
    err = bridge.DATA_PATH_ERROR
    project = data.parent
    failures = []

    def expect(label, result, blocked):
        got = isinstance(result, dict) and result.get("error") == err
        if got != blocked:
            failures.append(f"{label}: expected {'blocked' if blocked else 'allowed'}, "
                            f"got {result.get('error') if isinstance(result, dict) else result!r}")

    with bridge_patch("DATA", data), _headless():
        expect("read settings.json", bridge.tool_read_file({"path": str(secret)}), True)
        expect("list data folder", bridge.tool_list_directory({"path": str(data)}), True)
        expect("write into data", bridge.tool_write_file(
            {"path": str(data / "new.txt"), "content": "x"}), True)
        expect("edit settings.json", bridge.tool_edit_file(
            {"path": str(secret), "edits": [{"old_text": "canary", "new_text": "x"}]}), True)
        expect("delete settings.json", bridge.tool_delete_file({"path": str(secret)}), True)
        expect("grep in data", bridge.tool_grep_files(
            {"path": str(data), "pattern": "token"}), True)
        expect("find files in data", bridge.tool_find_files({"path": str(data)}), True)
        expect("read a report", bridge.tool_read_file({"path": str(report)}), False)
        expect("write a report", bridge.tool_write_file(
            {"path": str(report), "content": "x", "force": True}), True)
        if bridge.is_in_workspace(str(secret)):
            failures.append("is_in_workspace: data folder counted as workspace")
        hits = bridge.tool_grep_files({"path": str(project), "pattern": "discord_bot_token"})
        paths = [m.get("path", "") for m in hits.get("matches", []) + hits.get("files", [])]
        if any("settings.json" in p for p in paths):
            failures.append("grep from the project folder returned data/settings.json")
        if not any("notes.txt" in p for p in paths):
            failures.append("grep from the project folder missed notes.txt (search broke)")
        found = bridge.tool_find_files({"path": str(project)}).get("matches", [])
        if any("settings.json" in m["path"] for m in found):
            failures.append("find_files from the project folder listed data/settings.json")
        # scan_secrets used to walk straight into the data folder.
        expect("scan_secrets on data", bridge.tool_scan_secrets({"path": str(data)}), True)

    # The MCP key file sits in the app folder, outside DATA.
    keys = project / "bridge_mcp_config.json"
    keys.write_text('{"mcpServers": {"x": {"env": {"API_KEY": "canary-fake-key"}}}}',
                    encoding="utf-8")
    example = project / "bridge_mcp_config.example.json"
    example.write_text('{"mcpServers": {}}', encoding="utf-8")
    with bridge_patch("DATA", data), bridge_patch("_MCP_CONFIG_FILE", keys), \
            _headless():
        expect("read the MCP key file", bridge.tool_read_file({"path": str(keys)}), True)
        expect("read the example MCP config", bridge.tool_read_file({"path": str(example)}), False)
        hits = bridge.tool_grep_files({"path": str(project), "pattern": "canary-fake-key"})
        if hits.get("matches") or hits.get("files"):
            failures.append("grep from the project folder found the MCP key file")
        found = bridge.tool_find_files({"path": str(project)}).get("matches", [])
        if any(m["path"].endswith("bridge_mcp_config.json") for m in found):
            failures.append("find_files listed the MCP key file")
        if not any(m["path"].endswith("example.json") for m in found):
            failures.append("find_files missed the example config (search broke)")
        scan = bridge.tool_scan_secrets({"path": str(project)})
        if any(f.get("file", "").endswith("bridge_mcp_config.json")
               for f in scan.get("findings", [])) or scan.get("files_scanned") != 1:
            failures.append(f"scan_secrets read the MCP key file or the data folder "
                            f"(scanned {scan.get('files_scanned')}, expected only the example)")
        if not bridge.is_data_path(str(bridge.ROOT / "bridge_mcp_config.json")):
            failures.append("the real app folder's bridge_mcp_config.json is not blocked")
        expect("write the MCP key file", bridge.tool_write_file(       # last: it would empty it
            {"path": str(keys), "content": "{}", "force": True}), True)
    if not secret.exists():
        failures.append("settings.json was deleted")
    if "canary-fake-key" not in keys.read_text(encoding="utf-8"):
        failures.append("the MCP key file was overwritten")
    return failures


def test_data_folder_is_blocked(tmp_path):
    failures = check_data_block(tmp_path)
    assert not failures, "; ".join(failures)


# ---- Soft mode: shell commands that touch the data folder ask --------------------

def _soft_cases(app: Path) -> tuple[list, list]:
    """(commands that must ask, commands that must still run) as
    (command, kind, run_cwd). `app` stands in for the app folder."""
    data, proj = app / "data", app / "proj"
    s = f"{data}\\settings.json"
    wsl = "/mnt/" + str(data)[0].lower() + str(data)[2:].replace("\\", "/")
    script = app / "run_canary.py"
    script.write_text(f"print(open(r'{s}').read())\n", encoding="utf-8")
    ask = [
        (f'Get-Content "{s}"', "powershell", None),
        (f"type {s}", "powershell", None),
        (f'cd "{data}"; Get-Content settings.json', "powershell", None),
        (f'Select-String -Path "{data}\\*.json" -Pattern token', "powershell", None),
        (f'Copy-Item "{s}" C:\\tmp\\x.json', "powershell", None),
        (f'Get-ChildItem "{data}"', "powershell", None),
        (f"python -c \"print(open(r'{s}').read())\"", "powershell", None),
        (f'& "python" -X utf8 "{script}"', "powershell", None),       # the run_python tool
        (f'cmd /c type "{s}"', "powershell", None),
        ("type data\\settings.json", "powershell", str(app)),
        ("Get-Content $env:ACCURETTA_DATA_DIR\\settings.json", "powershell", None),
        (f'Get-Content "{data}\\reports\\..\\settings.json"', "powershell", None),
        ("rg discord_bot_token", "powershell", str(app)),
        ("Get-ChildItem -Recurse | Select-String token", "powershell", str(app)),
        (f"grep -ri token {app}", "powershell", None),
        (f"cat {wsl}/settings.json", "sandbox", None),
        # The MCP key file in the app folder.
        (f'Get-Content "{app}\\bridge_mcp_config.json"', "powershell", None),
        ("type bridge_mcp_config.json", "powershell", str(app)),
        ("Get-Content .\\bridge*.json", "powershell", str(app)),
        ("Get-Content *", "powershell", str(app)),
        ("Copy-Item * C:\\backup", "powershell", str(app)),
        ("Get-ChildItem *.json | Get-Content", "powershell", str(app)),
        ("python -c \"print(open('bridge_mcp_config.json').read())\"", "powershell", str(app)),
        (f'Get-Content "{app}\\dat*\\settings.json"', "powershell", None),
    ]
    run = [
        (f'Get-Content "{data}\\reports\\report-1.md"', "powershell", None),
        ("Get-Content README.md", "powershell", str(proj)),
        ("rg TODO", "powershell", str(proj)),
        ("Get-ChildItem -Recurse -Filter *.py", "powershell", str(app)),
        ("git status", "powershell", str(app)),
        (f'Get-Content "{app}\\database\\x.txt"', "powershell", None),
        ("Get-Content bridge_mcp_config.example.json", "powershell", str(app)),
        ("Get-ChildItem *.json", "powershell", str(app)),
        ("Get-Content *.py", "powershell", str(proj)),
        ("Get-Content *.md", "powershell", str(app)),
    ]
    return ask, run


def check_soft_mode(tmp: Path) -> list[str]:
    app = tmp / "app"
    for sub in ("data/reports", "proj", "database"):
        (app / sub).mkdir(parents=True, exist_ok=True)
    ask, run = _soft_cases(app)
    failures = []
    with bridge_patch("DATA", app / "data"), \
            bridge_patch("_MCP_CONFIG_FILE", app / "bridge_mcp_config.json"):
        for expected, cases in ((True, ask), (False, run)):
            for cmd, kind, run_cwd in cases:
                asks = bool(bridge._soft_command_concerns(cmd, kind, run_cwd=run_cwd))
                if asks != expected:
                    failures.append(f"{'runs' if expected else 'asks'}: "
                                    f"{cmd.replace(str(app), '<APP>')[:80]}")
        # Through request_approval: the run folder reaches the check.
        events = []
        with bridge_patch("_approval_mode", return_value="soft"), \
                bridge_patch("_recent_web_taint", return_value=False), \
                bridge_patch("_chat_in_goal_mode", return_value=False), \
                bridge_patch("broadcast_event", side_effect=events.append):
            asked = bridge.request_approval(
                "Command on Windows host", "type data\\settings.json",
                {"kind": "powershell", "target": "host", "run_cwd": str(app)}, timeout_s=0)
            ran = bridge.request_approval(
                "Command on Windows host", "Get-Content README.md",
                {"kind": "powershell", "target": "host", "run_cwd": str(app / "proj")}, timeout_s=0)
        if asked.get("auto"):
            failures.append("request_approval auto-approved a relative read of data/settings.json")
        if not ran.get("auto"):
            failures.append("request_approval stopped a plain project read")
        if not bridge._goal_action_is_gated("powershell", f'Get-Content "{app}\\data\\settings.json"'):
            failures.append("/goal would run a read of the data folder without asking")
    return failures


@pytest.mark.skipif(not WINDOWS, reason="host paths in these cases are Windows paths")
def test_soft_mode_asks_before_touching_the_data_folder(tmp_path):
    failures = check_soft_mode(tmp_path)
    assert not failures, "; ".join(failures)


# ---- standalone report --------------------------------------------------------

def _compare(label: str, argv: list[str], keep) -> str:
    exe = shutil.which(argv[0])
    if not exe:
        return f"  SKIPPED    {label} ({argv[0]} not installed)"
    argv = [exe, *argv[1:]]
    try:
        before = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                env=dict(os.environ))
        after = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                               env=bridge._child_env(keep=keep))
    except Exception as exc:
        return f"  ERROR      {label} ({type(exc).__name__})"
    same = before.returncode == after.returncode and before.stdout.strip() == after.stdout.strip()
    return f"  {'SAME' if same else 'DIFFERENT':<10} {label}"


def main() -> int:
    bad = 0
    print("Environment leak canary (names only, never values)\n")
    with fake_environment():
        seen = probe_control()
        print(f"  {'LEAK' if CANARY in seen else 'NO LEAK':<16} control: plain child, no env= "
              f"({'expected: proves the canary works' if CANARY in seen else 'UNEXPECTED'})")
        if CANARY not in seen:
            bad += 1
        for target, probe in PROBES.items():
            try:
                status, notes = verdict(target, probe())
            except Exception as exc:
                status, notes = "ERROR", [f"{type(exc).__name__}: {exc}"]
            bad += status != "CLEAN"
            print(f"  {status:<16} {target}" + (f" ({'; '.join(notes)})" if notes else ""))

    print("\nEveryday tools, full environment vs the new one")
    tools = bridge._ENV_TOOLS
    for line in (
        _compare("git reads your name", ["git", "config", "--get", "user.name"], tools),
        _compare("git reads your email", ["git", "config", "--get", "user.email"], tools),
        _compare("npx runs", ["npx", "--version"], tools),
        _compare("node runs", ["node", "--version"], tools),
        _compare("pip runs", ["pip", "--version"], tools),
        _compare("PowerShell finds installed modules",
                 ["powershell", "-NoProfile", "-Command", "(Get-Module -ListAvailable).Count"], tools),
        _compare("nvidia-smi sees the GPU", ["nvidia-smi", "-L"], ("gpu",)),
    ):
        print(line)
        bad += line.lstrip().startswith(("DIFFERENT", "ERROR"))

    print("\nGhidra worker, real boot")
    if bridge._resolve_ghidra_install_dir():
        worker = bridge._GhidraWorker()
        try:
            err = worker._start(_ghidra_env_extra(2048), 2048)
        finally:
            worker.stop()
        print(f"  {'OK' if not err else 'FAILED':<10} boots with the filtered environment"
              + (f" ({err})" if err else ""))
        bad += bool(err)
    else:
        print("  SKIPPED    Ghidra is not installed")

    print("\nData folder block")
    with tempfile.TemporaryDirectory() as tmp:
        failures = check_data_block(Path(tmp))
    print("  OK         file tools refuse data/ and the MCP key file, reports stay readable" if not failures
          else "\n".join(f"  FAILED     {f}" for f in failures))
    bad += len(failures)

    print("\nSoft mode, shell commands, the data folder and the MCP key file")
    with tempfile.TemporaryDirectory() as tmp:
        failures = check_soft_mode(Path(tmp))
    print("  OK         commands touching data/ or the key file ask, normal project work still runs" if not failures
          else "\n".join(f"  FAILED     {f}" for f in failures))
    bad += len(failures)

    print("\nRESULT:", "ALL CLEAN" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
