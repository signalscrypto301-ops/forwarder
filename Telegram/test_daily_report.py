import os
import sys
import unittest
import tempfile
import sqlite3
from unittest.mock import MagicMock

# Mock third-party dependencies not present in host environment
for mod in ["yaml", "aiogram", "aiogram.utils", "aiogram.types", "telethon", "telethon.sessions", "qrcode", "aiohttp", "requests"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import database

# Now import bot functions
import bot


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        # Create a temporary SQLite database for testing
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

    def test_record_and_get_daily_metrics(self):
        date_key = "2026-09-07"

        # Record 310 text messages (latency ~ 1000ms = 1.0s)
        for _ in range(310):
            database.record_delivery_metric("text", latency_ms=1000.0, date_str=date_key)

        # Record 85 photo messages (latency ~ 1500ms = 1.5s)
        for _ in range(85):
            database.record_delivery_metric("photo", latency_ms=1500.0, date_str=date_key)

        # Record 25 video messages (latency ~ 2000ms = 2.0s)
        for _ in range(25):
            database.record_delivery_metric("video", latency_ms=2000.0, date_str=date_key)

        # Record 2 retried successful messages (attempt > 0, failed=False)
        for _ in range(2):
            database.record_delivery_metric("text", latency_ms=1200.0, retried=True, failed=False, date_str=date_key)

        metrics = database.get_daily_metrics(date_key)

        # 310 + 2 retried = 312 text, 85 photos, 25 videos => total 422
        self.assertEqual(metrics["text"], 312)
        self.assertEqual(metrics["photos"], 85)
        self.assertEqual(metrics["videos"], 25)
        self.assertEqual(metrics["total_forwarded"], 422)
        self.assertEqual(metrics["retried_success"], 2)
        self.assertEqual(metrics["failed"], 0)
        self.assertGreater(metrics["avg_latency_s"], 1.0)
        self.assertLess(metrics["avg_latency_s"], 1.5)

    def test_active_channels_count(self):
        # Add 65 channels
        for i in range(1, 66):
            database.add_channel(f"-100{i:09d}")
            database.add_group_for_channel(f"-100{i:09d}", f"group_{i}@g.us")

        self.assertEqual(database.get_active_channels_count(), 65)

        # Pause 5 channels
        for i in range(1, 6):
            database.set_channel_paused(f"-100{i:09d}", True)

        self.assertEqual(database.get_active_channels_count(), 60)

        # Resume 2 channels
        for i in range(1, 3):
            database.set_channel_paused(f"-100{i:09d}", False)

        self.assertEqual(database.get_active_channels_count(), 62)

    def test_generate_daily_report_text_exact_format(self):
        date_key = "2026-09-07"

        # Simulate exact user scenario:
        # 420 messages (310 text, 85 photos, 25 videos)
        # 2 failed (retried successfully)
        # Latency ~1.2s
        # Active Channels: 65

        # 308 normal text + 2 retried text = 310 text total
        for _ in range(308):
            database.record_delivery_metric("text", latency_ms=1150.0, date_str=date_key)
        for _ in range(2):
            database.record_delivery_metric("text", latency_ms=1250.0, retried=True, failed=False, date_str=date_key)

        for _ in range(85):
            database.record_delivery_metric("photo", latency_ms=1200.0, date_str=date_key)

        for _ in range(25):
            database.record_delivery_metric("video", latency_ms=1200.0, date_str=date_key)

        # Add 65 active channels
        for i in range(1, 66):
            database.add_channel(f"-100{i:09d}")
            database.add_group_for_channel(f"-100{i:09d}", f"group_{i}@g.us")

        report = bot.generate_daily_report_text(date_key)

        expected_report = (
            "📊 Daily Forwarder Report (Sep 07)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Forwarded: 420 messages (310 text, 85 photos, 25 videos)\n"
            "❌ Failed: 2 (retried successfully)\n"
            "⏱️ Avg Delivery Latency: 1.2s\n"
            "🔋 Active Channels: 65\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        self.assertEqual(report.strip(), expected_report.strip())

    def test_edge_cases(self):
        # Empty day report
        report_empty = bot.generate_daily_report_text("2026-09-01")
        self.assertIn("✅ Forwarded: 0 messages (0 text, 0 photos, 0 videos)", report_empty)
        self.assertIn("❌ Failed: 0", report_empty)
        self.assertIn("⏱️ Avg Delivery Latency: 0.0s", report_empty)

        # Failed permanent dispatches
        database.record_delivery_metric("text", latency_ms=500.0, failed=True, date_str="2026-09-02")
        database.record_delivery_metric("photo", latency_ms=500.0, failed=True, date_str="2026-09-02")
        report_failures = bot.generate_daily_report_text("2026-09-02")
        self.assertIn("❌ Failed: 2", report_failures)


if __name__ == "__main__":
    unittest.main()
