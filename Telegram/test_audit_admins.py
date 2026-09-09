import os
import sys
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

aiogram_mod = sys.modules.get("aiogram")
if aiogram_mod:
    mock_dp = MagicMock()
    mock_dp.channel_post_handler = lambda *a, **k: (lambda fn: fn)
    mock_dp.message_handler = lambda *a, **k: (lambda fn: fn)
    mock_dp.callback_query_handler = lambda *a, **k: (lambda fn: fn)
    aiogram_mod.Dispatcher = MagicMock(return_value=mock_dp)
    aiogram_mod.Bot = MagicMock()

import config
import bot
import database
from handlers import admin
from tasks import watchdog


class TestAdminAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_admins = config.admin_ids
        config.admin_ids = [12345678]

    def tearDown(self):
        config.admin_ids = self.orig_admins

    def _set_audit_mock(self, mock_fn):
        for mod_name in ("bot", "Telegram.bot"):
            m = sys.modules.get(mod_name)
            if m and hasattr(m, "audit_channel_admins"):
                setattr(m, "audit_channel_admins", mock_fn)
        setattr(admin, "audit_channel_admins", mock_fn)
        from services import whatsapp as ws
        setattr(ws, "audit_channel_admins", mock_fn)

    def test_format_admin_audit_report_all_compliant(self):
        """When all accounts are Admins/Owners, report shows 100% compliance."""
        data = {
            "allCompliant": True,
            "totalDestinations": 62,
            "totalConfiguredAccounts": 4,
            "totalReadyAccounts": 4,
            "issuesCount": 0,
            "issues": [],
        }
        text, compliant = admin.format_admin_audit_report(data)
        self.assertTrue(compliant)
        self.assertIn("All Accounts Verified as Admins!", text)
        self.assertIn("100% OK (All Admins/Owners)", text)
        self.assertIn("62", text)

    def test_format_admin_audit_report_issues_detected(self):
        """When an account is NOT admin, report includes mobile number, channel name, ID, and role."""
        data = {
            "allCompliant": False,
            "totalDestinations": 2,
            "totalConfiguredAccounts": 4,
            "totalReadyAccounts": 2,
            "issuesCount": 2,
            "issues": [
                {
                    "destinationId": "120363400714184012@newsletter",
                    "destinationName": "🇨 🇷 🇾 🇵 🇹 🇴",
                    "destinationType": "newsletter",
                    "account": {
                        "clientId": "user",
                        "index": 1,
                        "label": "Account 1",
                        "phone": "+12262406756",
                        "role": "SUBSCRIBER",
                        "error": None,
                    },
                },
                {
                    "destinationId": "120363098765432101@g.us",
                    "destinationName": "VIP Trade Signals",
                    "destinationType": "group",
                    "account": {
                        "clientId": "account2",
                        "index": 2,
                        "label": "Account 2",
                        "phone": "+447812345678",
                        "role": "MEMBER",
                        "error": None,
                    },
                },
            ],
        }
        text, compliant = admin.format_admin_audit_report(data)
        self.assertFalse(compliant)
        self.assertIn("WhatsApp Admin Permission Issues Detected!", text)
        # Verify first issue: mobile, channel name, ID, and role
        self.assertIn("+12262406756", text)
        self.assertIn("🇨 🇷 🇾 🇵 🇹 🇴", text)
        self.assertIn("120363400714184012@newsletter", text)
        self.assertIn("SUBSCRIBER", text)
        # Verify second issue: mobile, channel name, ID, and role
        self.assertIn("+447812345678", text)
        self.assertIn("VIP Trade Signals", text)
        self.assertIn("120363098765432101@g.us", text)
        self.assertIn("MEMBER", text)

    async def test_audit_admins_command_success(self):
        """Running /audit_admins when all accounts are compliant displays success."""
        mock_data = (
            200,
            {
                "allCompliant": True,
                "totalDestinations": 10,
                "totalReadyAccounts": 3,
                "totalConfiguredAccounts": 4,
                "issues": [],
            },
        )
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 12345678
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        msg.reply = AsyncMock(return_value=status_msg)

        mock_audit = AsyncMock(return_value=mock_data)
        self._set_audit_mock(mock_audit)
        try:
            await admin.audit_admins_command(msg)
            msg.reply.assert_called_once()
            status_msg.edit_text.assert_called_once()
            args, kwargs = status_msg.edit_text.call_args
            self.assertIn("All Accounts Verified as Admins!", args[0])
        finally:
            self._set_audit_mock(bot.audit_channel_admins)

    async def test_audit_admins_command_with_issues(self):
        """Running /audit_admins when issues exist lists channel name and mobile number."""
        mock_data = (
            200,
            {
                "allCompliant": False,
                "totalDestinations": 5,
                "totalReadyAccounts": 2,
                "totalConfiguredAccounts": 4,
                "issues": [
                    {
                        "destinationId": "120363319815069334@newsletter",
                        "destinationName": "Forex Gold VIP",
                        "destinationType": "newsletter",
                        "account": {
                            "clientId": "account2",
                            "index": 2,
                            "label": "Account 2",
                            "phone": "+447999888777",
                            "role": "NOT_IN_CHANNEL",
                        },
                    }
                ],
            },
        )
        msg = MagicMock()
        msg.chat.type = "private"
        msg.from_user.id = 12345678
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        msg.reply = AsyncMock(return_value=status_msg)

        mock_audit = AsyncMock(return_value=mock_data)
        self._set_audit_mock(mock_audit)
        try:
            await admin.audit_admins_command(msg)
            status_msg.edit_text.assert_called_once()
            args, kwargs = status_msg.edit_text.call_args
            self.assertIn("+447999888777", args[0])
            self.assertIn("Forex Gold VIP", args[0])
            self.assertIn("NOT_IN_CHANNEL", args[0])
        finally:
            self._set_audit_mock(bot.audit_channel_admins)

    async def test_handle_audit_admins_callback(self):
        """Tapping [ 🛡️ Audit Channel Admins ] button triggers the audit and edits the message."""
        mock_data = (
            200,
            {
                "allCompliant": True,
                "totalDestinations": 15,
                "totalReadyAccounts": 4,
                "totalConfiguredAccounts": 4,
                "issues": [],
            },
        )
        call = MagicMock()
        call.from_user.id = 12345678
        call.answer = AsyncMock()
        call.message.edit_text = AsyncMock()

        mock_audit = AsyncMock(return_value=mock_data)
        self._set_audit_mock(mock_audit)
        try:
            await admin.handle_audit_admins_callback(call)
            call.answer.assert_called_once()
            call.message.edit_text.assert_called_once()
            args, kwargs = call.message.edit_text.call_args
            self.assertIn("All Accounts Verified as Admins!", args[0])
        finally:
            self._set_audit_mock(bot.audit_channel_admins)

    def test_database_get_all_unique_destinations(self):
        """get_all_unique_destinations returns all distinct non-empty group_ids."""
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS channel_groups (id INTEGER PRIMARY KEY, channel_id TEXT, group_id TEXT, UNIQUE(channel_id, group_id))")
        cursor.execute("INSERT OR IGNORE INTO channel_groups (channel_id, group_id) VALUES ('-1001', 'grpA@g.us')")
        cursor.execute("INSERT OR IGNORE INTO channel_groups (channel_id, group_id) VALUES ('-1002', 'grpA@g.us')")
        cursor.execute("INSERT OR IGNORE INTO channel_groups (channel_id, group_id) VALUES ('-1003', 'nlB@newsletter')")
        conn.commit()
        conn.close()

        dests = database.get_all_unique_destinations()
        self.assertIn("grpA@g.us", dests)
        self.assertIn("nlB@newsletter", dests)

    async def test_watchdog_permission_alert(self):
        """Watchdog check_and_alert_admin_permissions alerts admins when active accounts lack admin rights."""
        watchdog._last_admin_permission_alert_time = 0.0
        mock_data = (
            200,
            {
                "allCompliant": False,
                "issues": [
                    {
                        "destinationId": "120363400714184012@newsletter",
                        "destinationName": "Daily Crypto",
                        "account": {
                            "clientId": "user",
                            "index": 1,
                            "label": "Account 1",
                            "phone": "+12262406756",
                            "role": "SUBSCRIBER",
                        },
                    }
                ],
            },
        )
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        mock_audit = AsyncMock(return_value=mock_data)
        self._set_audit_mock(mock_audit)
        try:
            await watchdog.check_and_alert_admin_permissions(mock_bot)
            mock_bot.send_message.assert_called_once()
            args, kwargs = mock_bot.send_message.call_args
            self.assertEqual(args[0], 12345678)
            self.assertIn("+12262406756", args[1])
            self.assertIn("Daily Crypto", args[1])
        finally:
            self._set_audit_mock(bot.audit_channel_admins)


    def test_format_admin_audit_report_angle_bracket_escaping(self):
        """Angle brackets in destination names, IDs, or errors must be HTML escaped."""
        raw_data = {
            "allCompliant": False,
            "totalDestinations": 1,
            "totalReadyAccounts": 1,
            "totalConfiguredAccounts": 4,
            "issues": [
                {
                    "destinationId": "<120363319607678596@newsletter>",
                    "destinationName": "<120363319607678596@newsletter>",
                    "destinationType": "newsletter",
                    "account": {
                        "clientId": "user",
                        "index": 1,
                        "label": "<TestAccount>",
                        "phone": "+12262406756",
                        "role": "SUBSCRIBER",
                        "error": "<some_error_code>",
                    },
                }
            ],
        }
        text, compliant = admin.format_admin_audit_report(raw_data)
        self.assertFalse(compliant)
        # Should not contain unescaped start tags
        self.assertNotIn("<120363319607678596@newsletter>", text)
        self.assertNotIn("<TestAccount>", text)
        self.assertNotIn("<some_error_code>", text)
        # Raw destination IDs should be stripped of surrounding angle brackets
        self.assertIn("120363319607678596@newsletter", text)
        self.assertIn("&lt;TestAccount&gt;", text)
        self.assertIn("&lt;some_error_code&gt;", text)

    def test_database_id_sanitization(self):
        """Database clean_id and clean_destination_id must strip angle brackets and quotes."""
        self.assertEqual(database.clean_id("<-1004486923263>"), "-1004486923263")
        self.assertEqual(database.clean_id("  '<12345>'  "), "12345")
        self.assertEqual(database.clean_destination_id("<120363319607678596@newsletter>"), "120363319607678596@newsletter")
        self.assertEqual(database.clean_destination_id('"123456@g.us"'), "123456@g.us")

    async def test_watchdog_permission_alert_escapes_angle_brackets(self):
        """Watchdog check_and_alert_admin_permissions must escape angle brackets to prevent Telegram parse crash."""
        watchdog._last_admin_permission_alert_time = 0.0
        mock_data = (
            200,
            {
                "allCompliant": False,
                "issues": [
                    {
                        "destinationId": "<120363319607678596@newsletter>",
                        "destinationName": "<120363319607678596@newsletter>",
                        "account": {
                            "clientId": "user",
                            "index": 1,
                            "label": "<Account 1>",
                            "phone": "+12262406756",
                            "role": "NOT ADMIN",
                        },
                    }
                ],
            },
        )
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        mock_audit = AsyncMock(return_value=mock_data)
        self._set_audit_mock(mock_audit)
        try:
            await watchdog.check_and_alert_admin_permissions(mock_bot)
            mock_bot.send_message.assert_called_once()
            args, kwargs = mock_bot.send_message.call_args
            alert_text = args[1]
            # Must not contain unescaped angle bracket tags like <120363319607678596@newsletter>
            self.assertNotIn("<120363319607678596@newsletter>", alert_text)
            self.assertNotIn("<Account 1>", alert_text)
            self.assertIn("120363319607678596@newsletter", alert_text)
            self.assertIn("&lt;Account 1&gt;", alert_text)
        finally:
            self._set_audit_mock(bot.audit_channel_admins)


if __name__ == "__main__":
    unittest.main()
