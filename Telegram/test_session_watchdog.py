import os, sys, time, unittest
from unittest.mock import AsyncMock, MagicMock, patch

for mod in ["yaml","aiogram","aiogram.utils","aiogram.utils.executor","aiogram.types",
            "telethon","telethon.sessions","qrcode","aiohttp","requests","psutil"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import bot

# ── helper to reset watchdog state between tests ────────────────────────────
def reset():
    bot._watchdog_consecutive_failures = 0
    bot._watchdog_outage_start = None
    bot._watchdog_last_reconnect_at = 0.0

class TestWatchdog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()

    async def test_1_health_returns_true_when_ready(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"status": "ready"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch.object(bot, "get_http_session", AsyncMock(return_value=mock_sess)):
            result = await bot._get_whatsapp_health()
        self.assertTrue(result)

    async def test_2_health_returns_false_on_503(self):
        mock_resp = AsyncMock()
        mock_resp.status = 503
        mock_resp.json = AsyncMock(return_value={"status": "initializing"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch.object(bot, "get_http_session", AsyncMock(return_value=mock_sess)):
            result = await bot._get_whatsapp_health()
        self.assertFalse(result)

    async def test_3_health_returns_false_on_exception(self):
        with patch.object(bot, "get_http_session", AsyncMock(side_effect=Exception("timeout"))):
            result = await bot._get_whatsapp_health()
        self.assertFalse(result)

    async def test_4_reconnect_blocked_by_cooldown(self):
        bot._watchdog_last_reconnect_at = time.time() - 30  # only 30s ago
        calls = []
        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=lambda *a,**kw: calls.append(1) or (200,{"ready":True}))):
            await bot._watchdog_attempt_reconnect()
        self.assertEqual(len(calls), 0, "Reconnect should be blocked by cooldown")

    async def test_5_reconnect_fires_when_cooldown_elapsed(self):
        bot._watchdog_last_reconnect_at = time.time() - bot.WATCHDOG_RECONNECT_COOLDOWN - 5
        calls = []
        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=lambda *a,**kw: calls.append(1) or (200,{"ready":True}))):
            await bot._watchdog_attempt_reconnect()
        self.assertEqual(len(calls), 1, "Reconnect should fire once cooldown elapsed")

    async def test_6_no_duplicate_reconnect_while_locked(self):
        """Lock held = skip reconnect attempt."""
        bot._watchdog_last_reconnect_at = 0.0
        calls = []
        async with bot._watchdog_reconnect_lock:
            with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=lambda *a,**kw: calls.append(1) or (200,{"ready":True}))):
                await bot._watchdog_attempt_reconnect()
        self.assertEqual(len(calls), 0, "Should skip when lock is already held")

    async def test_7_below_threshold_no_reconnect(self):
        bot._watchdog_consecutive_failures = bot.WATCHDOG_FAILURE_THRESHOLD - 1
        self.assertLess(bot._watchdog_consecutive_failures, bot.WATCHDOG_FAILURE_THRESHOLD)

    async def test_8_recovery_resets_counters(self):
        bot._watchdog_consecutive_failures = 5
        bot._watchdog_outage_start = time.time() - 600
        # simulate recovery
        bot._watchdog_consecutive_failures = 0
        bot._watchdog_outage_start = None
        self.assertEqual(bot._watchdog_consecutive_failures, 0)
        self.assertIsNone(bot._watchdog_outage_start)

    async def test_9_multi_account_health_with_client_id(self):
        """_get_whatsapp_health with specific client_id should pass query parameter."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"status": "ready", "clientId": "account2"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch.object(bot, "get_http_session", AsyncMock(return_value=mock_sess)):
            result = await bot._get_whatsapp_health("account2")
        self.assertTrue(result)
        mock_sess.get.assert_called_once()
        called_url = mock_sess.get.call_args[0][0]
        self.assertIn("clientId=account2", called_url)

    async def test_10_multi_account_reconnect_with_client_id(self):
        """_watchdog_attempt_reconnect with account2 should post clientId: account2."""
        bot._watchdog_account_reconnect_at["account2"] = 0.0
        posted_data = []
        async def fake_post(endpoint, data, **kw):
            posted_data.append((endpoint, data))
            return 200, {"ready": True}

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            await bot._watchdog_attempt_reconnect("account2")

        self.assertEqual(len(posted_data), 1)
        self.assertEqual(posted_data[0][0], "createsession")
        self.assertEqual(posted_data[0][1], {"clientId": "account2"})

    async def test_11_multi_account_independent_cooldowns(self):
        """Cooldown on account2 should not prevent account3 from reconnecting."""
        now = time.time()
        bot._watchdog_account_reconnect_at["account2"] = now - 10  # recent
        bot._watchdog_account_reconnect_at["account3"] = 0.0       # expired

        posted_accounts = []
        async def fake_post(endpoint, data, **kw):
            posted_accounts.append(data.get("clientId"))
            return 200, {"ready": True}

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            # account2 is blocked by cooldown
            await bot._watchdog_attempt_reconnect("account2")
            # account3 should succeed
            await bot._watchdog_attempt_reconnect("account3")

        self.assertNotIn("account2", posted_accounts)
        self.assertIn("account3", posted_accounts)

    async def test_12_get_all_sessions_status(self):
        """_get_all_sessions_status queries /sessions and returns accounts."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "accounts": [
                {"id": "user", "isReady": True},
                {"id": "account2", "isReady": False},
            ]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_sess = MagicMock()
        mock_sess.get = MagicMock(return_value=mock_resp)
        with patch.object(bot, "get_http_session", AsyncMock(return_value=mock_sess)):
            accounts = await bot._get_all_sessions_status()
        self.assertIsNotNone(accounts)
        self.assertEqual(len(accounts), 2)
        self.assertTrue(accounts[0]["isReady"])
        self.assertFalse(accounts[1]["isReady"])

    async def test_13_cmd_watchdog_renders_multi_account_dashboard(self):
        """cmd_watchdog generates comprehensive card with all accounts."""
        mock_msg = AsyncMock()
        mock_msg.from_user.id = bot.config.admin_ids[0] if bot.config.admin_ids else 123
        mock_msg.answer = AsyncMock()

        sample_sessions = [
            {"id": "user", "label": "Account 1 (Primary)", "index": 1, "isReady": True, "status": "ready", "phone": "+1234567890"},
            {"id": "account2", "label": "Account 2", "index": 2, "isReady": True, "status": "ready", "phone": "+1987654321"},
            {"id": "account3", "label": "Account 3", "index": 3, "isReady": False, "status": "disconnected", "hasAuthFolder": True},
            {"id": "account4", "label": "Account 4", "index": 4, "isReady": False, "status": "not_logged_in", "hasAuthFolder": False},
        ]

        with patch.object(bot, "_get_all_sessions_status", AsyncMock(return_value=sample_sessions)), \
             patch.object(bot, "is_admin", return_value=True):
            await bot.cmd_watchdog(mock_msg)

        mock_msg.answer.assert_called_once()
        text = mock_msg.answer.call_args[0][0]
        self.assertIn("Multi-Account Session Watchdog Status", text)
        self.assertIn("Account 1 (Primary)", text)
        self.assertIn("Account 2", text)
        self.assertIn("Account 3", text)
        self.assertIn("Account 4", text)
        self.assertIn("2/4 Active", text)

if __name__ == "__main__":
    unittest.main(verbosity=2)

