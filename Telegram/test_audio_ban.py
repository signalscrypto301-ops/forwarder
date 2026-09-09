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

class MockContentType:
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    ANIMATION = "animation"
    DOCUMENT = "document"
    VOICE = "voice"
    AUDIO = "audio"


aiogram_types = sys.modules.get("aiogram.types")
if aiogram_types:
    aiogram_types.ContentType = MockContentType

import config
import bot
from handlers import dlq


class TestAudioForwardingBan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_ban_setting = getattr(config, "BAN_AUDIO_FORWARDING", True)
        config.BAN_AUDIO_FORWARDING = True

    def tearDown(self):
        config.BAN_AUDIO_FORWARDING = self.orig_ban_setting

    def test_pure_audio_detected(self):
        """Pure audio message (ContentType.AUDIO) must be identified as audio."""
        msg = MagicMock()
        msg.content_type = MockContentType.AUDIO
        msg.audio = MagicMock(file_size=3 * 1024 * 1024, file_unique_id="aud123")
        msg.voice = None
        msg.document = None
        msg.caption = None

        self.assertTrue(bot.is_audio_message(msg))

    def test_voice_note_detected(self):
        """Voice note message (ContentType.VOICE) must be identified as audio."""
        msg = MagicMock()
        msg.content_type = MockContentType.VOICE
        msg.audio = None
        msg.voice = MagicMock(file_size=500 * 1024, file_unique_id="voice123")
        msg.document = None
        msg.caption = None

        self.assertTrue(bot.is_audio_message(msg))

    def test_audio_with_caption_detected(self):
        """Audio message with attached caption must be identified as audio."""
        msg = MagicMock()
        msg.content_type = MockContentType.AUDIO
        msg.audio = MagicMock(file_size=4 * 1024 * 1024, file_unique_id="aud456")
        msg.voice = None
        msg.document = None
        msg.caption = "Important audio podcast"

        self.assertTrue(bot.is_audio_message(msg))

    def test_document_audio_by_mime_detected(self):
        """Document sent with audio MIME type (audio/mpeg, audio/ogg, etc.) must be identified as audio."""
        for mime in ["audio/mpeg", "audio/ogg", "audio/wav", "audio/x-m4a", "audio/flac", "audio/aac"]:
            msg = MagicMock()
            msg.content_type = MockContentType.DOCUMENT
            msg.audio = None
            msg.voice = None
            msg.document = MagicMock(mime_type=mime, file_name="recording.bin")
            self.assertTrue(bot.is_audio_message(msg), f"Failed to identify {mime} as audio")

    def test_document_audio_by_extension_detected(self):
        """Document sent with audio file extension (.mp3, .m4a, .wav, etc.) must be identified as audio."""
        for ext in [".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".wma", ".MP3"]:
            msg = MagicMock()
            msg.content_type = MockContentType.DOCUMENT
            msg.audio = None
            msg.voice = None
            msg.document = MagicMock(mime_type="application/octet-stream", file_name=f"speech{ext}")
            self.assertTrue(bot.is_audio_message(msg), f"Failed to identify {ext} as audio")

    def test_allowed_messages_not_flagged(self):
        """Normal text messages, photos, PDFs, and video files must NOT be flagged as audio."""
        # 1. Plain text
        msg_text = MagicMock()
        msg_text.content_type = MockContentType.TEXT
        msg_text.audio = None
        msg_text.voice = None
        msg_text.document = None
        self.assertFalse(bot.is_audio_message(msg_text))

        # 2. Photo
        msg_photo = MagicMock()
        msg_photo.content_type = MockContentType.PHOTO
        msg_photo.audio = None
        msg_photo.voice = None
        msg_photo.document = None
        self.assertFalse(bot.is_audio_message(msg_photo))

        # 3. PDF Document
        msg_doc = MagicMock()
        msg_doc.content_type = MockContentType.DOCUMENT
        msg_doc.audio = None
        msg_doc.voice = None
        msg_doc.document = MagicMock(mime_type="application/pdf", file_name="invoice.pdf")
        self.assertFalse(bot.is_audio_message(msg_doc))

    @patch("services.forwarder.send_to_single_group", new_callable=AsyncMock)
    @patch("services.forwarder.update_channel_last_post")
    @patch("services.forwarder.record_channel_post_activity")
    async def test_handle_channel_post_drops_audio(self, mock_act, mock_upd, mock_send):
        """handle_channel_post should immediately drop audio messages when BAN_AUDIO_FORWARDING is True."""
        msg = MagicMock()
        msg.chat = MagicMock(id=-1001234567890)
        msg.message_id = 999
        msg.content_type = MockContentType.AUDIO
        msg.audio = MagicMock(file_size=2 * 1024 * 1024, file_unique_id="aud999")
        msg.voice = None
        msg.document = None
        msg.caption = "Daily Audio Recap"

        await bot.handle_channel_post(msg)

        mock_send.assert_not_called()
        mock_upd.assert_not_called()
        mock_act.assert_not_called()

    @patch("services.forwarder.send_to_single_group", new_callable=AsyncMock)
    @patch("services.forwarder.update_channel_last_post")
    @patch("services.forwarder.record_channel_post_activity")
    async def test_handle_channel_post_drops_voice(self, mock_act, mock_upd, mock_send):
        """handle_channel_post should immediately drop voice notes when BAN_AUDIO_FORWARDING is True."""
        msg = MagicMock()
        msg.chat = MagicMock(id=-1001234567890)
        msg.message_id = 1000
        msg.content_type = MockContentType.VOICE
        msg.audio = None
        msg.voice = MagicMock(file_size=300 * 1024, file_unique_id="vox1000")
        msg.document = None
        msg.caption = None

        await bot.handle_channel_post(msg)

        mock_send.assert_not_called()
        mock_upd.assert_not_called()
        mock_act.assert_not_called()

    async def test_send_to_single_group_refuses_audio(self):
        """send_to_single_group must refuse to send audio/voice messages when BAN_AUDIO_FORWARDING is True."""
        # By content_type = 'audio'
        res1 = await bot.send_to_single_group(
            group="123456789@g.us",
            downloaded_media=None,
            caption="Audio note",
            content_type="audio",
        )
        self.assertFalse(res1)

        # By content_type = 'voice'
        res2 = await bot.send_to_single_group(
            group="123456789@g.us",
            downloaded_media=None,
            caption="",
            content_type="voice",
        )
        self.assertFalse(res2)

        # By downloaded media filename ending in audio extension
        res3 = await bot.send_to_single_group(
            group="123456789@g.us",
            downloaded_media="/tmp/audio_clip.mp3",
            caption="Audio document",
            content_type="document",
        )
        self.assertFalse(res3)

    async def test_dlq_skips_audio_retry(self):
        """DLQ retry_all_failed_messages must skip retry for audio and voice items when BAN_AUDIO_FORWARDING is True."""
        mock_items = [
            {
                "id": 1,
                "group_id": "123@g.us",
                "content_type": "audio",
                "caption": "audio failed",
                "original_filename": "song.mp3",
                "media_path": "media/song.mp3",
                "channel_id": "-1001",
            },
            {
                "id": 2,
                "group_id": "123@g.us",
                "content_type": "voice",
                "caption": "",
                "original_filename": "voice.ogg",
                "media_path": "media/voice.ogg",
                "channel_id": "-1001",
            },
        ]

        with patch.object(bot, "get_failed_messages", return_value=mock_items), \
             patch.object(bot, "send_to_single_group", new_callable=AsyncMock) as mock_send:
            recovered, failed = await bot.retry_all_failed_messages()

            # Both items should be skipped entirely, never attempted via send_to_single_group
            mock_send.assert_not_called()
            self.assertEqual(recovered, 0)
            self.assertEqual(failed, 0)


if __name__ == "__main__":
    unittest.main()
