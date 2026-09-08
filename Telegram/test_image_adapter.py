import os
import sys
import unittest
from unittest.mock import MagicMock
from collections import namedtuple
from PIL import Image

# Mock dependencies not installed in host environment before importing bot
for mod in [
    "yaml",
    "aiogram",
    "aiogram.utils",
    "aiogram.utils.executor",
    "aiogram.types",
    "telethon",
    "telethon.sessions",
    "qrcode",
    "aiohttp",
    "requests",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

aiogram_mod = sys.modules.get("aiogram")
if aiogram_mod:
    mock_dp = MagicMock()
    mock_dp.channel_post_handler = lambda *a, **k: (lambda fn: fn)
    mock_dp.message_handler = lambda *a, **k: (lambda fn: fn)
    mock_dp.callback_query_handler = lambda *a, **k: (lambda fn: fn)
    aiogram_mod.Dispatcher = MagicMock(return_value=mock_dp)
    aiogram_mod.Bot = MagicMock()

class MockInlineKeyboardMarkup:
    def __init__(self, row_width=1):
        self.inline_keyboard = []
        self.row_width = row_width

    def add(self, *buttons):
        for b in buttons:
            self.inline_keyboard.append([b])

    def row(self, *buttons):
        self.inline_keyboard.append(list(buttons))


class MockInlineKeyboardButton:
    def __init__(self, text="", callback_data=""):
        self.text = text
        self.callback_data = callback_data


class MockContentType:
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    ANIMATION = "animation"
    DOCUMENT = "document"
    VOICE = "voice"
    AUDIO = "audio"

aiogram_types = sys.modules.get("aiogram.types")
if aiogram_types:
    aiogram_types.ContentType = MockContentType
    aiogram_types.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_types.InlineKeyboardButton = MockInlineKeyboardButton

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

import config
import bot

MessageEntity = namedtuple("MessageEntity", ["type", "offset", "length", "url"], defaults=[None])


class TestImageAdapterAndCaptionFormatting(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(os.path.dirname(__file__), "test_media_tmp")
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            for f in os.listdir(self.test_dir):
                try:
                    os.remove(os.path.join(self.test_dir, f))
                except Exception:
                    pass
            try:
                os.rmdir(self.test_dir)
            except Exception:
                pass

    def test_landscape_banner_solid_black_adapted_to_square(self):
        """Wide landscape banner (2.65:1) with black edges should be padded to 1:1 square with black."""
        img_path = os.path.join(self.test_dir, "banner_black.jpg")
        img = Image.new("RGB", (400, 150), (0, 0, 0))
        for x in range(50, 350):
            for y in range(30, 120):
                img.putpixel((x, y), (255, 215, 0))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        self.assertEqual(res_path, img_path)

        with Image.open(res_path) as adapted:
            w, h = adapted.size
            self.assertEqual(w, 400)
            self.assertEqual(h, 400)
            top_pixel = adapted.getpixel((200, 10))
            self.assertEqual(top_pixel, (0, 0, 0))
            bottom_pixel = adapted.getpixel((200, 390))
            self.assertEqual(bottom_pixel, (0, 0, 0))
            center_pixel = adapted.getpixel((200, 200))
            self.assertTrue(all(abs(a - b) <= 2 for a, b in zip(center_pixel, (255, 215, 0))))

    def test_landscape_banner_solid_white_adapted_to_square(self):
        """Wide landscape banner with white edges should be padded with white."""
        img_path = os.path.join(self.test_dir, "banner_white.jpg")
        img = Image.new("RGB", (300, 100), (255, 255, 255))
        for x in range(50, 250):
            for y in range(25, 75):
                img.putpixel((x, y), (0, 128, 255))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        with Image.open(res_path) as adapted:
            w, h = adapted.size
            self.assertEqual(w, 300)
            self.assertEqual(h, 300)
            top_pixel = adapted.getpixel((150, 10))
            self.assertEqual(top_pixel, (255, 255, 255))

    def test_landscape_scenic_image_adapted_with_blur(self):
        """Wide landscape with non-uniform edge colors should use blurred background."""
        img_path = os.path.join(self.test_dir, "banner_scenic.jpg")
        img = Image.new("RGB", (300, 100))
        for x in range(300):
            for y in range(100):
                img.putpixel((x, y), ((x * 2) % 255, (y * 3) % 255, (x + y) % 255))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        with Image.open(res_path) as adapted:
            w, h = adapted.size
            self.assertEqual(w, 300)
            self.assertEqual(h, 300)

    def test_square_image_remains_untouched(self):
        """Image that is already square (1:1) should NOT be modified."""
        img_path = os.path.join(self.test_dir, "square.jpg")
        img = Image.new("RGB", (300, 300), (100, 150, 200))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        self.assertEqual(res_path, img_path)
        with Image.open(res_path) as adapted:
            self.assertEqual(adapted.size, (300, 300))

    def test_standard_portrait_remains_untouched(self):
        """Standard 4:5 portrait image (ratio ~0.80) should NOT be modified."""
        img_path = os.path.join(self.test_dir, "portrait.jpg")
        img = Image.new("RGB", (400, 500), (50, 50, 50))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        with Image.open(res_path) as adapted:
            self.assertEqual(adapted.size, (400, 500))

    def test_extreme_tall_portrait_adapted_to_4_5(self):
        """Extremely tall screenshot (ratio 0.40) should be padded to 4:5."""
        img_path = os.path.join(self.test_dir, "tall.jpg")
        img = Image.new("RGB", (200, 500), (0, 0, 0))
        img.save(img_path, "JPEG")

        res_path = bot.adapt_image_for_whatsapp_channel(img_path)
        with Image.open(res_path) as adapted:
            w, h = adapted.size
            self.assertEqual(h, 500)
            self.assertEqual(w, 400)

    def test_caption_formatting_no_double_asterisks(self):
        """Test that overlapping bold entities do NOT produce double asterisks."""
        text = "🔔 🔴 NEW #XAG SELL SIGNAL 📉"
        utf16_offset_xag = len(text[:text.index("#XAG")].encode("utf-16-le")) // 2
        utf16_len_xag = len("#XAG".encode("utf-16-le")) // 2
        utf16_len_total = len(text[text.index("#XAG"):].encode("utf-16-le")) // 2

        e1 = MessageEntity(type="bold", offset=utf16_offset_xag, length=utf16_len_xag)
        e2 = MessageEntity(type="bold", offset=utf16_offset_xag, length=utf16_len_total)

        formatted = bot.format_caption_for_whatsapp(text, [e1, e2])
        self.assertNotIn("**", formatted)
        self.assertEqual(formatted, "🔔 🔴 NEW *#XAG SELL SIGNAL 📉*")

    def test_caption_formatting_whitespace_trimming(self):
        """Test that whitespace inside bold entity is trimmed so WhatsApp renders it correctly."""
        text = "Check out Google Play Store"
        start_idx = text.index(" Google")
        utf16_offset = len(text[:start_idx].encode("utf-16-le")) // 2
        utf16_len = len(" Google ".encode("utf-16-le")) // 2

        e = MessageEntity(type="bold", offset=utf16_offset, length=utf16_len)
        formatted = bot.format_caption_for_whatsapp(text, [e])
        self.assertNotIn("* Google", formatted)
        self.assertNotIn("Google *", formatted)
        self.assertEqual(formatted, "Check out *Google* Play Store")


if __name__ == "__main__":
    unittest.main()
