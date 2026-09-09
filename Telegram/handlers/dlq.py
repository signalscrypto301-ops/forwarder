import os
import sys
import asyncio
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ParseMode,
)

import config
from logger import logger
from database import (
    get_failed_messages,
    increment_failed_message_retry,
    delete_failed_message,
    clear_failed_messages,
    get_failed_messages_count,
)
from services.whatsapp import resolve_group_name
from services.forwarder import send_to_single_group, DLQ_MEDIA_DIR


from bot_context import get_bot_module as _get_bot_module


def _get_ikm():
    bot_mod = _get_bot_module()
    return getattr(bot_mod, "InlineKeyboardMarkup", InlineKeyboardMarkup) if bot_mod else InlineKeyboardMarkup


def _get_ikb():
    bot_mod = _get_bot_module()
    return getattr(bot_mod, "InlineKeyboardButton", InlineKeyboardButton) if bot_mod else InlineKeyboardButton


def _is_admin(user_id: int) -> bool:
    bot_mod = _get_bot_module()
    if bot_mod and hasattr(bot_mod, "is_admin"):
        return bot_mod.is_admin(user_id)
    return user_id in config.admin_ids


def build_failed_queue_keyboard(limit: int = 20) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the interactive Dead-Letter Queue (DLQ) inspector UI and inline controls.
    """
    bot_mod = _get_bot_module()
    res_name_fn = getattr(bot_mod, "resolve_group_name", resolve_group_name) if bot_mod else resolve_group_name
    get_failed_fn = getattr(bot_mod, "get_failed_messages", get_failed_messages) if bot_mod else get_failed_messages
    count_failed_fn = getattr(bot_mod, "get_failed_messages_count", get_failed_messages_count) if bot_mod else get_failed_messages_count

    messages = get_failed_fn(limit=limit)
    count = count_failed_fn()

    IKM = _get_ikm()
    IKB = _get_ikb()
    keyboard = IKM(row_width=2)

    item_noun = "item" if count == 1 else "items"
    header = f"📬 <b>Failed Messages Queue ({count} {item_noun})</b>\n━━━━━━━━━━━━━━━━━━━━━━"

    if not messages:
        body = "\n✨ <i>Dead-Letter Queue is empty! All deliveries are healthy.</i>\n━━━━━━━━━━━━━━━━━━━━━━"
        keyboard.add(
            IKB(text="🔄 Refresh", callback_data="cb:dlq_refresh")
        )
        return f"{header}{body}", keyboard

    lines = []
    for idx, item in enumerate(messages, start=1):
        content_type = item.get("content_type", "text").capitalize()

        size_bytes = item.get("file_size_bytes") or 0
        size_str = ""
        if size_bytes >= 1024 * 1024:
            size_mb = round(size_bytes / (1024 * 1024))
            size_str = f" ({size_mb}MB)"

        target_name = item.get("group_name") or res_name_fn(item.get("group_id"))
        if len(target_name) > 25:
            target_name = target_name[:22] + "..."

        reason = item.get("reason") or "Delivery failed"
        lines.append(f"{idx}. {content_type}{size_str} -> {target_name} ({reason})")

    footer = "━━━━━━━━━━━━━━━━━━━━━━"
    if count > len(messages):
        footer = f"<i>...and {count - len(messages)} more items</i>\n━━━━━━━━━━━━━━━━━━━━━━"

    content = f"{header}\n" + "\n".join(lines) + f"\n{footer}"

    keyboard.row(
        IKB(text="🔄 Retry All Failed", callback_data="cb:dlq_retry"),
        IKB(text="🗑️ Purge Queue", callback_data="cb:dlq_purge"),
    )
    keyboard.add(
        IKB(text="🔄 Refresh", callback_data="cb:dlq_refresh")
    )

    return content, keyboard


async def retry_all_failed_messages() -> tuple[int, int]:
    """
    Re-dispatches queued failed messages through the token-bucket queue via send_to_single_group.
    Prunes successfully sent items and unlinks their temporary DLQ media files.
    Returns (recovered_count, still_failed_count).
    """
    bot_mod = _get_bot_module()
    get_failed_fn = getattr(bot_mod, "get_failed_messages", get_failed_messages) if bot_mod else get_failed_messages
    send_fn = getattr(bot_mod, "send_to_single_group", send_to_single_group) if bot_mod else send_to_single_group
    del_failed_fn = getattr(bot_mod, "delete_failed_message", delete_failed_message) if bot_mod else delete_failed_message
    inc_failed_fn = getattr(bot_mod, "increment_failed_message_retry", increment_failed_message_retry) if bot_mod else increment_failed_message_retry

    items = get_failed_fn(limit=50)
    if not items:
        return 0, 0

    recovered = 0
    failed = 0

    for item in items:
        item_id = item["id"]
        group = item["group_id"]
        content_type = item["content_type"]
        caption = item.get("caption") or ""
        original_filename = item.get("original_filename")
        media_path = item.get("media_path")

        if getattr(config, "BAN_VIDEO_FORWARDING", True) and content_type == "video":
            logger.info(
                f"🚫 [Video Ban] Skipping DLQ retry for video item {item_id} (video forwarding is banned)."
            )
            continue

        if content_type != "text" and (not media_path or not os.path.exists(media_path)):
            logger.warning(
                f"DLQ item {item_id} ({content_type}) cannot be retried: media file not available."
            )
            failed += 1
            await asyncio.to_thread(
                inc_failed_fn, item_id, "Media file missing"
            )
            continue

        try:
            success = await send_fn(
                group=group,
                downloaded_media=media_path,
                caption=caption,
                original_filename=original_filename,
                content_type=content_type,
                channel_id=item.get("channel_id"),
                is_retry=True,
            )
            if success:
                del_failed_fn(item_id)
                recovered += 1
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except Exception as e:
                        logger.error(f"Failed to remove DLQ media {media_path}: {e}")
            else:
                failed += 1
                await asyncio.to_thread(inc_failed_fn, item_id)
        except Exception as e:
            logger.error(f"Error retrying DLQ item {item_id}: {e}")
            failed += 1
            await asyncio.to_thread(inc_failed_fn, item_id, str(e))

    return recovered, failed


async def failed_queue_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    build_dlq_kb = getattr(bot_mod, "build_failed_queue_keyboard", build_failed_queue_keyboard) if bot_mod else build_failed_queue_keyboard
    text, kb = build_dlq_kb()
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_dlq_callbacks(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    data = call.data or ""
    bot_mod = _get_bot_module()
    build_dlq_kb = getattr(bot_mod, "build_failed_queue_keyboard", build_failed_queue_keyboard) if bot_mod else build_failed_queue_keyboard
    retry_fn = getattr(bot_mod, "retry_all_failed_messages", retry_all_failed_messages) if bot_mod else retry_all_failed_messages
    dlq_dir = getattr(bot_mod, "DLQ_MEDIA_DIR", DLQ_MEDIA_DIR) if bot_mod else DLQ_MEDIA_DIR
    get_failed_fn = getattr(bot_mod, "get_failed_messages", get_failed_messages) if bot_mod else get_failed_messages
    clear_failed_fn = getattr(bot_mod, "clear_failed_messages", clear_failed_messages) if bot_mod else clear_failed_messages

    if data == "cb:dlq_refresh":
        text, kb = build_dlq_kb()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer("Queue refreshed.")
        return

    elif data == "cb:dlq_purge":
        items = get_failed_fn(limit=500)
        for it in items:
            m_path = it.get("media_path")
            if m_path and os.path.exists(m_path):
                try:
                    os.remove(m_path)
                except Exception:
                    pass
        if os.path.exists(dlq_dir):
            for f in os.listdir(dlq_dir):
                try:
                    fp = os.path.join(dlq_dir, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass
        clear_failed_fn()
        await call.answer("🗑️ Failed queue purged!", show_alert=False)
        text, kb = build_dlq_kb()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    elif data == "cb:dlq_retry":
        await call.answer("🔄 Re-dispatching failed messages...", show_alert=False)
        recovered, failed = await retry_fn()
        text, kb = build_dlq_kb()
        prefix = f"🔄 <b>Retry Results</b>: {recovered} recovered, {failed} still failing.\n\n"
        try:
            await call.message.edit_text(prefix + text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return


def register_dlq_handlers(dp):
    dp.register_message_handler(failed_queue_command, commands=["failed", "dlq"])
    dp.register_callback_query_handler(
        handle_dlq_callbacks,
        lambda c: c.data in ("cb:dlq_refresh", "cb:dlq_purge", "cb:dlq_retry"),
    )
