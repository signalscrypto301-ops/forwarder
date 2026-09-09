import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

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


import aiogram.types

aiogram.types.InlineKeyboardMarkup = MockInlineKeyboardMarkup
aiogram.types.InlineKeyboardButton = MockInlineKeyboardButton

import database
import analytics_card
from analytics_card import generate_analytics_text_report, generate_analytics_infographic
import bot
from bot import build_analytics_keyboard, build_analytics_detail_keyboard, fetch_audience_metadata


class TestAnalyticsEngine(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        self.test_db = os.path.join(os.path.dirname(__file__), "test_analytics.db")
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass
        database.DB_PATH = self.test_db
        database.create_table()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass

    def test_record_channel_post_activity_and_hourly_aggregation(self):
        """Verify post volume tracking across channels and hourly buckets."""
        c1 = "-1001111111111"
        c2 = "-1002222222222"
        now = datetime(2026, 9, 8, 14, 15, 0)  # 14:00 bucket

        # Record 3 posts for c1 at 14:00
        database.record_channel_post_activity(c1, now)
        database.record_channel_post_activity(c1, now)
        database.record_channel_post_activity(c1, now)

        # Record 2 posts for c2 at 14:00
        database.record_channel_post_activity(c2, now)
        database.record_channel_post_activity(c2, now)

        # Record 1 post for c1 at 15:00
        later = datetime(2026, 9, 8, 15, 30, 0)
        database.record_channel_post_activity(c1, later)

        summary = database.get_channel_volume_summary("2026-09-08")
        self.assertEqual(summary["total_posts"], 6)
        self.assertEqual(summary["active_channels"], 2)
        self.assertEqual(summary["peak_hour"], 14)
        self.assertEqual(summary["peak_hour_volume"], 5)

    def test_get_top_active_channels_ranking(self):
        """Verify ranking of channels by post count with percentage calculation."""
        c1 = "-1001111111111"
        c2 = "-1002222222222"
        c3 = "-1003333333333"

        database.add_channel(c1)
        database.add_channel(c2)
        database.add_channel(c3)

        database.add_group_for_channel(c1, "120363001@g.us")
        database.add_group_for_channel(c1, "120363002@g.us")
        database.add_group_for_channel(c2, "120363003@newsletter")

        now = datetime(2026, 9, 8, 10, 0, 0)
        for _ in range(12):
            database.record_channel_post_activity(c1, now)
        for _ in range(6):
            database.record_channel_post_activity(c2, now)
        for _ in range(2):
            database.record_channel_post_activity(c3, now)

        top = database.get_top_active_channels("2026-09-08", limit=3)
        self.assertEqual(len(top), 3)

        # Rank 1: c1 (12 posts = 60.0%)
        self.assertEqual(top[0]["channel_id"], c1)
        self.assertEqual(top[0]["post_count"], 12)
        self.assertEqual(top[0]["percent"], 60.0)
        self.assertEqual(top[0]["groups_count"], 2)

        # Rank 2: c2 (6 posts = 30.0%)
        self.assertEqual(top[1]["channel_id"], c2)
        self.assertEqual(top[1]["post_count"], 6)
        self.assertEqual(top[1]["percent"], 30.0)
        self.assertEqual(top[1]["groups_count"], 1)

        # Rank 3: c3 (2 posts = 10.0%)
        self.assertEqual(top[2]["channel_id"], c3)
        self.assertEqual(top[2]["post_count"], 2)
        self.assertEqual(top[2]["percent"], 10.0)

    def test_get_hourly_traffic_distribution(self):
        """Verify 24-hour distribution and peak hour marking."""
        now = datetime(2026, 9, 8, 16, 0, 0)
        database.record_channel_post_activity("-1009999999999", now)
        database.record_channel_post_activity("-1009999999999", now)

        dist = database.get_hourly_traffic_distribution("2026-09-08")
        self.assertEqual(len(dist), 24)

        hour_16 = dist[16]
        self.assertEqual(hour_16["hour"], 16)
        self.assertEqual(hour_16["post_count"], 2)
        self.assertTrue(hour_16["is_peak"])

        hour_10 = dist[10]
        self.assertEqual(hour_10["post_count"], 0)
        self.assertFalse(hour_10["is_peak"])

    def test_generate_analytics_text_report_formatting(self):
        """Verify text report contains audience reach, top channels, and hourly buckets."""
        top_channels = [
            {"rank": 1, "channel_id": "-1001234567890", "post_count": 120, "percent": 58.5},
            {"rank": 2, "channel_id": "-1009876543210", "post_count": 85, "percent": 41.5},
        ]
        hourly_dist = [{"hour": h, "post_count": 10 if h == 14 else 1} for h in range(24)]
        audience_stats = {
            "totalAudience": 45820,
            "groupsCount": 12,
            "groupMembers": 4120,
            "newslettersCount": 8,
            "newsletterSubscribers": 41700,
        }
        summary = {
            "date_key": "2026-09-08",
            "total_posts": 205,
            "active_channels": 2,
            "peak_hour_label": "14:00",
            "peak_hour_volume": 10,
        }

        report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)

        self.assertIn("Audience & Channel Intelligence", report)
        self.assertIn("45,820", report)
        self.assertIn("41,700", report)
        self.assertIn("4,120", report)
        self.assertIn("Top 5 Active Channels", report)
        self.assertIn("-1001234567890", report)
        self.assertIn("120", report)
        self.assertIn("58.5%", report)
        self.assertIn("Hourly Traffic Heatmap", report)
        self.assertIn("12:00 - 16:00", report)

    def test_build_analytics_keyboard_callbacks(self):
        """Verify keyboard callback data lengths stay strictly <= 64 bytes."""
        kb = build_analytics_keyboard()
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertLessEqual(len(btn.callback_data.encode("utf-8")), 64)
                self.assertTrue(len(btn.text) > 0)

        detail_kb = build_analytics_detail_keyboard()
        for row in detail_kb.inline_keyboard:
            for btn in row:
                self.assertLessEqual(len(btn.callback_data.encode("utf-8")), 64)

    def test_analytics_infographic_graceful_or_rendered(self):
        """Verify that generate_analytics_infographic either returns valid PNG bytes or None if PIL is absent."""
        top_channels = [{"rank": 1, "channel_id": "-1001234567890", "post_count": 10, "percent": 100.0, "groups_count": 1}]
        hourly_dist = [{"hour": h, "post_count": 2, "is_peak": (h == 12)} for h in range(24)]
        audience_stats = {
            "totalAudience": 1500,
            "groupsCount": 2,
            "groupMembers": 300,
            "newslettersCount": 1,
            "newsletterSubscribers": 1200,
        }
        summary = {
            "date_key": "2026-09-08",
            "total_posts": 10,
            "active_channels": 1,
            "peak_hour_label": "12:00",
            "peak_hour_volume": 2,
        }

        png_bytes = generate_analytics_infographic("2026-09-08", top_channels, hourly_dist, audience_stats, summary)
        if analytics_card.HAS_PIL:
            self.assertIsNotNone(png_bytes)
            # Check PNG magic bytes
            self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        else:
            self.assertIsNone(png_bytes)


if __name__ == "__main__":
    unittest.main()
