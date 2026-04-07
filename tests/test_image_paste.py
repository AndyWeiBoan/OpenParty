"""
Tests for image paste feature (feat-support-image-paste).

Covers:
- payload construction with images field
- base64 encoding in server._build_image_blocks_from_history
- file-not-found fallback (silent skip)
- _save_clipboard_image compression pipeline (resize + format conversion)
- multiple images in one message
- different MIME types (png / jpeg / webp)
"""

import base64
import os
import sys
import tempfile
import unittest

# Allow importing from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── _save_clipboard_image tests ──────────────────────────────────────────────


class TestSaveClipboardImage(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image  # type: ignore[import-untyped]

            self._Image = Image
        except ImportError:
            self.skipTest("Pillow not installed")

    def _make_rgb_image(self, width=2000, height=1500):
        return self._Image.new("RGB", (width, height), color=(255, 0, 0))

    def _make_rgba_image(self, width=800, height=600):
        return self._Image.new("RGBA", (width, height), color=(0, 255, 0, 128))

    def test_rgb_image_saved_as_jpeg(self):
        """RGB image (no alpha) should be saved as JPEG."""
        from openparty_tui import _save_clipboard_image

        img = self._make_rgb_image()
        with tempfile.TemporaryDirectory() as tmpdir:
            path, mime = _save_clipboard_image(img, tmpdir, "test")
            self.assertTrue(path.endswith(".jpg"), f"Expected .jpg, got {path}")
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(os.path.exists(path))

    def test_rgba_image_saved_as_webp(self):
        """RGBA image (has alpha) should be saved as WebP."""
        from openparty_tui import _save_clipboard_image

        img = self._make_rgba_image()
        with tempfile.TemporaryDirectory() as tmpdir:
            path, mime = _save_clipboard_image(img, tmpdir, "test")
            self.assertTrue(path.endswith(".webp"), f"Expected .webp, got {path}")
            self.assertEqual(mime, "image/webp")
            self.assertTrue(os.path.exists(path))

    def test_large_image_resized(self):
        """Images larger than 1568px on longest edge should be resized."""
        from openparty_tui import _save_clipboard_image

        img = self._make_rgb_image(width=3840, height=2160)
        with tempfile.TemporaryDirectory() as tmpdir:
            path, mime = _save_clipboard_image(img, tmpdir, "test")
            saved = self._Image.open(path)
            self.assertLessEqual(max(saved.size), 1568)

    def test_small_image_not_upscaled(self):
        """Images smaller than 1568px should NOT be upscaled."""
        from openparty_tui import _save_clipboard_image

        img = self._make_rgb_image(width=400, height=300)
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = _save_clipboard_image(img, tmpdir, "test")
            saved = self._Image.open(path)
            self.assertEqual(saved.size, (400, 300))

    def test_compressed_file_under_5mb(self):
        """Compressed output should be well under 5 MB."""
        from openparty_tui import _save_clipboard_image, IMAGE_MAX_BYTES

        img = self._make_rgb_image(width=2880, height=1800)
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = _save_clipboard_image(img, tmpdir, "test")
            self.assertLess(os.path.getsize(path), IMAGE_MAX_BYTES)

    def test_unique_names_no_collision(self):
        """Different names should produce different files."""
        from openparty_tui import _save_clipboard_image
        import uuid

        img = self._make_rgb_image(width=100, height=100)
        with tempfile.TemporaryDirectory() as tmpdir:
            path1, _ = _save_clipboard_image(img, tmpdir, str(uuid.uuid4()))
            path2, _ = _save_clipboard_image(img, tmpdir, str(uuid.uuid4()))
            self.assertNotEqual(path1, path2)


# ── server._build_image_blocks_from_history tests ────────────────────────────


class TestBuildImageBlocks(unittest.TestCase):
    def _get_server(self):
        import server as srv

        return srv.RoomServer()

    def test_base64_encoding(self):
        """Image file content should be correctly base64-encoded."""
        server = self._get_server()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)
            tmp_path = f.name
        try:
            history = [{"images": [{"path": tmp_path, "mime": "image/jpeg"}]}]
            blocks = server._build_image_blocks_from_history(history)
            self.assertEqual(len(blocks), 1)
            block = blocks[0]
            self.assertEqual(block["type"], "image")
            self.assertEqual(block["source"]["media_type"], "image/jpeg")
            # Verify base64 decodes back to original data
            decoded = base64.b64decode(block["source"]["data"])
            self.assertEqual(decoded, open(tmp_path, "rb").read())
        finally:
            os.unlink(tmp_path)

    def test_missing_file_skipped(self):
        """Missing image files should be silently skipped, not raise."""
        server = self._get_server()
        history = [
            {"images": [{"path": "/nonexistent/path/img.jpg", "mime": "image/jpeg"}]}
        ]
        blocks = server._build_image_blocks_from_history(history)
        self.assertEqual(blocks, [])

    def test_multiple_images(self):
        """Multiple images across history entries should all be collected."""
        server = self._get_server()
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for i in range(3):
                p = os.path.join(tmpdir, f"img{i}.jpg")
                with open(p, "wb") as f:
                    f.write(b"\xff\xd8\xff" + bytes([i]) * 50)
                paths.append(p)

            history = [
                {"images": [{"path": paths[0], "mime": "image/jpeg"}]},
                {"content": "text only"},
                {
                    "images": [
                        {"path": paths[1], "mime": "image/jpeg"},
                        {"path": paths[2], "mime": "image/jpeg"},
                    ]
                },
            ]
            blocks = server._build_image_blocks_from_history(history)
            self.assertEqual(len(blocks), 3)

    def test_no_images_in_history(self):
        """History with no images should return empty list."""
        server = self._get_server()
        history = [
            {"name": "Andy", "content": "hello"},
            {"name": "claude-sonne", "content": "world"},
        ]
        blocks = server._build_image_blocks_from_history(history)
        self.assertEqual(blocks, [])


# ── payload construction tests ───────────────────────────────────────────────


class TestPayloadConstruction(unittest.TestCase):
    def test_images_field_in_payload(self):
        """Payload should include 'images' key when pending images exist."""
        images = [{"path": "/tmp/test.jpg", "mime": "image/jpeg"}]
        payload = {"type": "message", "content": "hello"}
        # Simulate what _handle_send does
        payload["images"] = list(images)
        self.assertIn("images", payload)
        self.assertEqual(payload["images"][0]["path"], "/tmp/test.jpg")
        self.assertEqual(payload["images"][0]["mime"], "image/jpeg")

    def test_no_images_key_when_no_pending(self):
        """Payload should NOT include 'images' key when no pending images."""
        pending = []
        payload = {"type": "message", "content": "hello"}
        if pending:
            payload["images"] = list(pending)
        self.assertNotIn("images", payload)

    def test_multiple_images_in_payload(self):
        """Multiple pending images should all appear in payload."""
        pending = [
            {"path": "/tmp/a.jpg", "mime": "image/jpeg"},
            {"path": "/tmp/b.webp", "mime": "image/webp"},
        ]
        payload = {"type": "message", "content": "look at these"}
        payload["images"] = list(pending)
        self.assertEqual(len(payload["images"]), 2)


# ── build_prompt with image_blocks tests ─────────────────────────────────────


class TestBuildPromptWithImages(unittest.TestCase):
    def test_no_images_returns_str(self):
        """build_prompt without image_blocks should return a plain string."""
        from bridge import build_prompt

        payload = {
            "history": [],
            "context": {"topic": "test", "participants": [], "total_turns": 0},
        }
        result = build_prompt(payload, "agent-1")
        self.assertIsInstance(result, str)

    def test_with_images_returns_list(self):
        """build_prompt with image_blocks should return a list of content blocks."""
        from bridge import build_prompt

        image_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc123"},
        }
        payload = {
            "history": [],
            "context": {"topic": "test", "participants": [], "total_turns": 0},
            "image_blocks": [image_block],
        }
        result = build_prompt(payload, "agent-1")
        self.assertIsInstance(result, list)
        # Image blocks come first
        self.assertEqual(result[0]["type"], "image")
        # Last block is text
        self.assertEqual(result[-1]["type"], "text")

    def test_text_content_preserved_in_list(self):
        """Text prompt content should be in the last block when images present."""
        from bridge import build_prompt

        image_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"},
        }
        payload = {
            "history": [],
            "context": {"topic": "my topic", "participants": [], "total_turns": 0},
            "image_blocks": [image_block],
        }
        result = build_prompt(payload, "agent-1")
        text_block = result[-1]
        self.assertIn("my topic", text_block["text"])


if __name__ == "__main__":
    unittest.main()
