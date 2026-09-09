import os
import sys
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timedelta

for mod in ['yaml', 'aiogram', 'aiogram.utils', 'aiogram.utils.executor', 'aiogram.types', 'telethon', 'telethon.sessions', 'qrcode', 'aiohttp', 'requests', 'psutil']:
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
    def __init__(self, text='', callback_data=''):
        self.text = text
        self.callback_data = callback_data

import aiogram.types
aiogram.types.InlineKeyboardMarkup = MockInlineKeyboardMarkup
aiogram.types.InlineKeyboardButton = MockInlineKeyboardButton

for attr in ['TEXT', 'PHOTO', 'VIDEO', 'DOCUMENT', 'VOICE', 'AUDIO']:
    setattr(aiogram.types.ContentType, attr, attr.lower())

import database
import bot
from bot import generate_stale_channels_digest, build_channel_detail_keyboard

class TestStaleChannelsDetector(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        self.test_db = os.path.join(os.path.dirname(__file__), 'test_stale.db')
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

    def test_channel_initialization_timestamp(self):
        database.add_channel('-1001111111111')
        last_post = database.get_channel_last_post('-1001111111111')
        self.assertIsNotNone(last_post)
        self.assertTrue(len(last_post) > 0)

    def test_update_channel_last_post(self):
        cid = '-1002222222222'
        database.add_channel(cid)
        past_time = (datetime.now() - timedelta(hours=80)).strftime('%Y-%m-%d %H:%M:%S')
        database.update_channel_last_post(cid, past_time)
        self.assertEqual(database.get_channel_last_post(cid), past_time)

        now = datetime.now()
        database.update_channel_last_post(cid)
        updated_post = database.get_channel_last_post(cid)
        dt = datetime.strptime(updated_post, '%Y-%m-%d %H:%M:%S')
        self.assertLessEqual((now - dt).total_seconds(), 5)

    def test_get_stale_channels_threshold(self):
        c_active = '-1003333333333'
        c_stale_80h = '-1004444444444'
        c_stale_100h = '-1005555555555'
        database.add_channel(c_active)
        database.add_channel(c_stale_80h)
        database.add_channel(c_stale_100h)

        now = datetime.now()
        database.update_channel_last_post(c_active, (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'))
        database.update_channel_last_post(c_stale_80h, (now - timedelta(hours=80)).strftime('%Y-%m-%d %H:%M:%S'))
        database.update_channel_last_post(c_stale_100h, (now - timedelta(hours=100)).strftime('%Y-%m-%d %H:%M:%S'))

        database.add_group_for_channel(c_stale_80h, '120363001@g.us')
        database.add_group_for_channel(c_stale_80h, '120363002@g.us')
        database.add_group_for_channel(c_stale_100h, '120363003@g.us')

        stale = database.get_stale_channels(threshold_hours=72)
        stale_ids = [s['channel_id'] for s in stale]

        self.assertNotIn(c_active, stale_ids)
        self.assertIn(c_stale_80h, stale_ids)
        self.assertIn(c_stale_100h, stale_ids)
        self.assertEqual(len(stale), 2)

        for s in stale:
            if s['channel_id'] == c_stale_80h:
                self.assertGreaterEqual(s['hours_inactive'], 79)
                self.assertEqual(s['groups_count'], 2)
            elif s['channel_id'] == c_stale_100h:
                self.assertGreaterEqual(s['hours_inactive'], 99)
                self.assertEqual(s['groups_count'], 1)

    def test_generate_stale_channels_digest_format(self):
        stale_data = [
            {
                'channel_id': '-1001234567890',
                'last_post_at': '2026-09-04 10:00:00',
                'hours_inactive': 78,
                'is_paused': False,
                'groups_count': 2,
            },
            {
                'channel_id': '-1009876543210',
                'last_post_at': '2026-09-03 18:00:00',
                'hours_inactive': 94,
                'is_paused': True,
                'groups_count': 1,
            },
        ]

        digest = generate_stale_channels_digest(stale_data, threshold_hours=72)
        self.assertIn('Stale Channel Alert: 2 channels have had no new posts in 72 hours.', digest)
        self.assertIn('-1001234567890', digest)
        self.assertIn('Inactive for 78h (2 groups)', digest)
        self.assertIn('-1009876543210', digest)
        self.assertIn('Inactive for 94h (1 group)', digest)
        self.assertIn('[PAUSED]', digest)
        self.assertIn('Check if bot permissions were revoked', digest)

    def test_empty_stale_channels_digest(self):
        digest = generate_stale_channels_digest([], threshold_hours=72)
        self.assertIn('0 channels have had no new posts in 72 hours', digest)
        self.assertIn('All monitored channels are active', digest)

    def test_build_channel_detail_keyboard_displays_last_activity(self):
        cid = '-1006666666666'
        database.add_channel(cid)
        test_time = '2026-09-07 14:30:00'
        database.update_channel_last_post(cid, test_time)

        text, kb = build_channel_detail_keyboard(cid)
        self.assertIn('Last Activity:', text)
        self.assertIn(test_time, text)

if __name__ == '__main__':
    unittest.main()