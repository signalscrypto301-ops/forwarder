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

if __name__ == "__main__":
    unittest.main(verbosity=2)
