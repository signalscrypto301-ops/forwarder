import os, sys, unittest
from unittest.mock import AsyncMock, MagicMock, patch

for mod in ["yaml","aiogram","aiogram.utils","aiogram.utils.executor","aiogram.types",
            "telethon","telethon.sessions","qrcode","aiohttp","requests","psutil"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

aiogram_mod = sys.modules["aiogram"]
mock_dp = MagicMock()
mock_dp.message_handler = lambda *a, **k: (lambda fn: fn)
mock_dp.callback_query_handler = lambda *a, **k: (lambda fn: fn)
mock_dp.channel_post_handler = lambda *a, **k: (lambda fn: fn)
aiogram_mod.Dispatcher = MagicMock(return_value=mock_dp)

if "bot" in sys.modules:
    del sys.modules["bot"]

import bot

class TestGetChatId(unittest.IsolatedAsyncioTestCase):
    async def test_1_missing_args(self):
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 111
        msg.get_args.return_value = ""
        msg.reply = AsyncMock()

        with patch.object(bot, "is_admin", return_value=True):
            await bot.get_chat_id_command(msg)

        msg.reply.assert_called_once()
        self.assertIn("Please provide a group or channel name", msg.reply.call_args[0][0])

    async def test_2_single_channel_found(self):
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 111
        msg.get_args.return_value = "FOREX"
        msg.reply = AsyncMock()

        mock_res = (200, {
            "groupId": "120363420111598085@newsletter",
            "name": "Bitcoin crypto Forex gold & Stock traders",
            "isChannel": True
        })

        with patch.object(bot, "is_admin", return_value=True):
            with patch.object(bot, "post_whatsapp_json", AsyncMock(return_value=mock_res)):
                await bot.get_chat_id_command(msg)

        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("120363420111598085@newsletter", reply_text)
        self.assertIn("Bitcoin crypto Forex gold", reply_text)

    async def test_3_multiple_matches_found(self):
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 111
        msg.get_args.return_value = "<FOREX>"
        msg.reply = AsyncMock()

        mock_res = (200, {
            "groupId": "120363420111598085@newsletter",
            "name": "Bitcoin crypto Forex gold & Stock traders",
            "isChannel": True,
            "matches": [
                {"groupId": "120363420111598085@newsletter", "name": "Bitcoin crypto Forex gold & Stock traders", "isChannel": True},
                {"groupId": "120363409066684829@g.us", "name": "Forex VIP Signals", "isGroup": True}
            ]
        })

        with patch.object(bot, "is_admin", return_value=True):
            with patch.object(bot, "post_whatsapp_json", AsyncMock(return_value=mock_res)):
                await bot.get_chat_id_command(msg)

        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Found 2 matching chats", reply_text)
        self.assertIn("Bitcoin crypto Forex", reply_text)
        self.assertIn("Forex VIP Signals", reply_text)

    async def test_4_not_found_provides_helpful_tips(self):
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 111
        msg.get_args.return_value = "NonExistentChannel"
        msg.reply = AsyncMock()

        mock_res = (404, {"message": "Group or channel not found"})

        with patch.object(bot, "is_admin", return_value=True):
            with patch.object(bot, "post_whatsapp_json", AsyncMock(return_value=mock_res)):
                await bot.get_chat_id_command(msg)

        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Tips to find your WhatsApp channel", reply_text)
        self.assertIn("Paste the channel link directly", reply_text)

if __name__ == "__main__":
    unittest.main(verbosity=2)
