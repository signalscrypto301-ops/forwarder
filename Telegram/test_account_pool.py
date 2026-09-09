import unittest
import asyncio
import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from unittest.mock import MagicMock, AsyncMock, patch

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

sys.modules["aiogram.types"].InlineKeyboardMarkup = MockInlineKeyboardMarkup
sys.modules["aiogram.types"].InlineKeyboardButton = MockInlineKeyboardButton

from account_pool import AccountPool, CONFIGURED_ACCOUNTS, ID_TO_INDEX, ACCOUNT_INDICES


class TestAccountPool(unittest.TestCase):
    def setUp(self):
        self.pool = AccountPool()

    def test_id_resolution(self):
        self.assertEqual(self.pool.resolve_account_id("1"), "user")
        self.assertEqual(self.pool.resolve_account_id(1), "user")
        self.assertEqual(self.pool.resolve_account_id("account1"), "user")
        self.assertEqual(self.pool.resolve_account_id("user"), "user")
        self.assertEqual(self.pool.resolve_account_id("2"), "account2")
        self.assertEqual(self.pool.resolve_account_id(2), "account2")
        self.assertEqual(self.pool.resolve_account_id("account2"), "account2")
        self.assertEqual(self.pool.resolve_account_id("3"), "account3")
        self.assertEqual(self.pool.resolve_account_id(3), "account3")
        self.assertEqual(self.pool.resolve_account_id("4"), "account4")
        self.assertEqual(self.pool.resolve_account_id(4), "account4")
        # Default fallback
        self.assertEqual(self.pool.resolve_account_id(None), "user")
        self.assertEqual(self.pool.resolve_account_id("unknown"), "user")

    def test_index_mapping(self):
        self.assertEqual(self.pool.get_index_for_id("user"), 1)
        self.assertEqual(self.pool.get_index_for_id("account2"), 2)
        self.assertEqual(self.pool.get_index_for_id("account3"), 3)
        self.assertEqual(self.pool.get_index_for_id("account4"), 4)

    def test_single_account_rotation(self):
        async def run_rotation():
            senders = [await self.pool.get_next_sender() for _ in range(5)]
            return senders

        senders = asyncio.run(run_rotation())
        self.assertEqual(senders, ["user", "user", "user", "user", "user"])
        self.assertEqual(self.pool.get_pool_summary(), "1/4 Accounts Active")

    def test_multi_account_round_robin(self):
        mock_api_data = {
            "accounts": [
                {"id": "user", "index": 1, "isReady": True, "status": "ready", "phone": "+1111111111", "name": "Primary"},
                {"id": "account2", "index": 2, "isReady": True, "status": "ready", "phone": "+2222222222", "name": "Second"},
                {"id": "account3", "index": 3, "isReady": True, "status": "ready", "phone": "+3333333333", "name": "Third"},
                {"id": "account4", "index": 4, "isReady": True, "status": "ready", "phone": "+4444444444", "name": "Fourth"},
            ]
        }
        self.pool.sync_from_api_response(mock_api_data)
        self.assertEqual(self.pool.get_pool_summary(), "4/4 Accounts Active")

        async def run_rotation():
            senders = [await self.pool.get_next_sender() for _ in range(8)]
            return senders

        senders = asyncio.run(run_rotation())
        expected = ["user", "account2", "account3", "account4", "user", "account2", "account3", "account4"]
        self.assertEqual(senders, expected)

    def test_failover_and_cooldown(self):
        mock_api_data = {
            "accounts": [
                {"id": "user", "index": 1, "isReady": True, "status": "ready"},
                {"id": "account2", "index": 2, "isReady": True, "status": "ready"},
                {"id": "account3", "index": 3, "isReady": True, "status": "ready"},
                {"id": "account4", "index": 4, "isReady": False, "status": "not_logged_in"},
            ]
        }
        self.pool.sync_from_api_response(mock_api_data)

        self.pool.mark_degraded("account2", cooldown_sec=10)

        async def run_rotation():
            senders = [await self.pool.get_next_sender() for _ in range(4)]
            return senders

        senders = asyncio.run(run_rotation())
        self.assertEqual(senders, ["user", "account3", "user", "account3"])

        self.pool.mark_recovered("account2")
        senders_after_recover = asyncio.run(run_rotation())
        self.assertIn("account2", senders_after_recover)

    def test_all_degraded_fallback(self):
        mock_api_data = {
            "accounts": [
                {"id": "user", "index": 1, "isReady": False, "status": "disconnected"},
                {"id": "account2", "index": 2, "isReady": False, "status": "disconnected"},
            ]
        }
        self.pool.sync_from_api_response(mock_api_data)
        ready = self.pool.get_ready_accounts()
        self.assertEqual(ready, ["user"])

    def test_volume_tracking(self):
        self.pool.record_dispatched("user")
        self.pool.record_dispatched("user")
        self.pool.record_dispatched("account2")

        info_user = self.pool.get_account_info("user")
        info_acc2 = self.pool.get_account_info("account2")
        self.assertEqual(info_user["today_dispatched"], 2)
        self.assertEqual(info_acc2["today_dispatched"], 1)

    def test_dashboard_formatting(self):
        mock_api_data = {
            "accounts": [
                {"id": "user", "index": 1, "isReady": True, "status": "ready", "phone": "+919876543210", "name": "Main"},
                {"id": "account2", "index": 2, "isReady": False, "status": "waiting_qr_scan"},
            ]
        }
        self.pool.sync_from_api_response(mock_api_data)
        self.pool.record_dispatched("user")

        card = self.pool.format_dashboard_card()
        self.assertIn("WhatsApp Accounts Pool", card)
        self.assertIn("+919876543210", card)
        self.assertIn("Dispatched Today", card)
        self.assertIn("1 msgs", card)
        self.assertIn("Waiting for QR Scan", card)
        self.assertIn("Direct (Host VPS IP)", card)

    def test_proxy_syncing_and_card_display(self):
        mock_api_data = {
            "accounts": [
                {
                    "id": "user",
                    "index": 1,
                    "isReady": True,
                    "status": "ready",
                    "phone": "+1234567890",
                    "hasProxy": True,
                    "proxyExitIp": "198.51.100.42",
                    "proxyCountry": "United States",
                    "proxyCountryCode": "US",
                    "proxyLatencyMs": 85,
                    "proxyStatus": "healthy",
                },
                {
                    "id": "account2",
                    "index": 2,
                    "isReady": True,
                    "status": "ready",
                    "hasProxy": True,
                    "proxyExitIp": "203.0.113.19",
                    "proxyCountry": "Germany",
                    "proxyCountryCode": "DE",
                    "proxyLatencyMs": 140,
                    "proxyStatus": "healthy",
                },
                {
                    "id": "account3",
                    "index": 3,
                    "isReady": False,
                    "status": "disconnected",
                    "hasProxy": True,
                    "proxyStatus": "healthy",
                    "proxyExitIp": "192.0.2.1",
                },
            ]
        }
        self.pool.sync_from_api_response(mock_api_data)

        user_info = self.pool.get_account_info("user")
        self.assertTrue(user_info["has_proxy"])
        self.assertEqual(user_info["proxy_exit_ip"], "198.51.100.42")
        self.assertEqual(user_info["proxy_country"], "United States")
        self.assertEqual(user_info["proxy_country_code"], "US")
        self.assertEqual(user_info["proxy_latency_ms"], 85)
        self.assertEqual(user_info["proxy_status"], "healthy")

        acc2_info = self.pool.get_account_info("account2")
        self.assertEqual(acc2_info["proxy_country"], "Germany")
        self.assertEqual(acc2_info["proxy_latency_ms"], 140)

        card = self.pool.format_dashboard_card()
        # Check Account 1 proxy display
        self.assertIn("198.51.100.42", card)
        self.assertIn("United States", card)
        self.assertIn("85ms", card)

        # Check Account 2 proxy display
        self.assertIn("203.0.113.19", card)
        self.assertIn("Germany", card)
        self.assertIn("140ms", card)

        # Check Account 3 proxy display
        self.assertIn("192.0.2.1", card)

        # Account 4 has no proxy configured -> Direct
        self.assertIn("Direct (Host VPS IP)", card)

    def test_build_account_select_keyboard_has_all_4_accounts(self):
        from handlers.accounts import build_account_select_keyboard
        kb = build_account_select_keyboard("cb:acc:login")
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("cb:acc:login:1", callbacks)
        self.assertIn("cb:acc:login:2", callbacks)
        self.assertIn("cb:acc:login:3", callbacks)
        self.assertIn("cb:acc:login:4", callbacks)
        self.assertIn("cb:acc:back", callbacks)

    def test_login_whatsapp_command_parsing(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import handlers.accounts as acc_handlers
        bot_mod = sys.modules.get("bot")

        # Test with /login 2
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 12345
        msg.get_args.return_value = "2"
        msg.text = "/login 2"
        msg.reply = AsyncMock()

        mock_login = AsyncMock()
        with patch.object(acc_handlers, "_is_admin", return_value=True), \
             patch.object(acc_handlers, "_perform_login_for_account", mock_login):
            if bot_mod and hasattr(bot_mod, "_perform_login_for_account"):
                with patch.object(bot_mod, "_perform_login_for_account", mock_login):
                    asyncio.run(acc_handlers.login_whatsapp(msg))
            else:
                asyncio.run(acc_handlers.login_whatsapp(msg))
            mock_login.assert_awaited_once_with(msg, 2)

        # Test with /login2
        msg2 = MagicMock()
        msg2.chat.type = "private"
        msg2.from_user.id = 12345
        msg2.get_args.return_value = ""
        msg2.text = "/login2"
        msg2.reply = AsyncMock()

        mock_login2 = AsyncMock()
        with patch.object(acc_handlers, "_is_admin", return_value=True), \
             patch.object(acc_handlers, "_perform_login_for_account", mock_login2):
            if bot_mod and hasattr(bot_mod, "_perform_login_for_account"):
                with patch.object(bot_mod, "_perform_login_for_account", mock_login2):
                    asyncio.run(acc_handlers.login_whatsapp(msg2))
            else:
                asyncio.run(acc_handlers.login_whatsapp(msg2))
            mock_login2.assert_awaited_once_with(msg2, 2)

        # Test with bare message "3"
        msg3 = MagicMock()
        msg3.chat.type = "private"
        msg3.from_user.id = 12345
        msg3.text = "3"
        msg3.reply = AsyncMock()

        mock_login3 = AsyncMock()
        with patch.object(acc_handlers, "_is_admin", return_value=True), \
             patch.object(acc_handlers, "_perform_login_for_account", mock_login3):
            if bot_mod and hasattr(bot_mod, "_perform_login_for_account"):
                with patch.object(bot_mod, "_perform_login_for_account", mock_login3):
                    asyncio.run(acc_handlers.account_number_quick_login(msg3))
            else:
                asyncio.run(acc_handlers.account_number_quick_login(msg3))
            mock_login3.assert_awaited_once_with(msg3, 3)


if __name__ == "__main__":
    unittest.main()

