# SPDX-License-Identifier: AGPL-3.0-only
"""The environment a child process gets: built from nothing, with only the names its
group of tools needs, so secrets in Accuretta's own environment stay out.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os


# What any program needs to start, find its files and reach the network.
# SYSTEMROOT must stay: without it Windows networking fails in the child.
_CHILD_ENV_BASE = (
    # Windows
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemDrive", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432",
    "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS", "DriverData", "PSModulePath",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    # Accuretta's user folder (installer builds), so its own helper scripts
    # (email, notch) find the user's files. A path, never a secret.
    "ACCURETTA_HOME",
    # macOS and Linux
    "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "DISPLAY", "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_*",
    # Language
    "LANG", "LANGUAGE", "LC_*",
)


_CHILD_ENV_PROXY = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")


# Extra groups a call site opts into with keep=(...).
_CHILD_ENV_GROUPS = {
    # Proxy plus company certificate files, so traffic through a corporate
    # proxy still verifies. "network_public" is the same list minus any proxy
    # value that carries a login (http://user:pass@host): for third-party code.
    "network": _CHILD_ENV_PROXY + (
        "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "PIP_CERT"),
    # Where developer tools live. Folder paths and switches, never secrets.
    "dev": (
        "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
        "PYTHONUTF8", "PYTHONIOENCODING", "JAVA_HOME", "GOPATH", "GOROOT",
        "CARGO_HOME", "RUSTUP_HOME", "NVM_HOME", "NVM_SYMLINK", "ANDROID_HOME",
        "DOTNET_ROOT"),
    # SSH agent and git's ssh command. These let a program sign in with the
    # user's SSH keys, so they never go to third-party code (MCP servers).
    "ssh": ("SSH_AUTH_SOCK", "GIT_SSH", "GIT_SSH_COMMAND"),
    # Which GPU llama-server uses and how its runtime behaves.
    "gpu": (
        "CUDA_*", "HIP_*", "ROCR_*", "HSA_*", "GPU_DEVICE_ORDINAL", "GGML_*",
        "VULKAN_SDK", "VK_*", "ONEAPI_*", "SYCL_*", "ZES_*", "OMP_*"),
}


_CHILD_ENV_GROUPS["network_public"] = _CHILD_ENV_GROUPS["network"]


# The usual set for tools the agent runs on the user's behalf (shell, Python,
# git, tests, linters, decompilers, programs it opens).
_ENV_TOOLS = ("network", "dev", "ssh")


def _env_name_matches(name: str, patterns) -> bool:
    upper = name.upper()
    for pattern in patterns:
        pattern = pattern.upper()
        if pattern.endswith("*"):
            if upper.startswith(pattern[:-1]):
                return True
        elif upper == pattern:
            return True
    return False


def _child_env(extra: dict | None = None, *, keep: tuple[str, ...] = ()) -> dict:
    """Environment for a child process, built from an empty dict.

    Copies the base names, then the names of each group in `keep` (see
    _CHILD_ENV_GROUPS), then applies `extra` on top. Nothing else from the
    bridge's environment is passed on."""
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if _env_name_matches(name, _CHILD_ENV_BASE):
            env[name] = value
    for group in keep:
        patterns = _CHILD_ENV_GROUPS[group]
        for name, value in os.environ.items():
            if not _env_name_matches(name, patterns):
                continue
            if (group == "network_public" and "@" in value
                    and _env_name_matches(name, _CHILD_ENV_PROXY)):
                continue        # proxy with a login: third-party code doesn't get it
            env[name] = value
    for name, value in (extra or {}).items():
        if value is not None:
            env[str(name)] = str(value)
    return env
