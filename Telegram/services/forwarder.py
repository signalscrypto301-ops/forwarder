import os
import sys
import shutil
import asyncio
import random
import time
import re
from PIL import Image, ImageFilter, ImageEnhance, ImageStat

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

import requests
from aiogram import types
from aiogram.types import ContentType, MessageEntity, ParseMode
from telethon import TelegramClient
from telethon.sessions import StringSession
try:
    from telethon import events
except ImportError:
    events = None

import config
from logger import logger
from database import (
    get_groups_for_channel,
    clean_id,
    is_channel_paused,
    record_delivery_metric,
    add_failed_message,
    update_channel_last_post,
    record_channel_post_activity,
    record_forwarded_message,
    get_forwarded_messages,
    delete_forwarded_message_records,
)
from services.whatsapp import (
    get_http_headers,
    get_http_session,
    resolve_group_name,
    notify_admins_auth_required,
    post_whatsapp_json,
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
DLQ_MEDIA_DIR = os.path.join(MEDIA_DIR, "dlq")
os.makedirs(DLQ_MEDIA_DIR, exist_ok=True)

UPLOAD_SEMAPHORE = asyncio.Semaphore(3)

ALBUM_LOCK = asyncio.Lock()
PROCESSED_MEDIA_GROUPS: dict[str, dict] = {}

bot_client: TelegramClient | None = None

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".flv",
    ".wmv",
    ".m4v",
    ".3gp",
    ".ts",
    ".m4p",
    ".mpg",
    ".mpeg",
}


from bot_context import get_bot_module as _get_bot_module


def cleanup_stale_media(max_age_seconds: int = 600):
    """Purge temporary media files older than max_age_seconds (Fix 2.6)."""
    bot_mod = _get_bot_module()
    media_dir = getattr(bot_mod, "MEDIA_DIR", MEDIA_DIR) if bot_mod else MEDIA_DIR
    now = time.time()
    if not os.path.exists(media_dir):
        return
    for fname in os.listdir(media_dir):
        fpath = os.path.join(media_dir, fname)
        try:
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > max_age_seconds):
                os.remove(fpath)
                logger.debug(f"Cleaned up stale media file: {fname}")
        except Exception as e:
            logger.error(f"Error removing {fpath}: {e}")

    # Also clean up any lingering qr_*.png files in working directory
    working_dir = os.path.dirname(os.path.dirname(__file__))
    for fname in os.listdir(working_dir):
        if fname.startswith("qr_") and fname.endswith(".png"):
            fpath = os.path.join(working_dir, fname)
            try:
                if now - os.path.getmtime(fpath) > 300:
                    os.remove(fpath)
            except Exception:
                pass


async def init_telethon_client():
    """Asynchronously initialize Telethon MTProto client with retry logic (Fix 2.3)."""
    global bot_client
    bot_mod = _get_bot_module()
    if not config.API_ID or not config.API_HASH:
        logger.warning(
            "Telethon API_ID or API_HASH not configured. Large file downloads via MTProto will be disabled."
        )
        return

    session = (
        StringSession(config.telethon_session_string)
        if config.telethon_session_string
        else "bot_client"
    )

    for attempt in range(5):
        try:
            client = TelegramClient(session, config.API_ID, config.API_HASH)
            await client.start(bot_token=config.bot_token)
            bot_client = client
            if bot_mod:
                bot_mod.bot_client = client
            logger.info("Telethon MTProto bot client successfully connected.")

            if events and hasattr(client, "on"):
                try:
                    @client.on(events.MessageDeleted())
                    async def on_telethon_message_deleted(event):
                        try:
                            chat_id = getattr(event, "chat_id", None)
                            deleted_ids = getattr(event, "deleted_ids", []) or []
                            if chat_id is not None and deleted_ids:
                                ch_str = str(chat_id)
                                bot_m = _get_bot_module()
                                del_fn = (
                                    getattr(bot_m, "delete_forwarded_post", delete_forwarded_post)
                                    if bot_m
                                    else delete_forwarded_post
                                )
                                for mid in deleted_ids:
                                    await del_fn(ch_str, mid)
                        except Exception as del_err:
                            logger.error(f"Error handling Telethon message deletion: {del_err}")
                except Exception as reg_err:
                    logger.debug(f"Failed to register Telethon MessageDeleted handler: {reg_err}")

            break
        except Exception as e:
            logger.warning(
                f"Attempt {attempt+1}/5 to start Telethon client failed ({e}). Retrying in 3s..."
            )
            await asyncio.sleep(3)


async def should_forward_caption(media_group_id: str | None, has_caption: bool) -> bool:
    """
    Prevents duplicate captions for media albums without dropping captions
    if the caption appears on a non-first item (Fix 3.3).
    """
    if not media_group_id:
        return has_caption
    if not has_caption:
        return False

    bot_mod = _get_bot_module()
    album_lock = getattr(bot_mod, "ALBUM_LOCK", ALBUM_LOCK) if bot_mod else ALBUM_LOCK
    processed_groups = getattr(bot_mod, "PROCESSED_MEDIA_GROUPS", PROCESSED_MEDIA_GROUPS) if bot_mod else PROCESSED_MEDIA_GROUPS

    async with album_lock:
        now = time.time()
        expired = [
            k for k, v in processed_groups.items() if now - v["timestamp"] > 120
        ]
        for k in expired:
            processed_groups.pop(k, None)

        album_info = processed_groups.get(media_group_id)
        if album_info and album_info.get("caption_sent"):
            return False

        processed_groups[media_group_id] = {
            "timestamp": now,
            "caption_sent": True,
        }
        return True


def adapt_image_for_whatsapp_channel(image_path: str) -> str:
    """
    Adapts images for optimal presentation in WhatsApp Channels (Newsletters).
    - Landscape (ratio > 1.20): Pads vertically to a 1:1 square canvas (w x w).
    - Extreme Portrait (ratio < 0.75): Pads horizontally to a 4:5 ratio canvas.
    - Standard ratios (0.75 to 1.20): Untouched.
    """
    if not image_path or not os.path.exists(image_path):
        return image_path

    try:
        with Image.open(image_path) as raw_img:
            if raw_img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", raw_img.size, (0, 0, 0))
                mask = raw_img.split()[-1]
                bg.paste(raw_img, mask=mask)
                img = bg
            elif raw_img.mode != "RGB":
                img = raw_img.convert("RGB")
            else:
                img = raw_img.copy()

        w, h = img.size
        if w <= 0 or h <= 0:
            return image_path

        ratio = w / h

        if 0.75 <= ratio <= 1.20:
            return image_path

        if ratio > 1.20:
            target_w = w
            target_h = w
            paste_x = 0
            paste_y = (target_h - h) // 2

            sample_h = max(1, min(8, h // 4))
            top_strip = img.crop((0, 0, w, sample_h))
            bottom_strip = img.crop((0, max(0, h - sample_h), w, h))

            stat_top = ImageStat.Stat(top_strip)
            stat_bottom = ImageStat.Stat(bottom_strip)

            top_std = max(stat_top.stddev[:3]) if len(stat_top.stddev) >= 3 else 0
            bottom_std = max(stat_bottom.stddev[:3]) if len(stat_bottom.stddev) >= 3 else 0

            avg_top = stat_top.mean[:3]
            avg_bot = stat_bottom.mean[:3]
            is_dark = max(avg_top) < 35 and max(avg_bot) < 35
            is_bright = min(avg_top) > 220 and min(avg_bot) > 220

            if is_dark:
                canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
            elif is_bright:
                canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
            elif top_std < 22 and bottom_std < 22:
                r = int((avg_top[0] + avg_bot[0]) / 2)
                g = int((avg_top[1] + avg_bot[1]) / 2)
                b = int((avg_top[2] + avg_bot[2]) / 2)
                canvas = Image.new("RGB", (target_w, target_h), (r, g, b))
            else:
                bg_card = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                bg_card = bg_card.filter(ImageFilter.GaussianBlur(radius=25))
                bg_card = ImageEnhance.Brightness(bg_card).enhance(0.75)
                canvas = bg_card

            canvas.paste(img, (paste_x, paste_y))

        else:
            target_h = h
            target_w = int(h * 0.80)
            paste_x = (target_w - w) // 2
            paste_y = 0

            sample_w = max(1, min(8, w // 4))
            left_strip = img.crop((0, 0, sample_w, h))
            right_strip = img.crop((max(0, w - sample_w), 0, w, h))

            stat_left = ImageStat.Stat(left_strip)
            stat_right = ImageStat.Stat(right_strip)

            left_std = max(stat_left.stddev[:3]) if len(stat_left.stddev) >= 3 else 0
            right_std = max(stat_right.stddev[:3]) if len(stat_right.stddev) >= 3 else 0

            avg_left = stat_left.mean[:3]
            avg_right = stat_right.mean[:3]
            is_dark = max(avg_left) < 35 and max(avg_right) < 35
            is_bright = min(avg_left) > 220 and min(avg_right) > 220

            if is_dark:
                canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
            elif is_bright:
                canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
            elif left_std < 22 and right_std < 22:
                r = int((avg_left[0] + avg_right[0]) / 2)
                g = int((avg_left[1] + avg_right[1]) / 2)
                b = int((avg_left[2] + avg_right[2]) / 2)
                canvas = Image.new("RGB", (target_w, target_h), (r, g, b))
            else:
                bg_card = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                bg_card = bg_card.filter(ImageFilter.GaussianBlur(radius=25))
                bg_card = ImageEnhance.Brightness(bg_card).enhance(0.75)
                canvas = bg_card

            canvas.paste(img, (paste_x, paste_y))

        canvas.save(image_path, "JPEG", quality=95, subsampling=0)
        logger.info(
            f"🖼️ [Image Adaptation] Successfully adapted image {image_path} from ({w}x{h}, ratio {ratio:.2f}) to ({target_w}x{target_h})."
        )
        return image_path
    except Exception as e:
        logger.error(f"Error adapting image {image_path} for WhatsApp: {e}")
        return image_path


# Backward compatibility alias
prepare_image_for_whatsapp_channel = adapt_image_for_whatsapp_channel


def format_caption_for_whatsapp(caption: str, entities: list[MessageEntity]) -> str:
    """
    Translates Telegram MessageEntity offset formatting to WhatsApp markdown.
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

    mergeable_types = {"bold", "italic", "strikethrough", "code", "spoiler"}
    by_type: dict[str, list[list[int]]] = {}
    other_entities: list[tuple[str, int, int, str | None]] = []

    for ent in entities:
        start = utf16_to_str_idx.get(ent.offset)
        end = utf16_to_str_idx.get(ent.offset + ent.length)

        if start is None or end is None or start >= end:
            continue

        if ent.type in mergeable_types:
            while start < end and caption[start].isspace():
                start += 1
            while end > start and caption[end - 1].isspace():
                end -= 1
            if start < end:
                by_type.setdefault(ent.type, []).append([start, end])
        else:
            other_entities.append((ent.type, start, end, getattr(ent, "url", None)))

    clean_entities: list[tuple[str, int, int, str | None]] = []
    for ent_type, intervals in by_type.items():
        intervals.sort(key=lambda x: (x[0], x[1]))
        merged: list[list[int]] = []
        for s, e in intervals:
            if not merged:
                merged.append([s, e])
            else:
                prev_s, prev_e = merged[-1]
                if s <= prev_e:
                    merged[-1][1] = max(prev_e, e)
                else:
                    merged.append([s, e])
        for s, e in merged:
            clean_entities.append((ent_type, s, e, None))

    for item in other_entities:
        clean_entities.append(item)

    prefix_insertions: dict[int, list[str]] = {}
    suffix_insertions: dict[int, list[str]] = {}
    link_insertions: dict[int, list[str]] = {}

    for ent_type, start, end, url in clean_entities:
        prefix = ""
        suffix = ""

        if ent_type == "bold":
            prefix, suffix = "*", "*"
        elif ent_type == "italic":
            prefix, suffix = "_", "_"
        elif ent_type == "strikethrough":
            prefix, suffix = "~", "~"
        elif ent_type == "code":
            prefix, suffix = "`", "`"
        elif ent_type == "pre":
            prefix, suffix = "```\n", "\n```"
        elif ent_type == "spoiler":
            prefix, suffix = "||", "||"
        elif ent_type in ("blockquote", "expandable_blockquote"):
            prefix = "> "
            for k in range(start, min(end, len(caption))):
                if caption[k] == "\n" and k + 1 < end:
                    prefix_insertions.setdefault(k + 1, []).append("> ")
        elif ent_type == "text_link" and url:
            link_insertions.setdefault(end, []).append(f" ({url})")
            continue
        else:
            continue

        if prefix:
            prefix_insertions.setdefault(start, []).append(prefix)
        if suffix:
            suffix_insertions.setdefault(end, []).insert(0, suffix)

    result = []
    for i, ch in enumerate(caption):
        if i in prefix_insertions:
            result.extend(prefix_insertions[i])
        result.append(ch)
        if (i + 1) in suffix_insertions:
            result.extend(suffix_insertions[i + 1])
        if (i + 1) in link_insertions:
            result.extend(link_insertions[i + 1])

    formatted = "".join(result)
    formatted = re.sub(r"\*{2,}", "*", formatted)
    formatted = re.sub(r"_{2,}", "_", formatted)
    formatted = re.sub(r"~{2,}", "~", formatted)

    return formatted


async def check_and_alert_health(account_id: str | None = None):
    """
    Checks rate controller health and alerts admins if 80% warning
    or 100% critical safety threshold is exceeded.
    """
    bot_mod = _get_bot_module()
    rc = getattr(bot_mod, "rate_controller", None)
    if not rc:
        return
    status, alert_msg = rc.check_health(account_id=account_id)
    if alert_msg:
        bot_instance = getattr(bot_mod, "bot", None)
        if bot_instance:
            for admin_id in config.admin_ids:
                try:
                    await bot_instance.send_message(admin_id, alert_msg, parse_mode=ParseMode.HTML)
                except Exception as alert_err:
                    logger.error(
                        f"Failed to send rate limit alert to admin {admin_id}: {alert_err}"
                    )


async def send_to_single_group(
    group: str,
    downloaded_media: str | None,
    caption: str,
    original_filename: str | None = None,
    content_type: str = "text",
    channel_id: str | None = None,
    is_retry: bool = False,
    tg_message_id: int | None = None,
) -> bool:
    """
    Sends message to a single WhatsApp group/newsletter.
    """
    if getattr(config, "BAN_VIDEO_FORWARDING", True):
        if content_type == "video" or (
            downloaded_media
            and any(downloaded_media.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)
        ):
            logger.info(
                f"🚫 [Video Ban] Refusing to forward video message ({content_type}) to group {group}."
            )
            return False

    bot_mod = _get_bot_module()
    rc = getattr(bot_mod, "rate_controller", None)
    pool = getattr(bot_mod, "account_pool", None)
    sem = getattr(bot_mod, "UPLOAD_SEMAPHORE", UPLOAD_SEMAPHORE) if bot_mod else UPLOAD_SEMAPHORE
    dlq_dir = getattr(bot_mod, "DLQ_MEDIA_DIR", DLQ_MEDIA_DIR) if bot_mod else DLQ_MEDIA_DIR
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session
    res_group_name_fn = getattr(bot_mod, "resolve_group_name", resolve_group_name) if bot_mod else resolve_group_name
    notify_auth_fn = getattr(bot_mod, "notify_admins_auth_required", notify_admins_auth_required) if bot_mod else notify_admins_auth_required

    if rc:
        rc.enter_queue()

    try:
        async with sem:
            sender_id = "user"
            if pool:
                sender_id = await pool.get_next_sender()

            if rc:
                bucket = rc.get_bucket(group, account_id=sender_id)
                waited = await bucket.acquire()
                if waited > 0:
                    logger.debug(f"Token bucket waited {waited:.2f}s for {group} (sender: {sender_id})")

                pacing_delay = rc.calculate_pacing_delay(group, account_id=sender_id)
                await asyncio.sleep(pacing_delay)

            session = await get_session_fn()
            max_retries = 2
            success = False
            last_latency_ms = 0.0
            last_err_msg = ""
            last_err_detail = ""
            last_exception: Exception | None = None
            status_code = 500

            for attempt in range(max_retries + 1):
                t_start = time.time()
                if attempt > 0 and pool:
                    sender_id = await pool.get_next_sender()
                if downloaded_media:
                    if not os.path.exists(downloaded_media):
                        logger.error(
                            f"Downloaded media {downloaded_media} does not exist for group {group}."
                        )
                        record_delivery_metric(
                            content_type=content_type,
                            latency_ms=0.0,
                            retried=False,
                            failed=True,
                        )
                        if not is_retry:
                            add_failed_message(
                                channel_id=channel_id or "",
                                group_id=group,
                                group_name=res_group_name_fn(group),
                                content_type=content_type,
                                caption=caption,
                                original_filename=original_filename,
                                media_path=None,
                                reason="Media file missing",
                                file_size_bytes=0,
                            )
                        return False

                    try:
                        fname = original_filename or os.path.basename(downloaded_media)
                        status_code = 500
                        msg = ""
                        err_detail = ""

                        if session:
                            with open(downloaded_media, "rb") as fp:
                                data = aiohttp.FormData()
                                data.add_field("clientId", sender_id)
                                data.add_field("groupId", group)
                                data.add_field("caption", caption or "")
                                data.add_field(
                                    "media",
                                    fp,
                                    filename=fname,
                                    content_type="application/octet-stream",
                                )

                                async with session.post(
                                    f"{config.whatsapp_service}/sendMedia", data=data
                                ) as res:
                                    last_latency_ms = (time.time() - t_start) * 1000.0
                                    status_code = res.status
                                    try:
                                        res_json = await res.json()
                                        msg = res_json.get("message", "")
                                        err_detail = res_json.get("error", "")
                                    except Exception:
                                        msg = await res.text()
                        else:
                            def send_media_sync():
                                with open(downloaded_media, "rb") as fp:
                                    files = {"media": (fname, fp)}
                                    data = {"clientId": sender_id, "groupId": group, "caption": caption or ""}
                                    return requests.post(
                                        f"{config.whatsapp_service}/sendMedia",
                                        data=data,
                                        files=files,
                                        headers=get_http_headers(),
                                        timeout=60,
                                    )

                            res_sync = await asyncio.to_thread(send_media_sync)
                            last_latency_ms = (time.time() - t_start) * 1000.0
                            status_code = res_sync.status_code
                            try:
                                res_json = res_sync.json()
                                msg = res_json.get("message", "")
                                err_detail = res_json.get("error", "")
                            except Exception:
                                msg = res_sync.text

                        if status_code == 200:
                            success = True
                            if pool:
                                pool.record_dispatched(sender_id)
                            record_delivery_metric(
                                content_type=content_type,
                                latency_ms=last_latency_ms,
                                retried=(attempt > 0 or is_retry),
                                failed=False,
                            )
                            logger.info(
                                f"Media message sent to group {group} via {sender_id} successfully."
                            )
                            if channel_id and tg_message_id:
                                wa_mid = res_json.get("messageId") if isinstance(res_json, dict) else None
                                wa_key = res_json.get("key") if isinstance(res_json, dict) else None
                                if wa_mid or wa_key:
                                    try:
                                        await asyncio.to_thread(
                                            record_forwarded_message,
                                            channel_id,
                                            tg_message_id,
                                            group,
                                            wa_mid,
                                            wa_key,
                                            sender_id,
                                        )
                                    except Exception as db_err:
                                        logger.debug(f"Failed to record forwarded message mapping: {db_err}")
                            if rc:
                                rc.record_sent(account_id=sender_id)
                            await check_and_alert_health(account_id=sender_id)
                            break
                        is_session_reconnecting = (status_code == 503) or (
                            status_code in (400, 401)
                            and any(k in str(msg).lower() for k in ("session", "reconnecting", "not ready", "unauthorized"))
                        )
                        if (status_code in (502, 503, 504) or is_session_reconnecting) and attempt < max_retries:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            if is_session_reconnecting and pool:
                                pool.mark_degraded(sender_id, cooldown_sec=60)
                            logger.warning(
                                f"WhatsApp service returned {status_code} for {group} (sender: {sender_id}, msg: {msg}). Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        else:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.error(
                                f"Failed to send media message to group {group} via {sender_id}: {msg} ({err_detail})"
                            )
                            if "Session is not authorized" in str(msg) or "session" in str(msg).lower() or status_code in (401, 503):
                                if pool:
                                    pool.mark_degraded(sender_id, cooldown_sec=120)
                                await notify_auth_fn()
                            break
                    except Exception as e:
                        last_exception = e
                        last_latency_ms = (time.time() - t_start) * 1000.0
                        if attempt < max_retries:
                            logger.warning(
                                f"Error connecting to WhatsApp service for group {group} (sender: {sender_id}): {e}. Retrying in {(attempt+1)*2}s..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        logger.error(
                            f"Failed to send media message to group {group}: {e}"
                        )
                        break

                else:
                    data = {"clientId": sender_id, "groupId": group, "text": caption}
                    status_code = 500
                    msg = ""
                    err_detail = ""

                    try:
                        if session:
                            async with session.post(
                                f"{config.whatsapp_service}/sendText", json=data
                            ) as res:
                                last_latency_ms = (time.time() - t_start) * 1000.0
                                status_code = res.status
                                try:
                                    res_json = await res.json()
                                    msg = res_json.get("message", "")
                                    err_detail = res_json.get("error", "")
                                except Exception:
                                    msg = await res.text()
                        else:
                            def send_text_sync():
                                return requests.post(
                                    f"{config.whatsapp_service}/sendText",
                                    json=data,
                                    headers=get_http_headers(),
                                    timeout=30,
                                )

                            res_sync = await asyncio.to_thread(send_text_sync)
                            last_latency_ms = (time.time() - t_start) * 1000.0
                            status_code = res_sync.status_code
                            try:
                                res_json = res_sync.json()
                                msg = res_json.get("message", "")
                                err_detail = res_json.get("error", "")
                            except Exception:
                                msg = res_sync.text

                        if status_code == 200:
                            success = True
                            if pool:
                                pool.record_dispatched(sender_id)
                            record_delivery_metric(
                                content_type=content_type,
                                latency_ms=last_latency_ms,
                                retried=(attempt > 0 or is_retry),
                                failed=False,
                            )
                            logger.info(
                                f"Text message sent to group {group} via {sender_id} successfully."
                            )
                            if channel_id and tg_message_id:
                                wa_mid = res_json.get("messageId") if isinstance(res_json, dict) else None
                                wa_key = res_json.get("key") if isinstance(res_json, dict) else None
                                if wa_mid or wa_key:
                                    try:
                                        await asyncio.to_thread(
                                            record_forwarded_message,
                                            channel_id,
                                            tg_message_id,
                                            group,
                                            wa_mid,
                                            wa_key,
                                            sender_id,
                                        )
                                    except Exception as db_err:
                                        logger.debug(f"Failed to record forwarded message mapping: {db_err}")
                            if rc:
                                rc.record_sent(account_id=sender_id)
                            await check_and_alert_health(account_id=sender_id)
                            break
                        elif (
                            status_code in (502, 503, 504)
                            or (
                                (status_code in (400, 401) or status_code == 503)
                                and any(k in str(msg).lower() for k in ("session", "reconnecting", "not ready", "unauthorized"))
                            )
                        ) and attempt < max_retries:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            if ("session" in str(msg).lower() or status_code in (401, 503)) and pool:
                                pool.mark_degraded(sender_id, cooldown_sec=60)
                            logger.warning(
                                f"WhatsApp service returned {status_code} for {group} (sender: {sender_id}, msg: {msg}). Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        else:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.error(
                                f"Failed to send text message to group {group} via {sender_id}: {msg} ({err_detail})"
                            )
                            if "Session is not authorized" in str(msg) or "session" in str(msg).lower() or status_code in (401, 503):
                                if pool:
                                    pool.mark_degraded(sender_id, cooldown_sec=120)
                                await notify_auth_fn()
                            break
                    except Exception as e:
                        last_exception = e
                        last_latency_ms = (time.time() - t_start) * 1000.0
                        if attempt < max_retries:
                            logger.warning(
                                f"Error connecting to WhatsApp service for group {group}: {e}. Retrying in {(attempt+1)*2}s..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        logger.error(
                            f"Failed to send text message to group {group}: {e}"
                        )
                        break

            if not success:
                record_delivery_metric(
                    content_type=content_type,
                    latency_ms=last_latency_ms,
                    retried=(attempt > 0 or is_retry),
                    failed=True,
                )
                if not is_retry:
                    combined_err = f"{last_err_msg} {last_err_detail} {last_exception or ''}".lower()
                    if "rate limit" in combined_err or status_code == 429:
                        error_reason = "WhatsApp rate limit"
                    elif "timeout" in combined_err or status_code in (504, 408):
                        error_reason = "Connection timeout"
                    elif "session is not authorized" in combined_err or status_code == 401:
                        error_reason = "Session unauthorized"
                    elif "permission" in combined_err or status_code == 403:
                        error_reason = "Invalid permissions"
                    elif status_code in (500, 502, 503):
                        error_reason = "WhatsApp service error"
                    elif last_exception:
                        error_reason = "Connection timeout" if "timeout" in str(last_exception).lower() else "Connection failed"
                    elif last_err_msg:
                        clean_msg = str(last_err_msg).strip()
                        error_reason = (clean_msg[:25] + "...") if len(clean_msg) > 28 else clean_msg
                    else:
                        error_reason = "Delivery failed"

                    dlq_media_path = None
                    file_size = 0
                    if downloaded_media and os.path.exists(downloaded_media):
                        file_size = os.path.getsize(downloaded_media)
                        ext = os.path.splitext(downloaded_media)[1]
                        dlq_fname = f"dlq_{int(time.time())}_{random.randint(1000, 9999)}{ext}"
                        dlq_media_path = os.path.join(dlq_dir, dlq_fname)
                        try:
                            shutil.copy2(downloaded_media, dlq_media_path)
                        except Exception as copy_err:
                            logger.error(f"Failed to preserve DLQ media: {copy_err}")
                            dlq_media_path = None

                    group_name = res_group_name_fn(group)
                    add_failed_message(
                        channel_id=channel_id or "",
                        group_id=group,
                        group_name=group_name,
                        content_type=content_type,
                        caption=caption,
                        original_filename=original_filename,
                        media_path=dlq_media_path,
                        reason=error_reason,
                        file_size_bytes=file_size,
                    )
                    logger.warning(
                        f"Delivery to {group} ({group_name}) routed to DLQ. Reason: {error_reason}"
                    )
            return success
    finally:
        if rc:
            rc.exit_queue()


def is_video_message(message: types.Message) -> bool:
    """
    Checks if a Telegram message is a video or has video attached.
    """
    if getattr(message, "video", None) is not None:
        return True
    if getattr(message, "content_type", None) == ContentType.VIDEO:
        return True
    if getattr(message, "video_note", None) is not None:
        return True
    if getattr(message, "content_type", None) == getattr(ContentType, "VIDEO_NOTE", "video_note"):
        return True
    if getattr(message, "animation", None) is not None:
        return True
    if getattr(message, "content_type", None) == getattr(ContentType, "ANIMATION", "animation"):
        return True

    doc = getattr(message, "document", None)
    if doc:
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("video/"):
            return True
        fname = (getattr(doc, "file_name", "") or "").lower()
        ext = os.path.splitext(fname)[1]
        if ext in VIDEO_EXTENSIONS:
            return True

    return False


async def handle_channel_post(message: types.Message):
    channel_id = clean_id(message.chat.id)
    bot_mod = _get_bot_module()
    media_dir = getattr(bot_mod, "MEDIA_DIR", MEDIA_DIR) if bot_mod else MEDIA_DIR
    bot_instance = getattr(bot_mod, "bot", None)
    b_client = getattr(bot_mod, "bot_client", bot_client) if bot_mod else bot_client
    send_fn = getattr(bot_mod, "send_to_single_group", send_to_single_group) if bot_mod else send_to_single_group
    res_group_name_fn = getattr(bot_mod, "resolve_group_name", resolve_group_name) if bot_mod else resolve_group_name

    # Ban video forwarding: if any message has video attached or is pure video, do not forward
    if getattr(config, "BAN_VIDEO_FORWARDING", True) and is_video_message(message):
        logger.info(
            f"🚫 [Video Ban] Message {getattr(message, 'message_id', 'unknown')} in channel {channel_id} contains video (type={getattr(message, 'content_type', 'unknown')}). Forwarding is banned. Dropping post."
        )
        return

    # Record channel activity timestamp
    await asyncio.to_thread(update_channel_last_post, channel_id)

    # Record channel hourly post volume
    await asyncio.to_thread(record_channel_post_activity, channel_id)

    # Check if forwarding is paused for this channel
    if is_channel_paused(channel_id):
        logger.info(f"Channel {channel_id} forwarding is paused. Skipping post.")
        return

    groups = await asyncio.to_thread(get_groups_for_channel, channel_id)
    if not groups:
        return

    caption = ""
    downloaded_media = None
    original_filename = None

    content_type_map = {
        ContentType.TEXT: "text",
        ContentType.PHOTO: "photo",
        ContentType.VIDEO: "video",
        ContentType.DOCUMENT: "doc",
        ContentType.VOICE: "audio",
        ContentType.AUDIO: "audio",
        "text": "text",
        "photo": "photo",
        "video": "video",
        "document": "doc",
        "doc": "doc",
        "voice": "audio",
        "audio": "audio",
    }
    content_type_str = content_type_map.get(message.content_type, "text")

    try:
        # 1. Process Text Messages
        if message.content_type in (getattr(ContentType, "TEXT", None), "text"):
            raw_text = message.text or ""
            entities = message.entities or []
            caption = format_caption_for_whatsapp(raw_text, entities) if raw_text else ""

        # 2. Process Photos
        elif message.content_type in (getattr(ContentType, "PHOTO", None), "photo"):
            raw_caption = message.caption or ""
            entities = message.caption_entities or []
            allow_caption = await should_forward_caption(
                str(message.media_group_id) if message.media_group_id else None,
                bool(raw_caption),
            )
            caption = (
                format_caption_for_whatsapp(raw_caption, entities)
                if (raw_caption and allow_caption)
                else ""
            )

            photo = message.photo[-1]
            unique_tag = f"{int(time.time() * 1000)}_{random.randint(100, 999)}"
            photo_path = os.path.join(media_dir, f"{photo.file_unique_id}_{unique_tag}.jpg")
            original_filename = f"{photo.file_unique_id}.jpg"

            try:
                await photo.download(destination_file=photo_path)
                if os.path.exists(photo_path) and os.path.getsize(photo_path) > 0:
                    # Adapt image if forwarding to any WhatsApp channels/newsletters
                    has_newsletter = any("@newsletter" in g for g in groups)
                    if has_newsletter:
                        adapt_fn = getattr(bot_mod, "adapt_image_for_whatsapp_channel", adapt_image_for_whatsapp_channel) if bot_mod else adapt_image_for_whatsapp_channel
                        photo_path = await asyncio.to_thread(adapt_fn, photo_path)
                    downloaded_media = photo_path
                else:
                    logger.error(f"Downloaded photo {photo_path} is missing or empty.")
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
            except Exception as e:
                logger.error(f"Failed to download photo {photo_path}: {e}")
                if os.path.exists(photo_path):
                    os.remove(photo_path)

        # 3. Process Videos
        elif message.content_type in (getattr(ContentType, "VIDEO", None), "video"):
            if getattr(config, "BAN_VIDEO_FORWARDING", True):
                logger.info(
                    f"🚫 [Video Ban] Video message in channel {channel_id} dropped (video forwarding is banned)."
                )
                return

            raw_caption = message.caption or ""
            entities = message.caption_entities or []
            allow_caption = await should_forward_caption(
                str(message.media_group_id) if message.media_group_id else None,
                bool(raw_caption),
            )
            caption = (
                format_caption_for_whatsapp(raw_caption, entities)
                if (raw_caption and allow_caption)
                else ""
            )

            video_size = getattr(message.video, "file_size", 0) or 0
            if video_size > 100 * 1024 * 1024:
                logger.warning(
                    f"Video in channel {channel_id} ({video_size / (1024*1024):.1f} MB) exceeds 100 MB limit. Skipping download."
                )
                fname = getattr(message.video, "file_name", None) or f"{message.video.file_unique_id}.mp4"
                for group in groups:
                    add_failed_message(
                        channel_id=channel_id,
                        group_id=group,
                        group_name=res_group_name_fn(group),
                        content_type="video",
                        caption=caption,
                        original_filename=fname,
                        media_path=None,
                        reason="Size limit",
                        file_size_bytes=video_size,
                    )
                if bot_instance:
                    for admin_id in config.admin_ids:
                        try:
                            await bot_instance.send_message(
                                admin_id,
                                f"⚠️ <b>Video Skipped</b>: A video in channel <code>{channel_id}</code> ({video_size / (1024*1024):.1f} MB) exceeded the 100 MB limit.",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            pass
                return

            video_file_path = os.path.join(
                media_dir, f"{message.video.file_unique_id}.mp4"
            )
            original_filename = (
                getattr(message.video, "file_name", None)
                or f"{message.video.file_unique_id}.mp4"
            )

            try:
                if video_size <= 20 * 1024 * 1024:
                    await message.video.download(destination_file=video_file_path)
                elif b_client:
                    video_message = await b_client.get_messages(
                        message.chat.id, ids=message.message_id
                    )
                    await video_message.download_media(file=video_file_path)
                else:
                    logger.warning(
                        f"Video {video_size / (1024*1024):.1f} MB requires Telethon but bot_client is not connected."
                    )
                    return

                if os.path.exists(video_file_path) and os.path.getsize(video_file_path) > 0:
                    downloaded_media = video_file_path
                else:
                    logger.error(f"Downloaded video {video_file_path} is missing or empty.")
                    if os.path.exists(video_file_path):
                        os.remove(video_file_path)
            except Exception as e:
                logger.error(f"Failed to download video {video_file_path}: {e}")
                if os.path.exists(video_file_path):
                    os.remove(video_file_path)

        # 4. Process Documents
        elif message.content_type in (getattr(ContentType, "DOCUMENT", None), "document", "doc"):
            doc = message.document
            if getattr(config, "BAN_VIDEO_FORWARDING", True) and doc:
                mime = (getattr(doc, "mime_type", "") or "").lower()
                fname = (getattr(doc, "file_name", "") or "").lower()
                ext = os.path.splitext(fname)[1]
                if mime.startswith("video/") or ext in VIDEO_EXTENSIONS:
                    logger.info(
                        f"🚫 [Video Ban] Document '{fname}' ({mime}) in channel {channel_id} is a video. Video forwarding is banned. Skipping."
                    )
                    return

            raw_caption = message.caption or ""
            entities = message.caption_entities or []
            allow_caption = await should_forward_caption(
                str(message.media_group_id) if message.media_group_id else None,
                bool(raw_caption),
            )
            caption = (
                format_caption_for_whatsapp(raw_caption, entities)
                if (raw_caption and allow_caption)
                else ""
            )

            doc_size = getattr(doc, "file_size", 0) or 0
            if doc_size > 100 * 1024 * 1024:
                logger.warning(
                    f"Document in channel {channel_id} ({doc_size / (1024*1024):.1f} MB) exceeds 100 MB limit. Skipping download."
                )
                fname = doc.file_name or f"{doc.file_unique_id}.bin"
                for group in groups:
                    add_failed_message(
                        channel_id=channel_id,
                        group_id=group,
                        group_name=res_group_name_fn(group),
                        content_type="doc",
                        caption=caption,
                        original_filename=fname,
                        media_path=None,
                        reason="Size limit",
                        file_size_bytes=doc_size,
                    )
                if bot_instance:
                    for admin_id in config.admin_ids:
                        try:
                            await bot_instance.send_message(
                                admin_id,
                                f"⚠️ <b>Document Skipped</b>: A document in channel <code>{channel_id}</code> ({doc_size / (1024*1024):.1f} MB) exceeded the 100 MB limit.",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            pass
                return

            original_filename = doc.file_name or f"{doc.file_unique_id}.bin"
            safe_name = original_filename.replace("/", "_").replace("\\", "_")
            doc_file_path = os.path.join(
                media_dir, f"{doc.file_unique_id}_{safe_name}"
            )

            try:
                if doc_size <= 20 * 1024 * 1024:
                    await doc.download(destination_file=doc_file_path)
                elif b_client:
                    doc_msg = await b_client.get_messages(
                        message.chat.id, ids=message.message_id
                    )
                    await doc_msg.download_media(file=doc_file_path)
                else:
                    logger.warning(
                        f"Document {doc_size / (1024*1024):.1f} MB requires Telethon but bot_client is not connected."
                    )
                    return

                if os.path.exists(doc_file_path) and os.path.getsize(doc_file_path) > 0:
                    downloaded_media = doc_file_path
                else:
                    logger.error(f"Downloaded document {doc_file_path} is missing or empty.")
                    if os.path.exists(doc_file_path):
                        os.remove(doc_file_path)
            except Exception as e:
                logger.error(f"Failed to download document {doc_file_path}: {e}")
                if os.path.exists(doc_file_path):
                    os.remove(doc_file_path)

        # 5. Process Voice / Audio
        elif message.content_type in (
            getattr(ContentType, "VOICE", None),
            getattr(ContentType, "AUDIO", None),
            "voice",
            "audio",
        ):
            raw_caption = message.caption or ""
            entities = message.caption_entities or []
            allow_caption = await should_forward_caption(
                str(message.media_group_id) if message.media_group_id else None,
                bool(raw_caption),
            )
            caption = (
                format_caption_for_whatsapp(raw_caption, entities)
                if (raw_caption and allow_caption)
                else ""
            )

            media_obj = message.voice or message.audio
            audio_size = getattr(media_obj, "file_size", 0) or 0
            if audio_size > 100 * 1024 * 1024:
                logger.warning(f"Audio in channel {channel_id} exceeds 100 MB limit.")
                fname = getattr(media_obj, "file_name", None) or f"{media_obj.file_unique_id}.mp3"
                for group in groups:
                    add_failed_message(
                        channel_id=channel_id,
                        group_id=group,
                        group_name=res_group_name_fn(group),
                        content_type="audio",
                        caption=caption,
                        original_filename=fname,
                        media_path=None,
                        reason="Size limit",
                        file_size_bytes=audio_size,
                    )
                return

            ext = ".ogg" if message.content_type == ContentType.VOICE else ".mp3"
            original_filename = (
                getattr(media_obj, "file_name", None)
                or f"{media_obj.file_unique_id}{ext}"
            )
            safe_name = original_filename.replace("/", "_").replace("\\", "_")
            audio_file_path = os.path.join(
                media_dir, f"{media_obj.file_unique_id}_{safe_name}"
            )

            try:
                if audio_size <= 20 * 1024 * 1024:
                    await media_obj.download(destination_file=audio_file_path)
                elif b_client:
                    audio_msg = await b_client.get_messages(
                        message.chat.id, ids=message.message_id
                    )
                    await audio_msg.download_media(file=audio_file_path)
                else:
                    logger.warning(
                        f"Audio {audio_size / (1024*1024):.1f} MB requires Telethon but bot_client is not connected."
                    )
                    return

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

        await asyncio.gather(
            *(
                send_fn(
                    group,
                    downloaded_media,
                    caption,
                    original_filename,
                    content_type=content_type_str,
                    channel_id=channel_id,
                    tg_message_id=getattr(message, "message_id", None),
                )
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


async def handle_edited_channel_post(message: types.Message):
    """
    Handles edited channel posts and propagates text/caption edits to forwarded WhatsApp messages.
    """
    try:
        channel_id = str(message.chat.id)
        clean_cid = clean_id(channel_id)
        tg_message_id = getattr(message, "message_id", None)
        if not tg_message_id:
            return

        bot_mod = _get_bot_module()
        fmt_caption_fn = (
            getattr(bot_mod, "format_caption_for_whatsapp", format_caption_for_whatsapp)
            if bot_mod
            else format_caption_for_whatsapp
        )
        post_fn = (
            getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json)
            if bot_mod
            else post_whatsapp_json
        )

        raw_text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
        new_text = fmt_caption_fn(raw_text, entities) if raw_text else ""

        if not new_text:
            logger.debug(
                f"Edited post {tg_message_id} in channel {channel_id} has empty text/caption. Skipping edit sync."
            )
            return

        get_fwd_fn = (
            getattr(bot_mod, "get_forwarded_messages", get_forwarded_messages)
            if bot_mod
            else get_forwarded_messages
        )
        records = await asyncio.to_thread(get_fwd_fn, clean_cid, tg_message_id)
        if not records and str(channel_id) != clean_cid:
            records = await asyncio.to_thread(get_fwd_fn, str(channel_id), tg_message_id)

        if not records:
            logger.debug(
                f"No forwarded WhatsApp messages found to edit for channel {channel_id} post {tg_message_id}."
            )
            return

        async def edit_single_target(rec: dict):
            group_id = rec.get("group_id")
            wa_key = rec.get("wa_key")
            sender_id = rec.get("sender_id") or "user"
            if not group_id or not wa_key:
                return False

            payload = {
                "clientId": sender_id,
                "groupId": group_id,
                "key": wa_key,
                "text": new_text,
            }
            try:
                status, res = await post_fn("editMessage", payload)
                if status == 200:
                    logger.info(
                        f"Successfully synced edit for post {channel_id}:{tg_message_id} to group {group_id}."
                    )
                    return True
                else:
                    logger.warning(
                        f"Failed to sync edit for post {channel_id}:{tg_message_id} to group {group_id}: {res}"
                    )
                    return False
            except Exception as e:
                logger.error(f"Error calling editMessage for {group_id}: {e}")
                return False

        await asyncio.gather(*(edit_single_target(r) for r in records))

    except Exception as e:
        logger.error(f"Unexpected error handling edited channel post: {e}", exc_info=True)


async def delete_forwarded_post(channel_id: str, tg_message_id: int) -> int:
    """
    Revokes/deletes forwarded WhatsApp messages corresponding to a Telegram post.
    Returns the count of successfully revoked messages.
    """
    try:
        clean_cid = clean_id(channel_id)
        bot_mod = _get_bot_module()
        get_fwd_fn = (
            getattr(bot_mod, "get_forwarded_messages", get_forwarded_messages)
            if bot_mod
            else get_forwarded_messages
        )
        del_recs_fn = (
            getattr(bot_mod, "delete_forwarded_message_records", delete_forwarded_message_records)
            if bot_mod
            else delete_forwarded_message_records
        )
        post_fn = (
            getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json)
            if bot_mod
            else post_whatsapp_json
        )

        records = await asyncio.to_thread(get_fwd_fn, clean_cid, tg_message_id)
        if not records and str(channel_id) != clean_cid:
            records = await asyncio.to_thread(get_fwd_fn, str(channel_id), tg_message_id)

        if not records:
            logger.debug(
                f"No forwarded WhatsApp messages found to delete for channel {channel_id} post {tg_message_id}."
            )
            return 0

        async def delete_single_target(rec: dict):
            group_id = rec.get("group_id")
            wa_key = rec.get("wa_key")
            sender_id = rec.get("sender_id") or "user"
            if not group_id or not wa_key:
                return False

            payload = {
                "clientId": sender_id,
                "groupId": group_id,
                "key": wa_key,
            }
            try:
                status, res = await post_fn("deleteMessage", payload)
                if status == 200:
                    logger.info(
                        f"Successfully revoked WhatsApp message in {group_id} for TG post {channel_id}:{tg_message_id}."
                    )
                    return True
                else:
                    logger.warning(
                        f"Failed to revoke WhatsApp message in {group_id}: {res}"
                    )
                    return False
            except Exception as e:
                logger.error(f"Error calling deleteMessage for {group_id}: {e}")
                return False

        results = await asyncio.gather(*(delete_single_target(r) for r in records))
        deleted_count = sum(1 for r in results if r)

        try:
            await asyncio.to_thread(del_recs_fn, clean_cid, tg_message_id)
            if str(channel_id) != clean_cid:
                await asyncio.to_thread(del_recs_fn, str(channel_id), tg_message_id)
        except Exception as db_err:
            logger.debug(f"Failed to delete forwarded message records: {db_err}")

        return deleted_count

    except Exception as e:
        logger.error(f"Unexpected error in delete_forwarded_post: {e}", exc_info=True)
        return 0


def register_forwarder_handlers(dp):
    dp.register_channel_post_handler(
        handle_channel_post,
        content_types=[
            ContentType.TEXT,
            ContentType.PHOTO,
            ContentType.VIDEO,
            getattr(ContentType, "VIDEO_NOTE", "video_note"),
            getattr(ContentType, "ANIMATION", "animation"),
            ContentType.DOCUMENT,
            ContentType.VOICE,
            ContentType.AUDIO,
        ],
    )
    if hasattr(dp, "register_edited_channel_post_handler"):
        dp.register_edited_channel_post_handler(
            handle_edited_channel_post,
            content_types=[
                ContentType.TEXT,
                ContentType.PHOTO,
                ContentType.VIDEO,
                getattr(ContentType, "VIDEO_NOTE", "video_note"),
                getattr(ContentType, "ANIMATION", "animation"),
                ContentType.DOCUMENT,
                ContentType.VOICE,
                ContentType.AUDIO,
            ],
        )
