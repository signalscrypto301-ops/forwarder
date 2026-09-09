import os
import sys
import unittest
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(__file__))

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
    "psutil",
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


aiogram_types_mod = sys.modules.get("aiogram.types")
if aiogram_types_mod:
    aiogram_types_mod.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_types_mod.InlineKeyboardButton = MockInlineKeyboardButton

import database
import bot


class TestChannelDeactivateAndUnlink(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = self.temp_db.name
        bot.InlineKeyboardMarkup = MockInlineKeyboardMarkup
        bot.InlineKeyboardButton = MockInlineKeyboardButton
        database.create_table()
        self.test_cid = "-1001234567890"

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except Exception:
                pass

    def test_database_deactivate_and_activate(self):
        """Test database helper functions deactivate_channel and activate_channel."""
        database.add_channel(self.test_cid)
        self.assertFalse(database.is_channel_paused(self.test_cid))

        # Deactivate
        res = database.deactivate_channel(self.test_cid)
        self.assertTrue(res)
        self.assertTrue(database.is_channel_paused(self.test_cid))

        # Activate
        res = database.activate_channel(self.test_cid)
        self.assertTrue(res)
        self.assertFalse(database.is_channel_paused(self.test_cid))

    def test_database_unlink_single_group(self):
        """Test unlinking a single group while keeping other groups."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "news1@newsletter")

        self.assertEqual(len(database.get_groups_for_channel(self.test_cid)), 2)

        # Unlink only grp1@g.us
        count = database.unlink_channel(self.test_cid, "grp1@g.us")
        self.assertEqual(count, 1)

        remaining = database.get_groups_for_channel(self.test_cid)
        self.assertEqual(remaining, ["news1@newsletter"])

        # Channel itself must remain registered
        self.assertIn(self.test_cid, database.get_all_channels())

    def test_database_unlink_all_groups(self):
        """Test unlinking all groups from a channel at once."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "grp2@g.us")
        database.add_group_for_channel(self.test_cid, "news1@newsletter")

        self.assertEqual(len(database.get_groups_for_channel(self.test_cid)), 3)

        # Unlink all
        count = database.unlink_channel(self.test_cid)
        self.assertEqual(count, 3)

        # 0 groups remain
        self.assertEqual(len(database.get_groups_for_channel(self.test_cid)), 0)

        # Channel itself remains in channels table
        self.assertIn(self.test_cid, database.get_all_channels())

    def test_build_channel_detail_keyboard_deactivate_and_unlink(self):
        """Test that detail keyboard displays correct badges, buttons, and unlink options."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "news1@newsletter")

        # 1. Active channel
        text, kb = bot.build_channel_detail_keyboard(self.test_cid, page=1)
        self.assertIn("ACTIVE", text)
        buttons = [b for row in kb.inline_keyboard for b in row]

        # Toggle button should say Deactivate / Pause
        toggle_btn = next((b for b in buttons if "Deactivate" in b.text or "Pause" in b.text), None)
        self.assertIsNotNone(toggle_btn)
        self.assertIn("pause", toggle_btn.callback_data)

        # Unlink all button should be present because > 1 group mapped
        unlink_all_btn = next((b for b in buttons if "Unlink All Destinations" in b.text), None)
        self.assertIsNotNone(unlink_all_btn)
        self.assertEqual(unlink_all_btn.callback_data, f"cb:unlink_all_confirm:{self.test_cid}:1")

        # Individual unlink buttons should exist
        unlink_grp_btn = next((b for b in buttons if "grp1@g.us" in b.text and "Unlink" in b.text), None)
        self.assertIsNotNone(unlink_grp_btn)

        # 2. Deactivated channel
        database.deactivate_channel(self.test_cid)
        text_paused, kb_paused = bot.build_channel_detail_keyboard(self.test_cid, page=1)
        self.assertIn("DEACTIVATED", text_paused)
        buttons_paused = [b for row in kb_paused.inline_keyboard for b in row]
        toggle_resume_btn = next((b for b in buttons_paused if "Activate" in b.text or "Resume" in b.text), None)
        self.assertIsNotNone(toggle_resume_btn)
        self.assertIn("unpause", toggle_resume_btn.callback_data)

    def test_build_unlink_confirm_keyboard(self):
        """Test build_unlink_confirm_keyboard builds confirmation dialog correctly."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "grp2@g.us")

        text, kb = bot.build_unlink_confirm_keyboard(self.test_cid, page=1)
        self.assertIn("Confirm Unlink All Destinations", text)
        self.assertIn(self.test_cid, text)

        buttons = [b for row in kb.inline_keyboard for b in row]
        confirm_btn = next((b for b in buttons if "Yes, Unlink All" in b.text), None)
        cancel_btn = next((b for b in buttons if "Cancel" in b.text), None)

        self.assertIsNotNone(confirm_btn)
        self.assertEqual(confirm_btn.callback_data, f"cb:unlink_all_exec:{self.test_cid}:1")
        self.assertIsNotNone(cancel_btn)
        self.assertEqual(cancel_btn.callback_data, f"cb:view:{self.test_cid}:1")

    async def test_deactivate_channel_command(self):
        """Test /deactivate command execution."""
        database.add_channel(self.test_cid)
        self.assertFalse(database.is_channel_paused(self.test_cid))

        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 12345
        msg.get_args.return_value = self.test_cid
        msg.reply = AsyncMock()

        with patch("handlers.channels._is_admin", return_value=True):
            await bot.deactivate_channel_command(msg)

        self.assertTrue(database.is_channel_paused(self.test_cid))
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Channel Deactivated", reply_text)
        self.assertIn(self.test_cid, reply_text)

    async def test_activate_channel_command(self):
        """Test /activate command execution."""
        database.add_channel(self.test_cid)
        database.deactivate_channel(self.test_cid)
        self.assertTrue(database.is_channel_paused(self.test_cid))

        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 12345
        msg.get_args.return_value = self.test_cid
        msg.reply = AsyncMock()

        with patch("handlers.channels._is_admin", return_value=True):
            await bot.activate_channel_command(msg)

        self.assertFalse(database.is_channel_paused(self.test_cid))
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Channel Activated", reply_text)
        self.assertIn(self.test_cid, reply_text)

    async def test_unlink_command_all_and_single(self):
        """Test /unlink command with single group and all groups."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "grp2@g.us")

        # 1. Unlink single
        msg_single = MagicMock()
        msg_single.chat.type = "private"
        msg_single.from_user.id = 12345
        msg_single.get_args.return_value = f"{self.test_cid} grp1@g.us"
        msg_single.reply = AsyncMock()

        with patch("handlers.channels._is_admin", return_value=True):
            await bot.unlink_command(msg_single)

        self.assertEqual(database.get_groups_for_channel(self.test_cid), ["grp2@g.us"])
        msg_single.reply.assert_called_once()
        self.assertIn("Destination Unlinked", msg_single.reply.call_args[0][0])

        # 2. Unlink all remaining
        msg_all = MagicMock()
        msg_all.chat.type = "private"
        msg_all.from_user.id = 12345
        msg_all.get_args.return_value = self.test_cid
        msg_all.reply = AsyncMock()

        with patch("handlers.channels._is_admin", return_value=True):
            await bot.unlink_command(msg_all)

        self.assertEqual(len(database.get_groups_for_channel(self.test_cid)), 0)
        msg_all.reply.assert_called_once()
        self.assertIn("Channel Unlinked", msg_all.reply.call_args[0][0])

    async def test_callbacks_toggle_and_unlink(self):
        """Test callback query actions for toggle, unlink_one, and unlink_all."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.add_group_for_channel(self.test_cid, "grp2@g.us")

        call = MagicMock()
        call.from_user.id = 12345
        call.answer = AsyncMock()
        call.message.edit_text = AsyncMock()

        with patch("handlers.channels._is_admin", return_value=True):
            # 1. Toggle to pause
            call.data = f"cb:toggle:{self.test_cid}:pause:1"
            await bot.handle_channels_callbacks(call)
            self.assertTrue(database.is_channel_paused(self.test_cid))

            # 2. Toggle to unpause
            call.data = f"cb:toggle:{self.test_cid}:unpause:1"
            await bot.handle_channels_callbacks(call)
            self.assertFalse(database.is_channel_paused(self.test_cid))

            # 3. Unlink one group
            call.data = f"cb:unlink_one:{self.test_cid}:grp1@g.us:1"
            await bot.handle_channels_callbacks(call)
            self.assertEqual(database.get_groups_for_channel(self.test_cid), ["grp2@g.us"])

            # 4. Unlink all confirmation dialog
            call.data = f"cb:unlink_all_confirm:{self.test_cid}:1"
            await bot.handle_channels_callbacks(call)
            call.message.edit_text.assert_called()
            edit_text = call.message.edit_text.call_args[0][0]
            self.assertIn("Confirm Unlink All Destinations", edit_text)

            # 5. Unlink all execution
            call.data = f"cb:unlink_all_exec:{self.test_cid}:1"
            await bot.handle_channels_callbacks(call)
            self.assertEqual(len(database.get_groups_for_channel(self.test_cid)), 0)

    async def test_handle_channel_post_drops_deactivated_channel(self):
        """Verify that handle_channel_post drops posts from deactivated/paused channels."""
        database.add_channel(self.test_cid)
        database.add_group_for_channel(self.test_cid, "grp1@g.us")
        database.deactivate_channel(self.test_cid)

        msg = MagicMock()
        msg.chat.id = int(self.test_cid)
        msg.message_id = 777
        msg.content_type = "text"
        msg.text = "Signal: Buy BTC"
        msg.entities = []
        msg.video = None
        msg.video_note = None
        msg.animation = None
        msg.document = None

        send_mock = AsyncMock()
        with patch.object(bot, "send_to_single_group", send_mock):
            await bot.handle_channel_post(msg)
            # Post should be dropped, send_to_single_group never called
            send_mock.assert_not_called()

    async def test_handle_channel_post_drops_unlinked_channel(self):
        """Verify that handle_channel_post skips when channel has 0 mapped groups."""
        database.add_channel(self.test_cid)
        database.unlink_channel(self.test_cid)  # 0 groups

        msg = MagicMock()
        msg.chat.id = int(self.test_cid)
        msg.message_id = 778
        msg.content_type = "text"
        msg.text = "Signal: Sell ETH"
        msg.entities = []
        msg.video = None
        msg.video_note = None
        msg.animation = None
        msg.document = None

        send_mock = AsyncMock()
        with patch.object(bot, "send_to_single_group", send_mock):
            await bot.handle_channel_post(msg)
            send_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
