"""show_image: the agent shows the user a picture inline in the chat.

Owner's choice (2026-11-24): any picture on the PC, no asking; it is only shown to
the user. Never Accuretta's own data folder, only real image files. A copy goes to
DATA/chat-media under a content-hash name; the chat loads it from /api/chat-media/
and the reply keeps the list (_images) so a reopened chat still shows it.
"""
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import bridge
from _source import app_source, frontend_source
from accuretta.tools import show_image as si

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


def _png(w=40, h=30, color=(20, 120, 220)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipIf(Image is None, "Pillow is not installed")
class ShowImageToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)     # outside every workspace folder on purpose

    def _file(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_shows_a_picture_from_anywhere(self):
        p = self._file("holiday.png", _png())
        out = si.tool_show_image({"path": str(p)})
        self.assertTrue(out.get("ok"), out)
        img = out["image"]
        self.assertRegex(img["url"], r"^/api/chat-media/[0-9a-f]{24}\.png$")
        self.assertEqual(img["caption"], "holiday.png")
        self.assertEqual((img["width"], img["height"]), (40, 30))
        stored = si.chat_media_file(img["url"].rsplit("/", 1)[-1])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.read_bytes(), p.read_bytes())
        self.assertNotIn("base64", json.dumps(out))   # the model never gets the pixels

    def test_caption_and_one_copy_per_picture(self):
        data = _png(color=(1, 2, 3))
        a = si.tool_show_image({"path": str(self._file("a.png", data)), "caption": "  Login   page "})
        b = si.tool_show_image({"path": str(self._file("b.png", data))})
        self.assertEqual(a["image"]["caption"], "Login page")
        self.assertEqual(a["image"]["url"], b["image"]["url"])

    def test_huge_picture_is_shrunk(self):
        out = si.tool_show_image({"path": str(self._file("wide.png", _png(4000, 100)))})
        self.assertEqual(out["image"]["width"], 2560)
        self.assertEqual(out["image"]["height"], 64)

    def test_refuses_what_is_not_a_picture(self):
        out = si.tool_show_image({"path": str(self._file("fake.png", b"not an image at all"))})
        self.assertIn("not a picture", out.get("error", ""))
        self.assertIn("not a file", si.tool_show_image({"path": str(self.dir / "missing.png")}).get("error", ""))
        self.assertIn("give `path`", si.tool_show_image({}).get("error", ""))

    def test_never_the_data_folder(self):
        inside = Path(bridge.DATA) / "show-image-test.png"
        inside.write_bytes(_png())
        self.addCleanup(lambda: inside.unlink(missing_ok=True))
        out = si.tool_show_image({"path": str(inside)})
        self.assertNotIn("ok", out)
        self.assertEqual(out.get("error"), bridge.DATA_PATH_ERROR)


class KeptWithTheReplyTests(unittest.TestCase):
    def test_reads_pictures_back_from_the_turn(self):
        good = {"ok": True, "image": {"url": "/api/chat-media/" + "a" * 24 + ".png", "caption": "x", "width": 3, "height": 4}}
        msgs = [
            {"role": "tool", "name": "show_image", "content": json.dumps(good)},
            {"role": "tool", "name": "read_file", "content": json.dumps(good)},
            {"role": "tool", "name": "show_image", "content": json.dumps({"image": {"url": "/api/chat-media/../settings.json"}})},
            {"role": "tool", "name": "show_image", "content": "not json"},
            {"role": "assistant", "content": "hi"},
        ]
        self.assertEqual(si._shown_images(msgs), [good["image"]])

    def test_reply_keeps_them(self):
        src = app_source()
        self.assertIn("_shown = _shown_images(appended)", src)
        self.assertIn('msg["_images"] = _shown', src)

    def test_page_draws_them_live_and_after_reopening(self):
        js = frontend_source()
        self.assertIn('${m.role === "assistant" ? shownImagesHtml(m._images) : ""}', js)
        self.assertIn('evt.name === "show_image" && evt.result?.image', js)
        self.assertIn("msg._images = row._shownImages.slice(-12)", js)
        self.assertIn(r"const SHOWN_IMAGE_URL = /^\/api\/chat-media\/[0-9a-f]{24}\.(?:png|jpg|gif|webp)$/;", js)


class ToolSetupTests(unittest.TestCase):
    def test_registered_and_offered(self):
        self.assertIn("show_image", bridge.TOOLS)
        self.assertIn("show_image", bridge._BALANCED_CORE_TOOL_NAMES)
        self.assertIn("show_image", bridge._CORE_TOOL_NAMES)


@unittest.skipIf(Image is None, "Pillow is not installed")
class ChatMediaRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = bridge.AccurettaHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        finally:
            conn.close()

    def test_serves_a_shown_picture(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.png"
            p.write_bytes(_png(color=(9, 9, 9)))
            url = si.tool_show_image({"path": str(p)})["image"]["url"]
        status, ctype, body = self._get(url)
        self.assertEqual((status, ctype), (200, "image/png"))
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_nothing_else_is_reachable(self):
        for bad in ("/api/chat-media/settings.json", "/api/chat-media/..%2Fsettings.json",
                    "/api/chat-media/" + "a" * 24 + ".png.txt", "/api/chat-media/" + "g" * 24 + ".png"):
            status, _, _ = self._get(bad)
            self.assertEqual(status, 404, bad)


if __name__ == "__main__":
    unittest.main()
