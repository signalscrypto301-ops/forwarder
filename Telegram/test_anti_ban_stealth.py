import unittest
import asyncio
import time
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(__file__))

for mod in ["yaml","aiogram","aiogram.utils","aiogram.utils.executor","aiogram.types",
            "telethon","telethon.sessions","qrcode","aiohttp","requests","psutil"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from rate_limiter import TokenBucket, DeliveryRateController
import tasks.watchdog as watchdog
import bot_context


class TestAntiBanStealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = DeliveryRateController(
            max_per_hour=120,
            max_per_day=1000,
            alert_threshold_percent=80.0,
            per_recipient_rate=1.0,
            per_recipient_burst=2,
            account_ids=["user", "account2", "account3", "account4"],
        )

    def test_pacing_delay_jitter_bounds(self):
        self.controller.active_queue_depth = 3
        delays = [
            self.controller.calculate_pacing_delay("12345@newsletter", account_id="user")
            for _ in range(50)
        ]

        for d in delays:
            self.assertGreaterEqual(d, 0.6)

        unique_delays = set(delays)
        self.assertGreater(len(unique_delays), 15, "Pacing delays must vary organically due to jitter")

    def test_pacing_delay_non_periodicity(self):
        self.controller.active_queue_depth = 10
        delays = [
            self.controller.calculate_pacing_delay("group1@g.us", account_id="account2")
            for _ in range(20)
        ]

        differences = [abs(delays[i] - delays[i - 1]) for i in range(1, len(delays))]
        non_zero_diffs = [diff for diff in differences if diff > 0.001]
        self.assertGreater(len(non_zero_diffs), 16)

    async def test_token_bucket_wait_jitter(self):
        bucket = TokenBucket(capacity=1.0, refill_rate=10.0)
        w0 = await bucket.acquire()
        self.assertEqual(w0, 0.0)

        waits = []
        for _ in range(3):
            w = await bucket.acquire()
            waits.append(w)

        for w in waits:
            self.assertGreater(w, 0.07)
            self.assertLess(w, 0.20)

    async def test_watchdog_dashboard_displays_proxy_status(self):
        """
        Verifies that the /watchdog status view correctly formats and displays
        dedicated proxy metadata for accounts operating through an isolated proxy.
        """
        import bot

        mock_sessions = [
            {
                "id": "user",
                "index": 1,
                "label": "Account 1",
                "isReady": True,
                "status": "ready",
                "phone": "+1001",
                "hasAuthFolder": True,
                "hasProxy": True,
                "proxyHost": "http://185.220.101.5:8080",
            },
            {
                "id": "account2",
                "index": 2,
                "label": "Account 2",
                "isReady": True,
                "status": "ready",
                "phone": "+1002",
                "hasAuthFolder": True,
                "hasProxy": False,
                "proxyHost": None,
            },
        ]

        mock_msg = MagicMock()
        mock_msg.answer = AsyncMock()
        mock_msg.from_user.id = 123456

        with patch.object(bot, "_get_all_sessions_status", AsyncMock(return_value=mock_sessions)), \
             patch.object(bot, "is_admin", return_value=True):
            await bot.cmd_watchdog(mock_msg)

        mock_msg.answer.assert_called_once()
        output = mock_msg.answer.call_args[0][0]
        # Account 1 must display proxy indicator
        self.assertIn("Proxy: http://185.220.101.5:8080", output)
        # Account 2 does not have proxy, must not have proxy string
        self.assertNotIn("Account 2</b>: 🟢 <b>Ready</b> (📞 <code>+1002</code>) [🌐 Proxy", output)

    def test_typing_presence_delay_bounds_model(self):
        def compute_expected_delay(jid: str, text: str, is_media: bool = False) -> tuple[bool, int, int]:
            if jid.endswith("@newsletter"):
                return False, 0, 0
            if is_media:
                return True, 1200, 2400
            text_len = len(text)
            min_delay = min(max(1000, text_len * 14), 3800)
            max_delay = min_delay + 500
            return True, min_delay, max_delay

        should_send, min_d, max_d = compute_expected_delay("120363029384756@newsletter", "Hello Channel")
        self.assertFalse(should_send)
        self.assertEqual(min_d, 0)

        should_send, min_d, max_d = compute_expected_delay("12036301@g.us", "Quick ping")
        self.assertTrue(should_send)
        self.assertGreaterEqual(min_d, 1000)
        self.assertLessEqual(max_d, 1640)

        should_send, min_d, max_d = compute_expected_delay("12036301@g.us", "A" * 100)
        self.assertTrue(should_send)
        self.assertGreaterEqual(min_d, 1400)
        self.assertLessEqual(max_d, 2000)

        should_send, min_d, max_d = compute_expected_delay("12036301@g.us", "A" * 500)
        self.assertTrue(should_send)
        self.assertEqual(min_d, 3800)
        self.assertEqual(max_d, 4300)

        should_send, min_d, max_d = compute_expected_delay("12036301@g.us", "", is_media=True)
        self.assertTrue(should_send)
        self.assertEqual(min_d, 1200)
        self.assertEqual(max_d, 2400)


if __name__ == "__main__":
    unittest.main()
