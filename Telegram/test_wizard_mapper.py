import os
import sys
import unittest
import tempfile
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

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


class TestWizardMapper(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        database.DB_PATH = self.temp_db.name
        database.create_table()

        # Reset bot's chats cache
        bot.WHATSAPP_CHATS_CACHE["timestamp"] = 0.0
        bot.WHATSAPP_CHATS_CACHE["chats"] = []

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def test_fetch_whatsapp_chats(self):
        # Mock post_whatsapp_json response
        mock_chats = [
            {"id": "120363001@g.us", "name": "Crypto Signals", "type": "group"},
            {"id": "120363002@newsletter", "name": "Tech News Daily", "type": "newsletter"},
            {"id": "120363003@g.us", "name": "VIP Trading", "type": "group"},
        ]

        async def run_test():
            with patch.object(bot, "post_whatsapp_json", new=AsyncMock(return_value=(200, {"chats": mock_chats}))):
                chats = await bot.fetch_whatsapp_chats()
                self.assertEqual(len(chats), 3)
                self.assertEqual(chats[0]["name"], "Crypto Signals")

                # Test search filtering
                crypto_filtered = await bot.fetch_whatsapp_chats(search_query="crypto")
                self.assertEqual(len(crypto_filtered), 1)
                self.assertEqual(crypto_filtered[0]["id"], "120363001@g.us")

                # Test cache reuse
                cached = await bot.fetch_whatsapp_chats()
                self.assertEqual(len(cached), 3)

        asyncio.run(run_test())

    def test_build_group_mapper_keyboard(self):
        channel_id = "-1002568318126"
        database.add_channel(channel_id)
        # Already map one group
        database.add_group_for_channel(channel_id, "120363001@g.us")

        mock_chats = [
            {"id": "120363001@g.us", "name": "Crypto Signals", "type": "group"},
            {"id": "120363002@newsletter", "name": "Tech News Daily", "type": "newsletter"},
            {"id": "120363003@g.us", "name": "VIP Trading", "type": "group"},
        ]

        async def run_test():
            with patch.object(bot, "fetch_whatsapp_chats", new=AsyncMock(return_value=mock_chats)):
                text, kb = await bot.build_group_mapper_keyboard(channel_id, wizard_page=1, channel_page=1, per_page=2)

                self.assertIn("WhatsApp Group Mapper Wizard", text)
                self.assertIn(channel_id, text)

                # Page 1 has 2 items
                # Row 0: 120363001@g.us is mapped -> should have '✅' and 'u:' callback
                btn_0 = kb.inline_keyboard[0][0]
                self.assertIn("✅", btn_0.text)
                self.assertIn("(Mapped)", btn_0.text)
                self.assertEqual(btn_0.callback_data, f"u:{channel_id}:120363001@g.us")

                # Row 1: 120363002@newsletter is unmapped -> should have '➕' and 'b:' callback
                btn_1 = kb.inline_keyboard[1][0]
                self.assertIn("➕", btn_1.text)
                self.assertIn("Tech News Daily", btn_1.text)
                self.assertEqual(btn_1.callback_data, f"b:{channel_id}:120363002@newsletter")

                # Check Telegram 64-byte limit across all buttons
                for row in kb.inline_keyboard:
                    for btn in row:
                        self.assertLessEqual(
                            len(btn.callback_data.encode("utf-8")),
                            64,
                            f"Button {btn.text} has callback_data exceeding 64 bytes: {btn.callback_data}"
                        )

        asyncio.run(run_test())

    def test_bind_and_unbind_callbacks(self):
        channel_id = "-1002568318126"
        group_id = "120363099999999999@g.us"
        database.add_channel(channel_id)

        # 1. Bind
        self.assertNotIn(group_id, database.get_groups_for_channel(channel_id))
        database.add_group_for_channel(channel_id, group_id)
        self.assertIn(group_id, database.get_groups_for_channel(channel_id))

        # 2. Unbind
        database.delete_group_for_channel(channel_id, group_id)
        self.assertNotIn(group_id, database.get_groups_for_channel(channel_id))

    def test_channel_detail_keyboard_has_wizard_button_and_unmap(self):
        channel_id = "-1002568318126"
        database.add_channel(channel_id)
        database.add_group_for_channel(channel_id, "120363001@g.us")
        database.add_group_for_channel(channel_id, "120363002@newsletter")

        text, kb = bot.build_channel_detail_keyboard(channel_id, page=1)

        self.assertIn("Channel:", text)
        self.assertIn("Mapped WhatsApp Destinations (2)", text)

        # Check that '➕ Map WhatsApp Destination' is present
        button_texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Map WhatsApp Destination" in t for t in button_texts))

        # Check that unmap buttons exist for each mapped group
        unmap_buttons = [btn for row in kb.inline_keyboard for btn in row if btn.callback_data.startswith("u:")]
        self.assertEqual(len(unmap_buttons), 2)
        self.assertEqual(unmap_buttons[0].callback_data, f"u:{channel_id}:120363001@g.us")
        self.assertEqual(unmap_buttons[1].callback_data, f"u:{channel_id}:120363002@newsletter")

        # Verify 64-byte callback limit
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertLessEqual(len(btn.callback_data.encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
