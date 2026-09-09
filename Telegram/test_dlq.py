import os
import sys
import unittest
import tempfile
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Mock dependencies not installed in host environment
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


aiogram_mod = sys.modules.get("aiogram")
if aiogram_mod:
    aiogram_mod.types.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_mod.types.InlineKeyboardButton = MockInlineKeyboardButton

aiogram_types_mod = sys.modules.get("aiogram.types")
if aiogram_types_mod:
    aiogram_types_mod.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_types_mod.InlineKeyboardButton = MockInlineKeyboardButton

import database
import bot

bot.InlineKeyboardMarkup = MockInlineKeyboardMarkup
bot.InlineKeyboardButton = MockInlineKeyboardButton


class TestDeadLetterQueue(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        database.DB_PATH = self.temp_db.name
        database.create_table()

        # Temporary DLQ media directory
        self.temp_dlq_dir = tempfile.TemporaryDirectory()
        self.orig_dlq_dir = bot.DLQ_MEDIA_DIR
        bot.DLQ_MEDIA_DIR = self.temp_dlq_dir.name

    def tearDown(self):
        bot.DLQ_MEDIA_DIR = self.orig_dlq_dir
        self.temp_dlq_dir.cleanup()
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def test_database_crud_operations(self):
        """Verify inserting, retrieving, counting, deleting, and clearing DLQ items in SQLite."""
        self.assertEqual(database.get_failed_messages_count(), 0)

        # 1. Add video failure (size limit)
        id1 = database.add_failed_message(
            channel_id="-100111",
            group_id="12036301@g.us",
            group_name="Crypto VIP",
            content_type="video",
            caption="Bitcoin breakout analysis",
            original_filename="crypto_update.mp4",
            media_path=None,
            reason="Size limit",
            file_size_bytes=105 * 1024 * 1024,
        )

        # 2. Add text failure (connection timeout)
        id2 = database.add_failed_message(
            channel_id="-100222",
            group_id="12036302@g.us",
            group_name="Forex Signals",
            content_type="text",
            caption="EUR/USD long at 1.0850",
            reason="Connection timeout",
        )

        # 3. Add photo failure (rate limit)
        id3 = database.add_failed_message(
            channel_id="-100333",
            group_id="12036303@newsletter",
            group_name="News Hub",
            content_type="photo",
            caption="Morning brief chart",
            original_filename="chart.jpg",
            reason="WhatsApp rate limit",
            file_size_bytes=450 * 1024,
        )

        self.assertEqual(database.get_failed_messages_count(), 3)

        items = database.get_failed_messages()
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["id"], id1)
        self.assertEqual(items[0]["content_type"], "video")
        self.assertEqual(items[0]["reason"], "Size limit")
        self.assertEqual(items[0]["file_size_bytes"], 105 * 1024 * 1024)

        self.assertEqual(items[1]["id"], id2)
        self.assertEqual(items[1]["content_type"], "text")
        self.assertEqual(items[1]["reason"], "Connection timeout")

        self.assertEqual(items[2]["id"], id3)
        self.assertEqual(items[2]["content_type"], "photo")
        self.assertEqual(items[2]["reason"], "WhatsApp rate limit")

        # Delete single item
        database.delete_failed_message(id2)
        self.assertEqual(database.get_failed_messages_count(), 2)

        # Clear remaining items
        database.clear_failed_messages()
        self.assertEqual(database.get_failed_messages_count(), 0)

    def test_build_failed_queue_keyboard_formatting(self):
        """Verify UI matches the exact user format and callbacks are <= 64 bytes."""
        # Insert 3 messages matching user prompt
        database.add_failed_message(
            channel_id="-100111",
            group_id="12036301@g.us",
            group_name="Crypto VIP",
            content_type="video",
            caption="Daily market wrap",
            reason="Size limit",
            file_size_bytes=105 * 1024 * 1024,
        )
        database.add_failed_message(
            channel_id="-100222",
            group_id="12036302@g.us",
            group_name="Forex Signals",
            content_type="text",
            caption="Buy signal",
            reason="Connection timeout",
        )
        database.add_failed_message(
            channel_id="-100333",
            group_id="12036303@newsletter",
            group_name="News Hub",
            content_type="photo",
            caption="Breaking News",
            reason="WhatsApp rate limit",
            file_size_bytes=800 * 1024,  # < 1MB so no size suffix
        )

        content, keyboard = bot.build_failed_queue_keyboard()

        # Check header
        self.assertIn("Failed Messages Queue (3 items)", content)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━━━", content)

        # Check line 1: Video (105MB) -> Crypto VIP (Size limit)
        self.assertIn("1. Video (105MB) -> Crypto VIP (Size limit)", content)

        # Check line 2: Text -> Forex Signals (Connection timeout)
        self.assertIn("2. Text -> Forex Signals (Connection timeout)", content)

        # Check line 3: Photo -> News Hub (WhatsApp rate limit)
        self.assertIn("3. Photo -> News Hub (WhatsApp rate limit)", content)

        # Verify buttons & callback data limits
        all_buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        button_texts = [btn.text for btn in all_buttons]
        self.assertIn("🔄 Retry All Failed", button_texts)
        self.assertIn("🗑️ Purge Queue", button_texts)
        self.assertIn("🔄 Refresh", button_texts)

        for btn in all_buttons:
            self.assertLessEqual(
                len(btn.callback_data.encode("utf-8")),
                64,
                f"Callback data too long: {btn.callback_data}",
            )

    def test_empty_queue_formatting(self):
        """Verify UI when DLQ is empty."""
        content, keyboard = bot.build_failed_queue_keyboard()
        self.assertIn("Failed Messages Queue (0 items)", content)
        self.assertIn("Dead-Letter Queue is empty", content)

    def test_retry_all_failed_messages(self):
        """Verify re-dispatching, successful pruning, and handling persistent failures."""
        # Create a test media file
        temp_media = os.path.join(self.temp_dlq_dir.name, "dlq_test_photo.jpg")
        with open(temp_media, "w") as f:
            f.write("dummy media content")

        # 1. Successful retry item (Text)
        id_success = database.add_failed_message(
            channel_id="-1001",
            group_id="group_success@g.us",
            group_name="Forex Signals",
            content_type="text",
            caption="Test retry success",
            reason="Connection timeout",
        )

        # 2. Successful retry item (Photo with media)
        id_photo = database.add_failed_message(
            channel_id="-1002",
            group_id="group_photo@g.us",
            group_name="News Hub",
            content_type="photo",
            caption="Photo update",
            original_filename="photo.jpg",
            media_path=temp_media,
            reason="WhatsApp rate limit",
        )

        # 3. Failing item
        id_fail = database.add_failed_message(
            channel_id="-1003",
            group_id="group_fail@g.us",
            group_name="Crypto VIP",
            content_type="text",
            caption="Test retry fail",
            reason="WhatsApp service error",
        )

        async def mock_send(group, downloaded_media, caption, original_filename, content_type, channel_id, is_retry):
            if "fail" in group:
                return False
            return True

        async def run_retry():
            with patch.object(bot, "send_to_single_group", side_effect=mock_send):
                recovered, failed = await bot.retry_all_failed_messages()
                return recovered, failed

        recovered, failed = asyncio.run(run_retry())

        self.assertEqual(recovered, 2)
        self.assertEqual(failed, 1)

        # Remaining in DB should only be id_fail
        remaining = database.get_failed_messages()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], id_fail)

        # Media file should have been removed after successful photo dispatch
        self.assertFalse(os.path.exists(temp_media))


if __name__ == "__main__":
    unittest.main()
