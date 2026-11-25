# SPDX-License-Identifier: AGPL-3.0-only
"""Pulling frames out of a video with ffmpeg so a model can look at them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import subprocess
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from pathlib import Path


def _run_process_capture(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_child_env(keep=_ENV_TOOLS),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _extract_video_frames(
    ffmpeg_bin: str,
    video_path: Path,
    out_dir: Path,
    max_frames: int,
    duration: float,
) -> tuple[list[Path], list[float | None]]:
    """Extract up to `max_frames` frames, sampling where the picture changes.

    Pass 1 is ffmpeg scene detection (timestamped via showinfo). Falls back to
    a uniform grid when the clip has no scene cuts (static/slow content), and
    evenly subsamples when the clip is cut-heavy."""
    pattern = str(out_dir / "scene_%03d.png")
    rc, _out, err = _run_process_capture(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "info", "-i", str(video_path),
            "-vf", "select='gt(scene,0.3)',showinfo", "-frames:v", str(min(max_frames + 8, 24)),
            "-vsync", "vfr", "-y", pattern,
        ],
        timeout=180,
    )
    times = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", err)]
    frames = sorted(out_dir.glob("scene_*.png"))
    if not frames:
        # no scene cuts detected — uniform grid so slow/static content is covered
        fps = max(0.05, max_frames / max(1.0, duration))
        rc, _out, _err = _run_process_capture(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                "-vf", f"fps={fps:.4f}", "-frames:v", str(max_frames),
                "-vsync", "vfr", "-y", pattern,
            ],
            timeout=180,
        )
        frames = sorted(out_dir.glob("scene_*.png"))
        times = []
        if frames:
            step = 1.0 / fps
            times = [index * step for index in range(len(frames))]
    if len(frames) > max_frames:
        if max_frames == 1:
            frames = [frames[len(frames) // 2]]
        else:
            keep = sorted({int(round(i * (len(frames) - 1) / (max_frames - 1))) for i in range(max_frames)})
            frames = [frames[i] for i in keep if i < len(frames)]
    if len(times) < len(frames):
        times = list(times) + [None] * (len(frames) - len(times))
    return frames, times[:len(frames)]
