import unittest
import asyncio
import time
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))

for mod in ["yaml", "aiogram", "aiogram.utils", "aiogram.utils.executor", "aiogram.types",
            "telethon", "telethon.sessions", "qrcode", "aiohttp", "requests", "psutil"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import config
from account_pool import AccountPool
import tasks.watchdog as watchdog


class TestInstantDisconnectAlert(unittest.TestCase):
    def setUp(self):
        self.pool = AccountPool()
        watchdog._last_logout_alert_time.clear()

    def test_format_active_siblings_string(self):
        # Scenario 1: Accounts 1, 2, 3 are ready
        api_data = {
            "accounts": [
                {"id": "user", "isReady": True, "status": "ready"},
                {"id": "account2", "isReady": True, "status": "ready"},
                {"id": "account3", "isReady": True, "status": "ready"},
                {"id": "account4", "isReady": False, "status": "not_logged_in"},
            ]
        }
        self.pool.sync_from_api_response(api_data)

        # If Account 2 is logged out, remaining active siblings are Account 1 & 3
        siblings_str = self.pool.format_active_siblings_string("account2")
        self.assertEqual(siblings_str, "Account 1 & 3")

        # Scenario 2: Accounts 1, 2, 3, 4 are ready
        api_data["accounts"][3]["isReady"] = True
        api_data["accounts"][3]["status"] = "ready"
        self.pool.sync_from_api_response(api_data)

        siblings_str = self.pool.format_active_siblings_string("account2")
        self.assertEqual(siblings_str, "Account 1, 3 & 4")

        # Scenario 3: Only Account 1 and Account 2 are ready
        api_data["accounts"][2]["isReady"] = False
        api_data["accounts"][3]["isReady"] = False
        self.pool.sync_from_api_response(api_data)

        siblings_str = self.pool.format_active_siblings_string("account2")
        self.assertEqual(siblings_str, "Account 1")

        # Scenario 4: Only Account 2 was ready and it logged out (pool exhausted)
        api_data["accounts"][0]["isReady"] = False
        api_data["accounts"][1]["isReady"] = False
        self.pool.sync_from_api_response(api_data)

        siblings_str = self.pool.format_active_siblings_string("account2")
        self.assertIn("Pool Exhausted", siblings_str)

    def test_send_instant_logout_alert_formatting_and_inline_button(self):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        api_data = {
            "accounts": [
                {"id": "user", "isReady": True, "status": "ready", "phone": "+1111111111"},
                {"id": "account2", "isReady": True, "status": "ready", "phone": "+12262406756"},
                {"id": "account3", "isReady": True, "status": "ready", "phone": "+3333333333"},
            ]
        }
        self.pool.sync_from_api_response(api_data)

        mock_bot_mod = MagicMock()
        mock_bot_mod.bot = mock_bot
        mock_bot_mod.account_pool = self.pool
        mock_bot_mod._last_logout_alert_time = watchdog._last_logout_alert_time
        mock_bot_mod.LOGOUT_ALERT_COOLDOWN = 300

        with patch("tasks.watchdog._get_bot_module", return_value=mock_bot_mod), \
             patch.object(config, "admin_ids", [5363402037, 5227551003]):

            result = asyncio.run(watchdog.send_instant_logout_alert("account2", phone="+12262406756"))
            self.assertTrue(result)

            # Both admin IDs should have been notified
            self.assertEqual(mock_bot.send_message.call_count, 2)

            call_args_1 = mock_bot.send_message.call_args_list[0]
            call_args_2 = mock_bot.send_message.call_args_list[1]

            self.assertEqual(call_args_1[0][0], 5363402037)
            self.assertEqual(call_args_2[0][0], 5227551003)

            msg_text = call_args_1[0][1]
            self.assertIn("🚨 <b>URGENT: WhatsApp Account 2 (+12262406756) was logged out!</b>", msg_text)
            self.assertIn("Traffic has been safely shifted to <b>Account 1 & 3</b>.", msg_text)

            # Check that inline keyboard was attached
            reply_markup = call_args_1[1].get("reply_markup")
            self.assertIsNotNone(reply_markup)

            # Verify Account 2 was degraded in pool
            acc2_info = self.pool.get_account_info("account2")
            self.assertFalse(acc2_info["is_ready"])

    def test_logout_alert_debouncing(self):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        mock_bot_mod = MagicMock()
        mock_bot_mod.bot = mock_bot
        mock_bot_mod.account_pool = self.pool
        mock_bot_mod._last_logout_alert_time = watchdog._last_logout_alert_time
        mock_bot_mod.LOGOUT_ALERT_COOLDOWN = 300

        with patch("tasks.watchdog._get_bot_module", return_value=mock_bot_mod), \
             patch.object(config, "admin_ids", [12345]):

            first_call = asyncio.run(watchdog.send_instant_logout_alert("account2"))
            self.assertTrue(first_call)
            self.assertEqual(mock_bot.send_message.call_count, 1)

            # Second call within 5 minutes should be debounced
            second_call = asyncio.run(watchdog.send_instant_logout_alert("account2"))
            self.assertFalse(second_call)
            self.assertEqual(mock_bot.send_message.call_count, 1)  # no new send

            # Different account should not be debounced
            acc3_call = asyncio.run(watchdog.send_instant_logout_alert("account3"))
            self.assertTrue(acc3_call)
            self.assertEqual(mock_bot.send_message.call_count, 2)

    def test_scheduled_instant_disconnect_watchdog_loop_triggers_alert(self):
        mock_alert_fn = AsyncMock()
        mock_bot_mod = MagicMock()
        mock_bot_mod.send_instant_logout_alert = mock_alert_fn

        mock_session = MagicMock()
        mock_res = MagicMock()
        mock_res.status = 200
        mock_res.json = AsyncMock(return_value={
            "events": [
                {
                    "eventId": "evt_123_account2",
                    "clientId": "account2",
                    "phone": "+12262406756",
                    "reason": "Session Logged Out or Banned",
                }
            ]
        })
        mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_res)
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_bot_mod.get_http_session = AsyncMock(return_value=mock_session)

        sleep_count = 0
        async def fake_sleep(duration):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError()

        with patch("tasks.watchdog._get_bot_module", return_value=mock_bot_mod), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            asyncio.run(watchdog.scheduled_instant_disconnect_watchdog_loop())

        mock_alert_fn.assert_called_once_with(
            "account2",
            phone="+12262406756",
            reason="Session Logged Out or Banned",
        )


if __name__ == "__main__":
    unittest.main()

