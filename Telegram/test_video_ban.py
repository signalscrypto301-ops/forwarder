import os
import sys
import unittest
import asyncio
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



class TestVideoForwardingBan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_ban_setting = getattr(config, "BAN_VIDEO_FORWARDING", True)
        config.BAN_VIDEO_FORWARDING = True

    def tearDown(self):
        config.BAN_VIDEO_FORWARDING = self.orig_ban_setting

    def test_pure_video_detected(self):
        """Pure video message (ContentType.VIDEO, no caption) must be identified as video."""
        msg = MagicMock()
        msg.content_type = MockContentType.VIDEO
        msg.video = MagicMock(file_size=5 * 1024 * 1024, file_unique_id="vid123")
        msg.caption = None
        msg.video_note = None
        msg.animation = None
        msg.document = None

        self.assertTrue(bot.is_video_message(msg))

    def test_video_with_attached_caption_detected(self):
        """Message with video and attached caption must be identified as video."""
        msg = MagicMock()
        msg.content_type = MockContentType.VIDEO
        msg.video = MagicMock(file_size=12 * 1024 * 1024, file_unique_id="vid456")
        msg.caption = "Important trade setup video walkthrough"
        msg.video_note = None
        msg.animation = None
        msg.document = None

        self.assertTrue(bot.is_video_message(msg))

    def test_video_note_detected(self):
        """Circular video round notes must be identified as video."""
        msg = MagicMock()
        msg.content_type = MockContentType.VIDEO_NOTE
        msg.video = None
        msg.video_note = MagicMock(file_unique_id="vn123")
        msg.animation = None
        msg.document = None

        self.assertTrue(bot.is_video_message(msg))

    def test_animation_gif_detected(self):
        """Telegram animations/GIFs (MP4) must be identified as video."""
        msg = MagicMock()
        msg.content_type = MockContentType.ANIMATION
        msg.video = None
        msg.video_note = None
        msg.animation = MagicMock(file_unique_id="anim123")
        msg.document = None

        self.assertTrue(bot.is_video_message(msg))

    def test_document_video_by_mime_detected(self):
        """Document sent with video MIME type (video/mp4, video/quicktime) must be identified as video."""
        for mime in ["video/mp4", "video/x-matroska", "video/quicktime", "video/webm"]:
            msg = MagicMock()
            msg.content_type = MockContentType.DOCUMENT
            msg.video = None
            msg.video_note = None
            msg.animation = None
            msg.document = MagicMock(mime_type=mime, file_name="clip.bin")
            self.assertTrue(bot.is_video_message(msg), f"Failed to identify {mime} as video")

    def test_document_video_by_extension_detected(self):
        """Document sent with video file extension (.mp4, .mkv, .mov, etc.) must be identified as video."""
        for ext in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp", ".MP4"]:
            msg = MagicMock()
            msg.content_type = MockContentType.DOCUMENT
            msg.video = None
            msg.video_note = None
            msg.animation = None
            msg.document = MagicMock(mime_type="application/octet-stream", file_name=f"presentation{ext}")
            self.assertTrue(bot.is_video_message(msg), f"Failed to identify {ext} as video")

    def test_allowed_messages_not_flagged(self):
        """Normal text messages, photos, PDFs, and audio must NOT be flagged as video."""
        # 1. Plain text
        msg_text = MagicMock()
        msg_text.content_type = MockContentType.TEXT
        msg_text.video = None
        msg_text.video_note = None
        msg_text.animation = None
        msg_text.document = None
        self.assertFalse(bot.is_video_message(msg_text))

        # 2. Photo
        msg_photo = MagicMock()
        msg_photo.content_type = MockContentType.PHOTO
        msg_photo.video = None
        msg_photo.video_note = None
        msg_photo.animation = None
        msg_photo.document = None
        self.assertFalse(bot.is_video_message(msg_photo))

        # 3. PDF Document
        msg_doc = MagicMock()
        msg_doc.content_type = MockContentType.DOCUMENT
        msg_doc.video = None
        msg_doc.video_note = None
        msg_doc.animation = None
        msg_doc.document = MagicMock(mime_type="application/pdf", file_name="statement.pdf")
        self.assertFalse(bot.is_video_message(msg_doc))

        # 4. Audio / Voice
        msg_audio = MagicMock()
        msg_audio.content_type = MockContentType.AUDIO
        msg_audio.video = None
        msg_audio.video_note = None
        msg_audio.animation = None
        msg_audio.document = None
        self.assertFalse(bot.is_video_message(msg_audio))

    async def test_handle_channel_post_drops_video_early(self):
        """handle_channel_post must immediately drop video without querying groups or downloading."""
        msg = MagicMock()
        msg.chat.id = -1001234567890
        msg.message_id = 999
        msg.content_type = MockContentType.VIDEO
        msg.video = MagicMock(file_size=2 * 1024 * 1024, file_unique_id="vid_test")
        msg.caption = "BTC Analysis Video"
        msg.video_note = None
        msg.animation = None
        msg.document = None

        with patch("bot.get_groups_for_channel") as mock_get_groups, \
             patch("bot.update_channel_last_post") as mock_update_last, \
             patch("bot.send_to_single_group") as mock_send:
            await bot.handle_channel_post(msg)

            # Verification: Post was dropped immediately, no groups queried, no media sent
            mock_get_groups.assert_not_called()
            mock_send.assert_not_called()

    async def test_handle_channel_post_drops_document_video(self):
        """handle_channel_post must drop document messages that contain video files."""
        msg = MagicMock()
        msg.chat.id = -1001234567890
        msg.message_id = 1000
        msg.content_type = MockContentType.DOCUMENT
        msg.video = None
        msg.video_note = None
        msg.animation = None
        msg.document = MagicMock(mime_type="video/mp4", file_name="trading_course.mp4")

        with patch("bot.get_groups_for_channel") as mock_get_groups, \
             patch("bot.send_to_single_group") as mock_send:
            await bot.handle_channel_post(msg)

            mock_get_groups.assert_not_called()
            mock_send.assert_not_called()

    async def test_send_to_single_group_rejects_video(self):
        """send_to_single_group must refuse to send video content type or video files."""
        # 1. content_type="video"
        result1 = await bot.send_to_single_group(
            group="12036301@g.us",
            downloaded_media=None,
            caption="test video",
            content_type="video",
        )
        self.assertFalse(result1)

        # 2. downloaded_media with .mp4 extension
        result2 = await bot.send_to_single_group(
            group="12036301@g.us",
            downloaded_media="media/sample.mp4",
            caption="test video file",
            content_type="doc",
        )
        self.assertFalse(result2)

    async def test_retry_all_failed_messages_skips_video_dlq(self):
        """retry_all_failed_messages must skip items in DLQ where content_type is video."""
        mock_items = [
            {
                "id": 1,
                "channel_id": "-1001",
                "group_id": "12036301@g.us",
                "content_type": "video",
                "caption": "video in dlq",
                "original_filename": "vid.mp4",
                "media_path": "media/vid.mp4",
            },
        ]

        with patch.object(bot, "get_failed_messages", return_value=mock_items), \
             patch("bot.send_to_single_group") as mock_send:
            recovered, failed = await bot.retry_all_failed_messages()

            # Video item was skipped entirely, never attempted via send_to_single_group
            mock_send.assert_not_called()
            self.assertEqual(recovered, 0)
            self.assertEqual(failed, 0)


if __name__ == "__main__":
    unittest.main()
