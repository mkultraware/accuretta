# SPDX-License-Identifier: AGPL-3.0-only
"""The separate vision server: its llama-server process and starting it when the main
model can't see.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.endpoints import LLAMA
from accuretta.core.safe_fs import safe_exists
from accuretta.core.state import STATE
from accuretta.runtime.llama_process import LlamaProcess
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.model_files import find_mmproj_for
from accuretta.security.agent_processes import _parse_llama_port
from pathlib import Path


_vision_llama = LlamaProcess()


def _start_vision_fallback_if_needed(s: dict) -> None:
    v_model = (s.get("vision_model") or "").strip()
    if os.environ.get("VISION_LLAMA_HOST"):
        return
    # Clear the side server address when fallback vision is removed or the main
    # model has native vision; a stale URL would block the next image turn.
    if _llama.is_vision_capable() or not v_model or not safe_exists(v_model):
        try:
            _vision_llama.stop_permanent()
        except Exception:
            pass
        STATE.vision_llama = LLAMA
        return
    
    main_port = _parse_llama_port()
    v_port = main_port + 1
    
    v_stem = Path(v_model).stem.lower()
    if "qwen2-vl" in v_stem or "qwen2.5-vl" in v_stem or "pixtral" in v_stem or "llava" in v_stem:
        v_mmproj = ""
    else:
        try:
            v_mmproj = find_mmproj_for(v_model)
        except Exception:
            v_mmproj = ""
        
    print(f"  spawning vision fallback server with {os.path.basename(v_model)} on port {v_port} ...")
    res = _vision_llama.start(
        model_path=v_model,
        wait=False,
        wait_seconds=60,
        port_override=v_port,
        mmproj_override=v_mmproj,
        ctx_override=4096,
        ngl_override=0
    )
    if res.get("ok"):
        STATE.vision_llama = f"http://127.0.0.1:{v_port}"
        print(f"  vision fallback: started (pid {res.get('pid')}) at {STATE.vision_llama}")
    else:
        print(f"  [warn] vision fallback didn't start: {res.get('error')}")
