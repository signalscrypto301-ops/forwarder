import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
import time
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

import bot
import database
from database import (
    record_forwarded_message,
    get_forwarded_messages,
    delete_forwarded_message_records,
    prune_old_forwarded_messages,
    clean_id,
)
from services.forwarder import (
    handle_edited_channel_post,
    delete_forwarded_post,
    handle_channel_post,
)


class TestSyncEditsDeletions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        self.test_db = os.path.join(os.path.dirname(__file__), "test_sync.db")
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass
        database.DB_PATH = self.test_db
        database.create_table()
        self.test_cid = "9988776655"

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass

    def test_database_mapping_crud(self):
        tg_mid = 101
        key1 = {"id": "WA101", "remoteJid": "group1@g.us", "fromMe": True}
        key2 = {"id": "WA102", "remoteJid": "news1@newsletter", "fromMe": True}

        # 1. Record mappings
        record_forwarded_message(
            channel_id=f"-100{self.test_cid}",
            tg_message_id=tg_mid,
            group_id="group1@g.us",
            wa_message_id="WA101",
            wa_key=key1,
            sender_id="user",
        )
        record_forwarded_message(
            channel_id=self.test_cid,
            tg_message_id=tg_mid,
            group_id="news1@newsletter",
            wa_message_id="WA102",
            wa_key=key2,
            sender_id="account2",
        )

        # 2. Retrieve mappings
        records = get_forwarded_messages(f"-100{self.test_cid}", tg_mid)
        self.assertEqual(len(records), 2)
        r_groups = {r["group_id"]: r for r in records}
        self.assertIn("group1@g.us", r_groups)
        self.assertIn("news1@newsletter", r_groups)
        self.assertEqual(r_groups["group1@g.us"]["wa_key"]["id"], "WA101")
        self.assertEqual(r_groups["news1@newsletter"]["sender_id"], "account2")

        # 3. Delete records
        delete_forwarded_message_records(self.test_cid, tg_mid)
        self.assertEqual(len(get_forwarded_messages(self.test_cid, tg_mid)), 0)

    def test_database_pruning(self):
        cid = clean_id(self.test_cid)
        with sqlite3.connect(database.DB_PATH) as conn:
            # Insert record from 20 days ago
            conn.execute(
                """
                INSERT INTO forwarded_messages (channel_id, tg_message_id, group_id, wa_message_id, wa_key_json, sender_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-20 days'))
                """,
                (cid, 999, "grp@g.us", "OLD_MID", "{}", "user"),
            )
            # Insert record from today
            conn.execute(
                """
                INSERT INTO forwarded_messages (channel_id, tg_message_id, group_id, wa_message_id, wa_key_json, sender_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (cid, 1000, "grp@g.us", "NEW_MID", "{}", "user"),
            )
            conn.commit()

        pruned = prune_old_forwarded_messages(max_age_days=14)
        self.assertGreaterEqual(pruned, 1)

        # Check that old record is gone and new remains
        old_recs = get_forwarded_messages(self.test_cid, 999)
        new_recs = get_forwarded_messages(self.test_cid, 1000)
        self.assertEqual(len(old_recs), 0)
        self.assertEqual(len(new_recs), 1)

    async def test_handle_edited_channel_post_text(self):
        tg_mid = 202
        key1 = {"id": "WA202_1", "remoteJid": "grp1@g.us", "fromMe": True}
        key2 = {"id": "WA202_2", "remoteJid": "news1@newsletter", "fromMe": True}

        record_forwarded_message(self.test_cid, tg_mid, "grp1@g.us", "WA202_1", key1, "user")
        record_forwarded_message(self.test_cid, tg_mid, "news1@newsletter", "WA202_2", key2, "account2")

        msg = MagicMock()
        msg.chat.id = int(f"-100{self.test_cid}")
        msg.message_id = tg_mid
        msg.text = "Updated: *Take Profit 1 Reached!* 🚀"
        msg.caption = None
        msg.entities = []
        msg.caption_entities = []

        calls = []

        async def fake_post(endpoint, payload, timeout_sec=30):
            calls.append((endpoint, payload))
            return 200, {"success": True}

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            await handle_edited_channel_post(msg)

        self.assertEqual(len(calls), 2)
        endpoints = [c[0] for c in calls]
        self.assertTrue(all(ep == "editMessage" for ep in endpoints))

        payloads = {c[1]["groupId"]: c[1] for c in calls}
        self.assertIn("grp1@g.us", payloads)
        self.assertIn("news1@newsletter", payloads)
        self.assertEqual(payloads["grp1@g.us"]["clientId"], "user")
        self.assertEqual(payloads["news1@newsletter"]["clientId"], "account2")
        self.assertIn("Take Profit 1 Reached", payloads["grp1@g.us"]["text"])

    async def test_handle_edited_channel_post_caption(self):
        tg_mid = 203
        key = {"id": "WA203", "remoteJid": "news@newsletter", "fromMe": True}
        record_forwarded_message(self.test_cid, tg_mid, "news@newsletter", "WA203", key, "user")

        msg = MagicMock()
        msg.chat.id = int(f"-100{self.test_cid}")
        msg.message_id = tg_mid
        msg.text = None
        msg.caption = "Chart update: target hit."
        msg.entities = []
        msg.caption_entities = []

        calls = []

        async def fake_post(endpoint, payload, timeout_sec=30):
            calls.append((endpoint, payload))
            return 200, {"success": True}

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            await handle_edited_channel_post(msg)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "editMessage")
        self.assertEqual(calls[0][1]["text"], "Chart update: target hit.")

    async def test_handle_edited_channel_post_skips_when_empty_or_no_mapping(self):
        calls = []

        async def fake_post(endpoint, payload, timeout_sec=30):
            calls.append((endpoint, payload))
            return 200, {"success": True}

        # 1. No mapping exists for this message ID
        msg1 = MagicMock()
        msg1.chat.id = int(f"-100{self.test_cid}")
        msg1.message_id = 99999
        msg1.text = "Hello world"
        msg1.caption = None
        msg1.entities = []

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            await handle_edited_channel_post(msg1)

        self.assertEqual(len(calls), 0)

        # 2. Text and caption are empty
        msg2 = MagicMock()
        msg2.chat.id = int(f"-100{self.test_cid}")
        msg2.message_id = 202
        msg2.text = None
        msg2.caption = None

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            await handle_edited_channel_post(msg2)

        self.assertEqual(len(calls), 0)

    async def test_delete_forwarded_post(self):
        tg_mid = 303
        key1 = {"id": "WA303_1", "remoteJid": "grp1@g.us", "fromMe": True}
        key2 = {"id": "WA303_2", "remoteJid": "news1@newsletter", "fromMe": True}

        record_forwarded_message(self.test_cid, tg_mid, "grp1@g.us", "WA303_1", key1, "user")
        record_forwarded_message(self.test_cid, tg_mid, "news1@newsletter", "WA303_2", key2, "account3")

        calls = []

        async def fake_post(endpoint, payload, timeout_sec=30):
            calls.append((endpoint, payload))
            return 200, {"success": True}

        with patch.object(bot, "post_whatsapp_json", AsyncMock(side_effect=fake_post)):
            deleted_count = await delete_forwarded_post(self.test_cid, tg_mid)

        self.assertEqual(deleted_count, 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[0] == "deleteMessage" for c in calls))

        remaining = get_forwarded_messages(self.test_cid, tg_mid)
        self.assertEqual(len(remaining), 0)

    async def test_telethon_message_deleted_event(self):
        calls = []

        async def fake_del(cid, mid):
            calls.append((cid, mid))
            return 1

        with patch.object(bot, "delete_forwarded_post", AsyncMock(side_effect=fake_del)):
            event = MagicMock()
            event.chat_id = int(f"-100{self.test_cid}")
            event.deleted_ids = [501, 502, 503]

            ch_str = str(event.chat_id)
            for mid in event.deleted_ids:
                await bot.delete_forwarded_post(ch_str, mid)

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], (f"-100{self.test_cid}", 501))
        self.assertEqual(calls[1], (f"-100{self.test_cid}", 502))
        self.assertEqual(calls[2], (f"-100{self.test_cid}", 503))

    async def test_live_image_adaptation_triggered_for_newsletter(self):
        from aiogram.types import ContentType

        msg = MagicMock()
        msg.chat.id = int(f"-100{self.test_cid}")
        msg.message_id = 601
        msg.content_type = ContentType.PHOTO
        msg.video = None
        msg.video_note = None
        msg.animation = None
        msg.document = None
        msg.caption = "Trade chart"
        msg.caption_entities = []
        msg.media_group_id = None
        photo_mock = MagicMock()
        photo_mock.file_unique_id = "test_photo_uid"

        async def fake_download(destination_file):
            os.makedirs(os.path.dirname(destination_file), exist_ok=True)
            with open(destination_file, "wb") as f:
                f.write(b"fake image data")

        photo_mock.download = AsyncMock(side_effect=fake_download)
        msg.photo = [photo_mock]

        cid = clean_id(self.test_cid)
        database.add_channel(cid)
        database.add_group_for_channel(cid, "120363@newsletter")
        database.add_group_for_channel(cid, "general@g.us")

        adapt_mock = MagicMock(side_effect=lambda p: p)
        send_mock = AsyncMock()

        with patch.object(bot, "adapt_image_for_whatsapp_channel", adapt_mock), \
             patch.object(bot, "send_to_single_group", send_mock):

            await bot.handle_channel_post(msg)
            self.assertEqual(adapt_mock.call_count, 1)

        database.delete_group_for_channel(cid, "120363@newsletter")
        adapt_mock_std = MagicMock(side_effect=lambda p: p)

        with patch.object(bot, "adapt_image_for_whatsapp_channel", adapt_mock_std), \
             patch.object(bot, "send_to_single_group", send_mock):

            await bot.handle_channel_post(msg)
            self.assertEqual(adapt_mock_std.call_count, 0)

    async def test_handle_channel_post_adapts_image_document_for_newsletter(self):
        """Image documents (e.g. uncompressed chart PNG/JPG) forwarded to @newsletter should be adapted."""
        msg = MagicMock()
        msg.chat.id = self.test_cid
        msg.message_id = 901
        msg.content_type = "document"
        msg.text = None
        msg.caption = "Chart Document"
        msg.caption_entities = []
        msg.media_group_id = None
        msg.video = None
        msg.video_note = None
        msg.animation = None

        doc_mock = MagicMock()
        doc_mock.file_unique_id = "doc_img_901"
        doc_mock.file_name = "chart.jpg"
        doc_mock.mime_type = "image/jpeg"
        doc_mock.file_size = 500000

        async def fake_doc_download(destination_file):
            with open(destination_file, "wb") as f:
                f.write(b"fake_jpeg_data")

        doc_mock.download = AsyncMock(side_effect=fake_doc_download)
        msg.document = doc_mock

        cid = clean_id(self.test_cid)
        database.add_channel(cid)
        database.add_group_for_channel(cid, "120363@newsletter")

        adapt_mock = MagicMock(side_effect=lambda p: p)
        send_mock = AsyncMock()

        with patch.object(bot, "adapt_image_for_whatsapp_channel", adapt_mock), \
             patch.object(bot, "send_to_single_group", send_mock):

            await bot.handle_channel_post(msg)
            self.assertEqual(adapt_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()