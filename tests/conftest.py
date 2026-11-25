"""Suite-wide sandbox. pytest loads this before any test module, so bridge is
never imported against the real install.

bridge.py derives DATA from ACCURETTA_DATA_DIR at import time; pointing it at a
fresh temp dir here keeps settings.json, chats.json, logs, journals and
renders out of the user's data/ folder. An audit hook then makes the remaining
escape routes fail loudly instead of silently touching real state:

  * any open/mkdir/remove/rename/sqlite connect under the repo's data/ folder
  * reading the real bridge_mcp_config.json (holds API keys, starts servers)
  * launching llama-server
  * any non-loopback network traffic, and loopback traffic to the live
    bridge port (8787) — a real instance may be running a task there
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DATA = Path(tempfile.mkdtemp(prefix="accuretta-test-data-"))
os.environ["ACCURETTA_DATA_DIR"] = str(_TMP_DATA)
os.environ.pop("ACCURETTA_LLAMA_BIN", None)

from network_policy import local_address  # noqa: E402  (after the env is set)

_LIVE_PORT = 8787
_REAL_DATA = os.path.normcase(str(ROOT / "data"))
_REAL_MCP = os.path.normcase(str(ROOT / "bridge_mcp_config.json"))


class SandboxViolation(RuntimeError):
    pass


# bridge wraps a lot of I/O in broad try/except, which would swallow the
# exception; every violation is also recorded and fails the session at the end.
_violations = []


def _deny(message):
    current = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
    if current:
        message = f"{current}: {message}"
    if message not in _violations:
        _violations.append(message)
    raise SandboxViolation(message)


def _norm(path):
    if isinstance(path, int):
        return ""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return ""


def _in_real_data(path):
    p = _norm(path)
    return bool(p) and (p == _REAL_DATA or p.startswith(_REAL_DATA + os.sep))


def _is_llama_server(argv):
    if isinstance(argv, (str, bytes, os.PathLike)):
        argv = [argv]
    for arg in argv or []:
        try:
            name = os.path.basename(os.fsdecode(arg)).lower()
        except TypeError:
            continue
        if name.startswith("llama-server"):
            return True
    return False


def _guard(event, args):
    if event in {"open", "os.mkdir", "os.remove", "os.rmdir", "os.truncate", "sqlite3.connect"}:
        target = args[0] if args else None
        if target is None:
            return
        if _in_real_data(target):
            _deny(f"test touched the real data folder: {target}")
        if event == "open" and _norm(target) == _REAL_MCP:
            _deny("test read the real bridge_mcp_config.json")
    elif event in {"os.rename", "shutil.move", "shutil.rmtree", "shutil.copyfile"}:
        for target in args[:2]:
            if isinstance(target, (str, bytes, os.PathLike)) and _in_real_data(target):
                _deny(f"test touched the real data folder: {target}")
    elif event == "subprocess.Popen":
        if _is_llama_server(args[1]) or _is_llama_server(args[0]):
            _deny("test tried to launch llama-server")
    elif event == "socket.getaddrinfo":
        if args[0] not in (None, "", "0.0.0.0", "::") and not local_address(args[0]):
            _deny(f"test tried to resolve {args[0]!r}")
    elif event in {"socket.connect", "socket.sendto"}:
        address = args[1] if event == "socket.connect" else args[-1]
        if not isinstance(address, tuple) or len(address) < 2:
            return
        if not local_address(address[0]):
            _deny(f"test tried to reach {address[0]}:{address[1]}")
        if address[1] == _LIVE_PORT:
            _deny("test tried to reach the live bridge on port 8787")


sys.addaudithook(_guard)

import bridge  # noqa: E402

assert bridge.DATA == _TMP_DATA.resolve(), f"bridge.DATA escaped the sandbox: {bridge.DATA}"

def _set_everywhere(name, value):
    """Set `name` on bridge and on every accuretta module that has it. Code moved
    out of bridge.py looks names up in its own module, so setting it on bridge
    alone would leave the moved code with the real value."""
    setattr(bridge, name, value)
    for mod_name, module in list(sys.modules.items()):
        if (module is not None and (mod_name == "accuretta" or mod_name.startswith("accuretta."))
                and name in getattr(module, "__dict__", {})):
            setattr(module, name, value)


# auto_tune probes `llama-server --help` through find_llama_bin(), which picks
# up whatever build is on this machine's PATH. Pretend there is none, so a dev
# box behaves like CI; tests that need caps patch llama_caps themselves.
_set_everywhere("find_llama_bin", lambda: "")

# The real MCP config holds API keys and names servers to spawn; give the
# suite an empty one. Tests that exercise MCP patch this to their own file.
_set_everywhere("_MCP_CONFIG_FILE", _TMP_DATA / "bridge_mcp_config.json")


def pytest_sessionfinish(session, exitstatus):
    if _violations:
        print("\n\nSANDBOX VIOLATIONS (the suite tried to escape its sandbox):", file=sys.stderr)
        for message in _violations:
            print("  - " + message, file=sys.stderr)
        session.exitstatus = 1


def pytest_unconfigure(config):
    shutil.rmtree(_TMP_DATA, ignore_errors=True)
