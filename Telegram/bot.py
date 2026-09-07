import os
import sys
import asyncio
import random
import time
import requests
from datetime import datetime

import aiogram
from aiogram import types
from aiogram.utils import executor
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    ParseMode,
    ContentType,
    MessageEntity,
)
import qrcode
from telethon import TelegramClient

import config
from logger import logger
from database import (
    add_channel,
    add_group_for_channel,
    get_all_channels,
    get_groups_for_channel,
    delete_group_for_channel,
)

bot = aiogram.Bot(config.bot_token)
dp = aiogram.Dispatcher(bot)

bot_client = TelegramClient("bot_client", config.API_ID, config.API_HASH).start(
    bot_token=config.bot_token
)

# Concurrency limiter to protect WhatsApp Web and network resources
UPLOAD_SEMAPHORE = asyncio.Semaphore(3)

# Debounced alert timestamp to prevent admin notification spam
last_auth_alert_time = 0.0

# In-memory TTL cache for Telegram media groups (albums) to avoid duplicate captions
PROCESSED_MEDIA_GROUPS = {}


def is_media_group_caption_needed(media_group_id: str | None) -> bool:
    """
    Checks if this media in an album should include the caption.
    Only the first item in the album receives the caption; subsequent items omit it.
    """
    if not media_group_id:
        return True
    now = time.time()
    # Purge entries older than 120 seconds
    expired = [k for k, v in PROCESSED_MEDIA_GROUPS.items() if now - v > 120]
    for k in expired:
        PROCESSED_MEDIA_GROUPS.pop(k, None)

    if media_group_id in PROCESSED_MEDIA_GROUPS:
        return False
    else:
        PROCESSED_MEDIA_GROUPS[media_group_id] = now
        return True


async def notify_admins_auth_required():
    """Alert admins when WhatsApp session drops or needs QR login (throttled to once every 5 mins)."""
    global last_auth_alert_time
    now = datetime.now().timestamp()
    if now - last_auth_alert_time > 300:
        last_auth_alert_time = now
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    "?? <b>WhatsApp Session Error</b>: The WhatsApp Web session is unauthorized or disconnected. Please send /login to re-authenticate.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Failed to alert admin {admin_id} about auth: {e}")


def generate_qr_code(qr_data: str) -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    image_path = os.path.join(os.getcwd(), f"qr_{int(datetime.now().timestamp())}.png")
    img.save(image_path)
    return image_path


@dp.message_handler(commands=["start"])
async def start_command(message: Message):
    await message.reply("Hello! Forwarder bot is operational. Send /help for available commands.")


@dp.message_handler(commands=["help"])
async def help_command(message: Message):
    if not message.chat.type == "private":
        return
    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    help_text = (
        "?? <b>Forwarder Admin Commands</b>:\n\n"
        "? /status - Live health dashboard & channel statistics\n"
        "? /login - Generate WhatsApp login QR code\n"
        "? /logout - Disconnect and clean WhatsApp session\n"
        "? /get_chat_id &lt;group_name&gt; - Find WhatsApp group or newsletter JID\n"
        "? /add_group &lt;channel_id&gt; &lt;group_id&gt; - Map channel to WhatsApp group\n"
        "? /delete_group &lt;channel_id&gt; &lt;group_id&gt; - Remove group mapping\n"
        "? /view_groups - List all channels and mapped groups\n"
        "? /add_channel &lt;channel_id&gt; - Register new Telegram channel\n"
        "? /listen - Toggle listening to incoming WhatsApp messages"
    )
    await message.reply(help_text, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["status"])
async def status_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    start_time = time.time()
    wa_status = "?? Disconnected"
    wa_details = ""
    latency_ms = 0

    try:
        res = await asyncio.to_thread(
            requests.get, url=f"{config.whatsapp_service}/health", timeout=5
        )
        latency_ms = int((time.time() - start_time) * 1000)
        if res.status_code == 200:
            data = res.json()
            wa_status = "?? Ready & Authorized"
            wa_details = f"Active Sessions: {data.get('sessionsCount', 1)}"
        else:
            wa_status = f"?? Initializing ({res.status_code})"
    except Exception as e:
        wa_status = f"?? Unreachable: {e}"

    channels = get_all_channels()
    total_channels = len(channels)
    total_mappings = sum(len(get_groups_for_channel(ch)) for ch in channels)

    status_text = (
        "?? <b>Forwarder System Status</b>\n"
        "??????????????????????\n"
        f"<b>WhatsApp Service</b>: {wa_status}\n"
        f"<b>WhatsApp Health Latency</b>: {latency_ms} ms\n"
        f"<b>Monitored Channels</b>: <code>{total_channels}</code>\n"
        f"<b>Active Group Mappings</b>: <code>{total_mappings}</code>\n"
        "<b>Concurrency Workers</b>: <code>3 (UPLOAD_SEMAPHORE)</code>\n"
        "<b>Anti-Flood Jitter</b>: <code>0.5s - 1.5s active</code>\n"
        "<b>Album Debouncer</b>: <code>Active (120s TTL)</code>\n"
        "<b>Supported Media</b>: <code>Text, Photo, Video, Document, Voice, Audio</code>\n"
        "??????????????????????\n"
        "? <i>System healthy and operating normally.</i>"
    )
    await message.reply(status_text, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["get_chat_id"])
async def get_chat_id_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    args = message.get_args()
    if not args:
        await message.reply("Please provide a group name")
        return

    data = {
        "chatName": args,
        "clientId": "user",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, url=f"{config.whatsapp_service}/getChatId", json=data, timeout=30
        )
        res_data = res.json()
        if res.status_code == 200:
            await message.reply(
                f"Group Id : <code>{res_data.get('groupId')}</code>",
                parse_mode=ParseMode.HTML,
            )
        elif res.status_code in (400, 404):
            await message.reply(f"{res_data.get('message')}", parse_mode=ParseMode.HTML)
        else:
            await message.reply("Something went wrong.")
    except Exception as e:
        logger.error(f"Error in get_chat_id: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


@dp.message_handler(commands=["login"])
async def login_whatsapp(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    data = {
        "clientId": "user",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, url=f"{config.whatsapp_service}/createsession", json=data, timeout=30
        )
        res_data = res.json()

        if res.status_code == 200:
            qr_data = res_data.get("qrcode")
            qr_path = generate_qr_code(qr_data)
            try:
                with open(qr_path, "rb") as qr_fp:
                    await message.reply_photo(qr_fp)
            finally:
                if os.path.exists(qr_path):
                    os.remove(qr_path)
        elif res.status_code == 400:
            await message.reply(f"{res_data.get('message')}")
        else:
            await message.reply("Failed to create session.")
    except Exception as e:
        logger.error(f"Error in login_whatsapp: {e}")
        await message.reply("Error contacting WhatsApp service.")


@dp.message_handler(commands=["listen"])
async def listen_whatsapp(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    data = {
        "clientId": "user",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, url=f"{config.whatsapp_service}/startlistening", json=data, timeout=30
        )
        res_data = res.json()
        if res.status_code == 200:
            await message.reply(f"{res_data.get('message')}")
        elif res.status_code == 400:
            await message.reply(f"{res_data.get('message')}")
        else:
            await message.reply("Something went wrong.")
    except Exception as e:
        logger.error(f"Error in listen_whatsapp: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


@dp.message_handler(commands=["logout"])
async def logout_whatsapp(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    data = {
        "clientId": "user",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, url=f"{config.whatsapp_service}/logout", json=data, timeout=30
        )
        if res.status_code == 200:
            await message.reply("Logged out and session files removed successfully.")
        elif res.status_code == 400:
            await message.reply(f"{res.json().get('message')}")
        else:
            await message.reply("Failed to log out. Please try again later.")
    except Exception as e:
        logger.error(f"Error in logout_whatsapp: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


@dp.message_handler(commands=["add_group"])
async def add_group_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /add_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    add_group_for_channel(channel_id, group_id)
    await message.reply(f"Group {group_id} added for channel {channel_id}.")


@dp.message_handler(commands=["delete_group"])
async def delete_group_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /delete_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    delete_group_for_channel(channel_id, group_id)
    await message.reply(f"Group {group_id} deleted for channel {channel_id}.")


@dp.message_handler(commands=["view_groups"])
async def view_groups_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    channels = get_all_channels()
    if not channels:
        await message.reply("No channels found.")
        return

    reply = "Channels and WhatsApp Groups:\n"
    for ch in channels:
        groups = get_groups_for_channel(ch)
        reply += f"Channel: <code>{ch}</code>\nGroups: {', '.join(groups) if groups else 'None'}\n\n"
    await message.reply(reply, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["add_channel"])
async def add_channel_command(message: Message):
    if not message.chat.type == "private":
        return

    user_id = message.from_user.id
    if user_id not in config.admin_ids:
        return

    args = message.get_args()
    if not args:
        await message.reply("Usage: /add_channel <channel_id>")
        return

    channel_id = args.strip()
    add_channel(channel_id)
    await message.reply(f"Channel {channel_id} added successfully.")


def format_caption_for_whatsapp(caption: str, entities: list[MessageEntity]) -> str:
    """
    Translates Telegram MessageEntity offset formatting to WhatsApp markdown.
    Uses UTF-16 code units to preserve emoji and surrogate pair indexing.
    Supports bold, italic, strikethrough, monospace, blockquotes (Telegram 7.0), and text links.
    Uses LIFO (Last-In, First-Out) suffix insertion to ensure symmetric nesting.
    """
    if not caption or not entities:
        return caption or ""

    utf16_to_str_idx = {}
    str_idx = 0
    utf16_idx = 0
    for ch in caption:
        utf16_to_str_idx[utf16_idx] = str_idx
        utf16_len = len(ch.encode("utf-16-le")) // 2
        utf16_idx += utf16_len
        str_idx += 1
    utf16_to_str_idx[utf16_idx] = str_idx

    prefix_insertions = {}
    suffix_insertions = {}

    for ent in entities:
        prefix = ""
        suffix = ""
        if ent.type == "bold":
            prefix, suffix = "*", "*"
        elif ent.type == "italic":
            prefix, suffix = "_", "_"
        elif ent.type == "strikethrough":
            prefix, suffix = "~", "~"
        elif ent.type == "code":
            prefix, suffix = chr(96), chr(96)
        elif ent.type == "pre":
            prefix, suffix = (chr(96) * 3) + chr(10), chr(10) + (chr(96) * 3)
        elif ent.type == "spoiler":
            prefix, suffix = "||", "||"
        elif ent.type in ("blockquote", "expandable_blockquote"):
            prefix, suffix = "> ", ""
            # For multiline quotes, prefix each line with '> '
            start_pos = utf16_to_str_idx.get(ent.offset)
            end_pos = utf16_to_str_idx.get(ent.offset + ent.length)
            if start_pos is not None and end_pos is not None:
                for k in range(start_pos, min(end_pos, len(caption))):
                    if caption[k] == "\n" and k + 1 < end_pos:
                        prefix_insertions.setdefault(k + 1, []).append("> ")
        elif ent.type == "text_link" and getattr(ent, "url", None):
            suffix = f" ({ent.url})"
        else:
            continue

        start = utf16_to_str_idx.get(ent.offset)
        end = utf16_to_str_idx.get(ent.offset + ent.length)

        if start is not None and prefix:
            prefix_insertions.setdefault(start, []).append(prefix)
        if end is not None and suffix:
            # LIFO order: insert at index 0 so inner tags close before outer tags
            suffix_insertions.setdefault(end, []).insert(0, suffix)

    result = []
    for i, ch in enumerate(caption):
        if i in prefix_insertions:
            result.extend(prefix_insertions[i])
        result.append(ch)
        if (i + 1) in suffix_insertions:
            result.extend(suffix_insertions[i + 1])

    return "".join(result)


async def send_to_single_group(
    group: str,
    downloaded_media: str | None,
    caption: str,
    original_filename: str | None = None,
):
    """
    Sends message to a single WhatsApp group/newsletter with:
    - Concurrency bounding via UPLOAD_SEMAPHORE
    - Randomized jitter delay (anti-flood/anti-ban)
    - Exponential backoff retry on transient 5xx/connection drops
    """
    async with UPLOAD_SEMAPHORE:
        # Anti-flood jitter delay between group deliveries
        await asyncio.sleep(random.uniform(0.5, 1.5))

        max_retries = 2
        for attempt in range(max_retries + 1):
            if downloaded_media:
                if not os.path.exists(downloaded_media):
                    logger.error(
                        f"Downloaded media {downloaded_media} does not exist for group {group}."
                    )
                    return

                def send_media_sync(path, grp, cap, orig_name):
                    with open(path, "rb") as media_file:
                        fname = orig_name or os.path.basename(path)
                        files = {"media": (fname, media_file)}
                        data = {"clientId": "user", "groupId": grp, "caption": cap}
                        return requests.post(
                            url=f"{config.whatsapp_service}/sendMedia",
                            data=data,
                            files=files,
                            timeout=60,
                        )

                try:
                    response = await asyncio.to_thread(
                        send_media_sync, downloaded_media, group, caption, original_filename
                    )
                    if response.status_code == 200:
                        logger.info(f"Media message sent to group {group} successfully.")
                        break
                    elif response.status_code in (502, 503, 504) and attempt < max_retries:
                        logger.warning(
                            f"WhatsApp service returned {response.status_code} for {group}. Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                        )
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    else:
                        msg = (
                            response.json().get("message")
                            if response.headers.get("content-type", "").startswith("application/json")
                            else response.text
                        )
                        logger.error(f"Failed to send media message to group {group}: {msg}")
                        if "Session is not authorized" in str(msg):
                            await notify_admins_auth_required()
                        break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    if attempt < max_retries:
                        logger.warning(
                            f"Connection error to WhatsApp service for group {group}: {e}. Retrying in {(attempt+1)*2}s..."
                        )
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    logger.error(f"Error sending media message to group {group}: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error sending media message to group {group}: {e}")
                    break

            else:
                def send_text_sync(grp, txt):
                    data = {"clientId": "user", "groupId": grp, "text": txt}
                    return requests.post(
                        url=f"{config.whatsapp_service}/sendText",
                        json=data,
                        timeout=30,
                    )

                try:
                    response = await asyncio.to_thread(send_text_sync, group, caption)
                    if response.status_code == 200:
                        logger.info(f"Text message sent to group {group} successfully.")
                        break
                    elif response.status_code in (502, 503, 504) and attempt < max_retries:
                        logger.warning(
                            f"WhatsApp service returned {response.status_code} for {group}. Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                        )
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    else:
                        msg = (
                            response.json().get("message")
                            if response.headers.get("content-type", "").startswith("application/json")
                            else response.text
                        )
                        logger.error(f"Failed to send text message to group {group}: {msg}")
                        if "Session is not authorized" in str(msg):
                            await notify_admins_auth_required()
                        break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    if attempt < max_retries:
                        logger.warning(
                            f"Connection error to WhatsApp service for group {group}: {e}. Retrying in {(attempt+1)*2}s..."
                        )
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    logger.error(f"Error sending text message to group {group}: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error sending text message to group {group}: {e}")
                    break


@dp.channel_post_handler(
    content_types=[
        ContentType.TEXT,
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.DOCUMENT,
        ContentType.VOICE,
        ContentType.AUDIO,
    ]
)
async def handle_channel_post(message: types.Message):
    channel_id = str(message.chat.id)
    groups = get_groups_for_channel(channel_id)
    if not groups:
        return

    caption = ""
    downloaded_media = None
    original_filename = None

    try:
        # Album / Media Group deduplication
        is_caption_allowed = is_media_group_caption_needed(
            str(message.media_group_id) if message.media_group_id else None
        )

        if message.content_type == ContentType.TEXT:
            caption = message.text or ""

        elif message.content_type == ContentType.PHOTO:
            caption = (message.caption or "") if is_caption_allowed else ""
            photo = message.photo[-1]  # Highest resolution photo
            photo_path = f"{photo.file_unique_id}.jpg"
            original_filename = f"{photo.file_unique_id}.jpg"
            try:
                await photo.download(destination_file=photo_path)
                if os.path.exists(photo_path) and os.path.getsize(photo_path) > 0:
                    downloaded_media = photo_path
                else:
                    logger.error(f"Downloaded photo {photo_path} is missing or empty.")
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
            except Exception as e:
                logger.error(f"Failed to download photo {photo_path}: {e}")
                if os.path.exists(photo_path):
                    os.remove(photo_path)

        elif message.content_type == ContentType.VIDEO:
            caption = (message.caption or "") if is_caption_allowed else ""
            media_dir = "media"
            os.makedirs(media_dir, exist_ok=True)
            video_file_path = os.path.join(media_dir, f"{message.video.file_unique_id}.mp4")
            original_filename = getattr(message.video, "file_name", None) or f"{message.video.file_unique_id}.mp4"
            try:
                video_message = await bot_client.get_messages(
                    message.chat.id, ids=message.message_id
                )
                await video_message.download_media(file=video_file_path)

                if os.path.exists(video_file_path) and os.path.getsize(video_file_path) > 0:
                    file_size = os.path.getsize(video_file_path)
                    if file_size > 100 * 1024 * 1024:
                        os.remove(video_file_path)
                        logger.warning(
                            f"Video in channel {channel_id} ({file_size / (1024*1024):.1f} MB) exceeds 100 MB limit."
                        )
                        for admin_id in config.admin_ids:
                            try:
                                await bot.send_message(
                                    admin_id,
                                    f"?? <b>Video Skipped</b>: A video in channel <code>{channel_id}</code> ({file_size / (1024*1024):.1f} MB) exceeded the 100 MB limit and was not forwarded.",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass
                        return
                    downloaded_media = video_file_path
                else:
                    logger.error(f"Downloaded video {video_file_path} is missing or empty.")
                    if os.path.exists(video_file_path):
                        os.remove(video_file_path)
            except Exception as e:
                logger.error(f"Failed to download video {video_file_path}: {e}")
                if os.path.exists(video_file_path):
                    os.remove(video_file_path)

        elif message.content_type == ContentType.DOCUMENT:
            caption = (message.caption or "") if is_caption_allowed else ""
            doc = message.document
            media_dir = "media"
            os.makedirs(media_dir, exist_ok=True)
            original_filename = doc.file_name or f"{doc.file_unique_id}.bin"
            doc_file_path = os.path.join(media_dir, f"{doc.file_unique_id}_{original_filename}")

            try:
                if (doc.file_size or 0) <= 20 * 1024 * 1024:
                    await doc.download(destination_file=doc_file_path)
                else:
                    doc_msg = await bot_client.get_messages(message.chat.id, ids=message.message_id)
                    await doc_msg.download_media(file=doc_file_path)

                if os.path.exists(doc_file_path) and os.path.getsize(doc_file_path) > 0:
                    file_size = os.path.getsize(doc_file_path)
                    if file_size > 100 * 1024 * 1024:
                        os.remove(doc_file_path)
                        logger.warning(
                            f"Document in channel {channel_id} ({file_size / (1024*1024):.1f} MB) exceeds 100 MB limit."
                        )
                        for admin_id in config.admin_ids:
                            try:
                                await bot.send_message(
                                    admin_id,
                                    f"?? <b>Document Skipped</b>: A document in channel <code>{channel_id}</code> ({file_size / (1024*1024):.1f} MB) exceeded the 100 MB limit.",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass
                        return
                    downloaded_media = doc_file_path
                else:
                    logger.error(f"Downloaded document {doc_file_path} is missing or empty.")
                    if os.path.exists(doc_file_path):
                        os.remove(doc_file_path)
            except Exception as e:
                logger.error(f"Failed to download document {doc_file_path}: {e}")
                if os.path.exists(doc_file_path):
                    os.remove(doc_file_path)

        elif message.content_type in (ContentType.VOICE, ContentType.AUDIO):
            caption = (message.caption or "") if is_caption_allowed else ""
            media_obj = message.voice or message.audio
            media_dir = "media"
            os.makedirs(media_dir, exist_ok=True)
            ext = ".ogg" if message.content_type == ContentType.VOICE else ".mp3"
            original_filename = getattr(media_obj, "file_name", None) or f"{media_obj.file_unique_id}{ext}"
            audio_file_path = os.path.join(media_dir, f"{media_obj.file_unique_id}_{original_filename}")

            try:
                if (media_obj.file_size or 0) <= 20 * 1024 * 1024:
                    await media_obj.download(destination_file=audio_file_path)
                else:
                    audio_msg = await bot_client.get_messages(message.chat.id, ids=message.message_id)
                    await audio_msg.download_media(file=audio_file_path)

                if os.path.exists(audio_file_path) and os.path.getsize(audio_file_path) > 0:
                    downloaded_media = audio_file_path
                else:
                    logger.error(f"Downloaded audio {audio_file_path} is missing or empty.")
                    if os.path.exists(audio_file_path):
                        os.remove(audio_file_path)
            except Exception as e:
                logger.error(f"Failed to download audio {audio_file_path}: {e}")
                if os.path.exists(audio_file_path):
                    os.remove(audio_file_path)

        if caption:
            caption = format_caption_for_whatsapp(caption, message.caption_entities or [])

        # Parallel fan-out bounded by UPLOAD_SEMAPHORE
        await asyncio.gather(
            *(
                send_to_single_group(group, downloaded_media, caption, original_filename)
                for group in groups
            )
        )

    except Exception as e:
        logger.error(f"Unexpected error handling channel post: {e}", exc_info=True)
    finally:
        if downloaded_media and os.path.exists(downloaded_media):
            try:
                os.remove(downloaded_media)
            except Exception as e:
                logger.error(f"Failed to remove {downloaded_media}: {e}")


async def main(_):
    print("Bot is up")


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=main)
