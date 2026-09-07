import os
import sys
import unittest
import tempfile
from unittest.mock import MagicMock

# Mock dependencies not installed in host environment
for mod in ["yaml", "aiogram", "aiogram.utils", "aiogram.utils.executor", "aiogram.types", "telethon", "telethon.sessions", "qrcode", "aiohttp", "requests"]:
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


class TestEmergencyControls(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        database.DB_PATH = self.temp_db.name
        database.create_table()

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def test_set_all_channels_paused(self):
        # Add 65 channels
        for i in range(1, 66):
            database.add_channel(f"-100{i:09d}")

        self.assertEqual(database.get_active_channels_count(), 65)

        # 1. Engage Kill Switch (Pause All)
        affected = database.set_all_channels_paused(True)
        self.assertEqual(affected, 65)
        self.assertEqual(database.get_active_channels_count(), 0)

        for i in range(1, 66):
            self.assertTrue(database.is_channel_paused(f"-100{i:09d}"))

        # 2. Disengage Kill Switch (Resume All)
        resumed = database.set_all_channels_paused(False)
        self.assertEqual(resumed, 65)
        self.assertEqual(database.get_active_channels_count(), 65)

        for i in range(1, 66):
            self.assertFalse(database.is_channel_paused(f"-100{i:09d}"))

    def test_build_channels_keyboard_master_row(self):
        # Add 65 channels
        for i in range(1, 66):
            database.add_channel(f"-100{i:09d}")

        # When all active
        text, kb = bot.build_channels_keyboard(page=1)
        master_row = kb.inline_keyboard[0]
        self.assertEqual(len(master_row), 2)
        self.assertIn("🚨 Emergency Pause All (65)", master_row[0].text)
        self.assertEqual(master_row[0].callback_data, "cb:bulk_pause:1")
        self.assertIn("🟢 All Channels Active", master_row[1].text)

        # Pause all
        database.set_all_channels_paused(True)
        text_paused, kb_paused = bot.build_channels_keyboard(page=1)
        master_row_paused = kb_paused.inline_keyboard[0]
        self.assertIn("⏸️ All Channels Paused", master_row_paused[0].text)
        self.assertIn("▶️ Resume All (65)", master_row_paused[1].text)
        self.assertEqual(master_row_paused[1].callback_data, "cb:bulk_resume:1")

        # Verify callback length limit
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertLessEqual(len(btn.callback_data.encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
