# SPDX-License-Identifier: AGPL-3.0-only
"""What the installed llama-server supports, read from its --help once per binary.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess
from accuretta.security.child_env import _child_env


# ---- llama-server flag capability probe --------------------------------------
# Builds understand different flags, and an unknown flag makes llama-server
# exit at startup (--n-cpu-moe, the ngram flag rename, draft-mtp,
# rope-scaling). Probe --help once per binary and gate every optional flag on
# the answer.
_LLAMA_CAPS_CACHE: dict = {}


def llama_caps(bin_path: str = "") -> dict:
    """Probe a llama-server binary's --help and report which optional flags it
    understands. Cached per (path, mtime). On probe failure assume everything
    is supported — identical to the pre-probe behavior."""
    caps = {"n_cpu_moe": True, "override_tensor": True, "ngram_mod": False,
            "ngram_legacy": False, "draft_mtp": True, "dflash": False,
            "dspark": False, "rope_scaling": True,
            "flash_attn": True, "probed": False,
            # Context checkpoints default off: unlike the flags above (True for
            # pre-probe parity), they are new enough that assuming them on an old binary
            # would make the server exit at boot.
            "ctx_checkpoints": False, "checkpoint_min_step": False,
            "checkpoint_every_n": False,
            # Newer flags: fail-closed until seen in --help (an unknown flag
            # hard-exits the server at boot).
            "moe_cache": False, "fit_target": False, "no_mmproj_offload": False}
    if not bin_path:
        return caps
    try:
        mtime = os.path.getmtime(bin_path)
    except Exception:
        mtime = 0
    key = (bin_path, mtime)
    if key in _LLAMA_CAPS_CACHE:
        return _LLAMA_CAPS_CACHE[key]
    try:
        r = subprocess.run(
            [bin_path, "--help"], capture_output=True, text=True, timeout=8,
            env=_child_env(keep=("gpu",)),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        help_txt = (r.stdout or "") + "\n" + (r.stderr or "")
        if help_txt.strip():
            caps["probed"] = True
            caps["n_cpu_moe"] = ("--n-cpu-moe" in help_txt) or ("ncmoe" in help_txt)
            caps["override_tensor"] = ("--override-tensor" in help_txt) or (" -ot" in help_txt)
            caps["ngram_mod"] = "--spec-ngram-mod-n-match" in help_txt
            # Removed flags still appear in --help annotated "the argument has
            # been removed" — exclude those so old builds keep their working
            # legacy names and new builds don't think the old flag is real.
            caps["ngram_legacy"] = ("--spec-ngram-size-n" in help_txt) and (
                "the argument has been removed" not in help_txt)
            caps["draft_mtp"] = "draft-mtp" in help_txt
            # Self-speculative decoding types (llama.cpp #21930): DFlash / DSpark draft
            # from the model's own hidden states. Fail-closed until seen in --help, since
            # an unknown --spec-type value makes the server exit at boot.
            caps["dflash"] = "dflash" in help_txt
            caps["dspark"] = "dspark" in help_txt
            caps["rope_scaling"] = "--rope-scaling" in help_txt
            caps["flash_attn"] = ("--flash-attn" in help_txt) or ("-fa" in help_txt)
            caps["ctx_checkpoints"] = ("--ctx-checkpoints" in help_txt) or ("--swa-checkpoints" in help_txt)
            # llama.cpp renamed --checkpoint-every-n-tokens → --checkpoint-min-step
            caps["checkpoint_min_step"] = "--checkpoint-min-step" in help_txt
            caps["checkpoint_every_n"] = "--checkpoint-every-n-tokens" in help_txt
            caps["moe_cache"] = "--moe-cache-mib" in help_txt
            caps["fit_target"] = "--fit-target" in help_txt
            # How to load the weights without keeping a memory-mapped copy:
            # newer builds take --load-mode, older ones --no-mmap.
            caps["load_mode"] = "--load-mode" in help_txt
            caps["no_mmap"] = "--no-mmap" in help_txt
            caps["no_mmproj_offload"] = "--no-mmproj-offload" in help_txt
    except Exception:
        pass
    _LLAMA_CAPS_CACHE[key] = caps
    return caps
