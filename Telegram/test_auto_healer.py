import os
import sys
import time
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import database
from auto_healer import AutoHealer


class TestAutoHealer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_autohealer_")
        self.media_dir = os.path.join(self.temp_dir, "media")
        self.dlq_dir = os.path.join(self.media_dir, "dlq")
        os.makedirs(self.media_dir, exist_ok=True)
        os.makedirs(self.dlq_dir, exist_ok=True)

        self.orig_db_path = database.DB_PATH
        self.test_db_path = os.path.join(self.temp_dir, "test_healer.db")
        database.DB_PATH = self.test_db_path
        database.create_table()

        self.healer = AutoHealer(
            ram_critical_percent=90.0,
            ram_warning_percent=85.0,
            disk_warning_percent=90.0,
            temp_files_threshold=5,
            temp_file_max_age_sec=2,
            heal_cooldown_sec=10,
        )

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_1_telemetry_keys(self):
        telem = self.healer.get_telemetry()
        expected_keys = [
            "host_ram_pct",
            "host_ram_used_gb",
            "host_ram_total_gb",
            "cpu_pct",
            "disk_pct",
            "disk_free_gb",
            "disk_total_gb",
            "bot_ram_mb",
            "media_count",
            "media_size_mb",
            "media_size_bytes",
        ]
        for key in expected_keys:
            self.assertIn(key, telem)

    def test_2_purge_preserves_dlq(self):
        stale_file = os.path.join(self.media_dir, "stale_photo.jpg")
        with open(stale_file, "wb") as f:
            f.write(b"x" * 1024)
        os.utime(stale_file, (time.time() - 100, time.time() - 100))

        dlq_file = os.path.join(self.dlq_dir, "important_failed_msg.jpg")
        with open(dlq_file, "wb") as f:
            f.write(b"y" * 2048)
        os.utime(dlq_file, (time.time() - 100, time.time() - 100))

        with patch("auto_healer.MEDIA_DIR", self.media_dir), patch(
            "auto_healer.WHATSAPP_UPLOADS_DIR", os.path.join(self.temp_dir, "nonexistent")
        ):
            purged_count, bytes_freed = self.healer.purge_temp_media(max_age_seconds=10)

        self.assertEqual(purged_count, 1)
        self.assertEqual(bytes_freed, 1024)
        self.assertFalse(os.path.exists(stale_file), "Stale media must be purged")
        self.assertTrue(os.path.exists(dlq_file), "DLQ file MUST NOT be purged")

    def test_3_trim_oversized_logs(self):
        log_file = os.path.join(self.temp_dir, "bot.log")
        with open(log_file, "w", encoding="utf-8") as f:
            for i in range(15000):
                f.write(f"Line {i} - Log entry payload data\n")

        original_size = os.path.getsize(log_file)
        with patch("auto_healer.BASE_DIR", self.temp_dir):
            freed = self.healer.trim_oversized_logs(max_bytes=original_size // 2)

        self.assertGreater(freed, 0)
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 10000)

    def test_4_threshold_trigger_ram(self):
        mock_telem = {
            "host_ram_pct": 92.5,
            "disk_pct": 50.0,
            "media_count": 0,
            "media_size_mb": 0.0,
            "media_size_bytes": 0,
            "cpu_pct": 10,
            "disk_free_gb": 100.0,
            "disk_total_gb": 200.0,
            "bot_ram_mb": 50,
            "host_ram_used_gb": 7.4,
            "host_ram_total_gb": 8.0,
        }
        with patch.object(self.healer, "get_telemetry", return_value=mock_telem):
            heal_result = self.healer.check_and_heal_if_needed()
        self.assertIsNotNone(heal_result)
        self.assertEqual(heal_result.get("status"), "success")
        self.assertIn("Host RAM Critical", heal_result.get("reason"))

    def test_5_threshold_trigger_temp_files(self):
        mock_telem = {
            "host_ram_pct": 40.0,
            "disk_pct": 30.0,
            "media_count": 25,
            "media_size_mb": 50.0,
            "media_size_bytes": 50000000,
            "cpu_pct": 5,
            "disk_free_gb": 100.0,
            "disk_total_gb": 200.0,
            "bot_ram_mb": 50,
            "host_ram_used_gb": 3.2,
            "host_ram_total_gb": 8.0,
        }
        with patch.object(self.healer, "get_telemetry", return_value=mock_telem):
            heal_result = self.healer.check_and_heal_if_needed()
        self.assertIsNotNone(heal_result)
        self.assertEqual(heal_result.get("status"), "success")
        self.assertIn("Temp Files Accumulation", heal_result.get("reason"))

    def test_6_threshold_no_trigger_healthy(self):
        mock_telem = {
            "host_ram_pct": 45.0,
            "disk_pct": 50.0,
            "media_count": 2,
            "media_size_mb": 1.0,
            "media_size_bytes": 1000000,
            "cpu_pct": 10,
            "disk_free_gb": 100.0,
            "disk_total_gb": 200.0,
            "bot_ram_mb": 50,
            "host_ram_used_gb": 3.6,
            "host_ram_total_gb": 8.0,
        }
        with patch.object(self.healer, "get_telemetry", return_value=mock_telem):
            heal_result = self.healer.check_and_heal_if_needed()
        self.assertIsNone(heal_result)

    def test_7_cooldown_skips_subsequent_heals(self):
        mock_telem = {
            "host_ram_pct": 95.0,
            "disk_pct": 50.0,
            "media_count": 0,
            "media_size_mb": 0.0,
            "media_size_bytes": 0,
            "cpu_pct": 10,
            "disk_free_gb": 100.0,
            "disk_total_gb": 200.0,
            "bot_ram_mb": 50,
            "host_ram_used_gb": 7.6,
            "host_ram_total_gb": 8.0,
        }
        with patch.object(self.healer, "get_telemetry", return_value=mock_telem):
            r1 = self.healer.check_and_heal_if_needed()
            self.assertEqual(r1.get("status"), "success")

            r2 = self.healer.check_and_heal_if_needed()
            self.assertEqual(r2.get("status"), "cooldown")

            r3 = self.healer.perform_heal(reason="Forced", force=True)
            self.assertEqual(r3.get("status"), "success")

    def test_8_database_logging_and_dashboard_card(self):
        initial_stats = database.get_auto_heal_stats()
        prev_events = initial_stats.get("total_events", 0)

        res = self.healer.perform_heal(
            reason="Test DB Event",
            force=True,
            wa_cleanup_result={"filesPurged": 3, "bytesReclaimed": 3000},
        )
        self.assertEqual(res.get("status"), "success")

        new_stats = database.get_auto_heal_stats()
        self.assertEqual(new_stats.get("total_events"), prev_events + 1)

        recent = database.get_recent_auto_heals(limit=1)
        self.assertTrue(len(recent) >= 1)
        self.assertEqual(recent[0]["trigger_reason"], "Test DB Event")

        card = self.healer.format_dashboard_card(wa_memory_mb=75)
        self.assertIn("Auto-Healer", card)
        self.assertIn("75 MB", card)
        self.assertIn("Test DB Event", card)


if __name__ == "__main__":
    unittest.main(verbosity=2)