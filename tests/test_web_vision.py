import io
import shutil
import tempfile
import unittest
from pathlib import Path

import bridge
try:
    from PIL import Image
except ImportError:  # Pillow is optional; skip these tests without it
    raise unittest.SkipTest("Pillow isn't installed")


def _png_bytes(w=20, h=10, color=(200, 30, 90)):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class WebVisionToolTests(unittest.TestCase):
    def test_tools_registered_with_fns(self):
        self.assertIn("web_view_image", bridge.TOOLS)
        self.assertIn("web_render", bridge.TOOLS)
        self.assertTrue(callable(bridge.TOOLS["web_view_image"]["fn"]))
        self.assertTrue(callable(bridge.TOOLS["web_render"]["fn"]))

    def test_mime_sniffing(self):
        self.assertEqual(bridge._sniff_image_mime(_png_bytes()), "png")
        self.assertEqual(bridge._sniff_image_mime(b"RIFF1234WEBPVP8 "), "webp")
        self.assertEqual(bridge._sniff_image_mime(b"GIF89a..."), "gif")
        self.assertEqual(bridge._sniff_image_mime(b"not an image"), "")

    def test_normalize_converts_and_reports_size(self):
        b64, w, h = bridge._normalize_image_bytes(_png_bytes(w=3000, h=500))
        self.assertTrue(b64)
        self.assertEqual((w, h), (3000, 500))
        # result decodes back to a valid PNG
        raw = __import__("base64").b64decode(b64)
        out = Image.open(io.BytesIO(raw))
        self.assertEqual(out.format, "PNG")
        self.assertLessEqual(max(out.size), 1600)

    def test_view_image_refuses_without_native_vision(self):
        # this test machine has no mmproj-equipped llama-server running, so the
        # native gate must refuse BEFORE any network activity
        if bridge._native_vision_ready():
            self.skipTest("native vision is actually running")
        result = bridge.TOOLS["web_view_image"]["fn"]({"url": "https://example.com/x.png"})
        self.assertFalse(result.get("ok"))
        self.assertIn("native vision", result.get("error", ""))

    def test_view_image_validates_url_first(self):
        # even with vision ready, a bad URL is refused without network
        result = bridge.TOOLS["web_view_image"]["fn"]({"url": "ftp://example.com/x.png"})
        self.assertFalse(result.get("ok"))
        self.assertIn("http", result.get("error", ""))

    def test_render_worker_dependency_gate(self):
        result = bridge.TOOLS["web_render"]["fn"]({"url": "https://example.com"})
        # either playwright is missing (dependency gate) or it works — never a crash
        self.assertIsInstance(result, dict)
        if "error" in result:
            self.assertIn("playwright", str(result.get("dependency_missing", "playwright")).lower())

    def test_new_tools_in_balanced_core(self):
        self.assertIn("web_view_image", bridge._BALANCED_CORE_TOOL_NAMES)
        self.assertIn("web_render", bridge._BALANCED_CORE_TOOL_NAMES)
        self.assertIn("web_view_image", bridge._TOOL_BUNDLES["web"])
        self.assertIn("web_render", bridge._TOOL_BUNDLES["web"])


class WatchVideoToolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_video_tool_registered(self):
        self.assertIn("web_watch_video", bridge.TOOLS)
        self.assertTrue(callable(bridge.TOOLS["web_watch_video"]["fn"]))
        self.assertIn("web_watch_video", bridge._BALANCED_CORE_TOOL_NAMES)
        self.assertIn("web_watch_video", bridge._TOOL_BUNDLES["web"])

    def test_watch_video_refuses_without_native_vision(self):
        if bridge._native_vision_ready():
            self.skipTest("native vision is actually running")
        result = bridge.TOOLS["web_watch_video"]["fn"]({"url": "https://example.com/video"})
        self.assertFalse(result.get("ok"))
        self.assertIn("native vision", result.get("error", ""))

    def test_extract_frames_from_real_clip(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg not installed")
        clip = Path(self.tempdir.name) / "clip.mp4"
        frame_dir = Path(self.tempdir.name) / "frames"
        frame_dir.mkdir()
        # 4-second clip, hard cut red -> blue at the 2s mark
        rc, _out, err = bridge._run_process_capture(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:size=320x180:duration=2",
                "-f", "lavfi", "-i", "color=c=blue:size=320x180:duration=2",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1[outv]",
                "-map", "[outv]", "-y", str(clip),
            ],
            timeout=120,
        )
        self.assertEqual(rc, 0, f"clip generation failed: {err[:300]}")
        frames, times = bridge._extract_video_frames(ffmpeg, clip, frame_dir, max_frames=8, duration=4.0)
        self.assertTrue(frames, "no frames extracted")
        self.assertLessEqual(len(frames), 8)
        self.assertEqual(len(times), len(frames))
        for frame in frames:
            img = Image.open(frame)
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.size, (320, 180))

    def test_uniform_fallback_when_no_cuts(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg not installed")
        clip = Path(self.tempdir.name) / "slow.mp4"
        frame_dir = Path(self.tempdir.name) / "frames"
        frame_dir.mkdir()
        # 6 seconds of one unchanging color: no scene cuts at all
        rc, _out, err = bridge._run_process_capture(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=green:size=320x180:duration=6",
             "-y", str(clip)],
            timeout=120,
        )
        self.assertEqual(rc, 0, f"clip generation failed: {err[:300]}")
        frames, times = bridge._extract_video_frames(ffmpeg, clip, frame_dir, max_frames=6, duration=6.0)
        self.assertEqual(len(frames), 6, "uniform fallback should produce the full grid")
        self.assertEqual(len(times), 6)
        stamps = [s for s in times if s is not None]
        self.assertEqual(len(stamps), 6, "uniform fallback should have arithmetic timestamps")
        self.assertAlmostEqual(stamps[-1], 5.0, delta=0.3)

    def test_ytdlp_probe_cache_shape(self):
        cmd = bridge._resolve_ytdlp_cmd()
        self.assertIsInstance(cmd, list)
        if cmd:
            rc, out, _err = bridge._run_process_capture([*cmd, "--version"], timeout=30)
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
