import os
import sys
import unittest
import tempfile
from unittest.mock import MagicMock, patch

# Mock dependencies not installed on host
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
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import bot


class TestServerTelemetry(unittest.TestCase):
    def setUp(self):
        self.temp_media_dir = tempfile.TemporaryDirectory()
        self.orig_media_dir = bot.MEDIA_DIR
        bot.MEDIA_DIR = self.temp_media_dir.name

    def tearDown(self):
        bot.MEDIA_DIR = self.orig_media_dir
        self.temp_media_dir.cleanup()

    def test_get_server_telemetry_formatted(self):
        """Verify telemetry formatting matches user's exact specification."""
        mock_vm = MagicMock()
        mock_vm.percent = 42.0
        mock_vm.used = int(3.4 * (1024 ** 3))
        mock_vm.total = int(8.0 * (1024 ** 3))

        mock_disk = MagicMock()
        mock_disk.percent = 22.0  # 78% free
        mock_disk.free = int(45 * (1024 ** 3))

        mock_proc = MagicMock()
        mock_proc.memory_info.return_value.rss = 62 * 1024 * 1024  # 62 MB

        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value = mock_vm
        mock_psutil.cpu_percent.return_value = 12.0
        mock_psutil.disk_usage.return_value = mock_disk
        mock_psutil.Process.return_value = mock_proc

        with patch.object(bot, "psutil", mock_psutil):
            output = bot.get_server_telemetry(baileys_ram_mb=48)

        self.assertIn("🖥️ <b>Server & Engine Telemetry</b>", output)
        self.assertIn("• Host RAM: 42% used (3.4GB / 8.0GB)", output)
        self.assertIn("• CPU Load: 12%", output)
        self.assertIn("• Disk Space: 78% free (45GB available)", output)
        self.assertIn("• Baileys Socket RAM: 48 MB", output)
        self.assertIn("• Telegram Bot RAM: 62 MB", output)
        self.assertIn("• Media Temp Directory: 0 files (Clean)", output)

    def test_media_temp_directory_non_empty(self):
        """Verify media temp directory reports count when files are pending cleanup."""
        # Create 3 temporary dummy files in MEDIA_DIR
        for i in range(3):
            with open(os.path.join(bot.MEDIA_DIR, f"temp_{i}.jpg"), "w") as f:
                f.write("dummy")

        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value.percent = 50.0
        mock_psutil.virtual_memory.return_value.used = 4 * (1024 ** 3)
        mock_psutil.virtual_memory.return_value.total = 8 * (1024 ** 3)
        mock_psutil.cpu_percent.return_value = 5.0
        mock_psutil.disk_usage.return_value.percent = 20.0
        mock_psutil.disk_usage.return_value.free = 50 * (1024 ** 3)
        mock_psutil.Process.return_value.memory_info.return_value.rss = 40 * 1024 * 1024

        with patch.object(bot, "psutil", mock_psutil):
            output = bot.get_server_telemetry(baileys_ram_mb=35)

        self.assertIn("• Media Temp Directory: 3 files", output)
        self.assertIn("• Baileys Socket RAM: 35 MB", output)

    def test_telemetry_graceful_fallback(self):
        """Verify telemetry does not crash when psutil is None."""
        with patch.object(bot, "psutil", None):
            output = bot.get_server_telemetry(baileys_ram_mb=None)

        self.assertIn("🖥️ <b>Server & Engine Telemetry</b>", output)
        self.assertIn("• Host RAM: Unavailable", output)
        self.assertIn("• CPU Load: Unavailable", output)
        self.assertIn("• Baileys Socket RAM: N/A", output)
        self.assertIn("• Media Temp Directory: 0 files (Clean)", output)


if __name__ == "__main__":
    unittest.main()
