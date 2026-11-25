import base64
import io
import unittest

try:
    from PIL import Image
except ImportError:  # Pillow is optional; skip these tests without it
    raise unittest.SkipTest("Pillow isn't installed")

import bridge


def _data_url(image: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    image.save(buf, fmt)
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def _decode(url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


@unittest.skipUnless(bridge._HAVE_PIL, "Pillow not available")
class ImageClampTests(unittest.TestCase):
    """Every image that reaches the vision model is bounded on its long edge."""

    def test_large_png_is_clamped_and_stays_png(self):
        url = _data_url(Image.new("RGB", (3000, 1500), "white"))
        out = bridge._clamp_image_data_url(url)
        img = _decode(out)
        self.assertEqual(max(img.size), bridge.IMAGE_WIRE_MAX_EDGE)
        self.assertTrue(out.startswith("data:image/png"))
        self.assertLess(len(out), len(url))

    def test_large_jpeg_is_clamped_and_stays_jpeg(self):
        url = _data_url(Image.new("RGB", (2000, 3000), "navy"), "JPEG")
        out = bridge._clamp_image_data_url(url)
        img = _decode(out)
        self.assertEqual(max(img.size), bridge.IMAGE_WIRE_MAX_EDGE)
        self.assertTrue(out.startswith("data:image/jpeg"))

    def test_small_image_passes_through_untouched(self):
        url = _data_url(Image.new("RGB", (640, 480), "red"))
        self.assertEqual(bridge._clamp_image_data_url(url), url)

    def test_clamp_results_are_cached(self):
        url = _data_url(Image.new("RGB", (2400, 1200), "white"))
        first = bridge._clamp_image_data_url(url)
        self.assertIn(first, bridge._clamped_image_cache.values())
        self.assertEqual(bridge._clamp_image_data_url(url), first)

    def test_non_images_and_garbage_pass_through(self):
        self.assertIsNone(bridge._clamp_image_data_url(None))
        self.assertEqual(bridge._clamp_image_data_url("data:text/plain;base64,aGk="),
                         "data:text/plain;base64,aGk=")
        self.assertEqual(bridge._clamp_image_data_url("data:image/png;base64,not-real"),
                         "data:image/png;base64,not-real")


if __name__ == "__main__":
    unittest.main()
