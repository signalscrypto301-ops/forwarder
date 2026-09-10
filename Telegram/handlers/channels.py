import sys
import asyncio
import html
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ParseMode,
)

import config
from database import (
    add_channel,
    delete_channel,
    clean_id,
    set_channel_paused,
    set_all_channels_paused,
    get_channel_details,
    get_channels_overview,
    unlink_channel,
    deactivate_channel,
    activate_channel,
    delete_group_for_channel,
)


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


def build_channels_keyboard(page: int = 1, per_page: int = 6) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds a paginated inline keyboard displaying all monitored channels
    with status indicators (Active vs Paused), mapped group counts,
    and Master Emergency Kill Switch / Resume All controls.
    """
    IKM = _get_ikm()
    IKB = _get_ikb()

    channels_data = get_channels_overview()
    total_channels = len(channels_data)
    total_pages = max(1, (total_channels + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    paused_count = sum(1 for ch in channels_data if ch["is_paused"])
    active_count = total_channels - paused_count

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = channels_data[start_idx:end_idx]

    keyboard = IKM(row_width=1)

    # Master Emergency Control Row (Kill Switch / Resume All)
    if active_count > 0:
        pause_all_btn = IKB(
            text=f"🚨 Emergency Pause All ({active_count})",
            callback_data=f"cb:bulk_pause:{page}",
        )
    else:
        pause_all_btn = IKB(
            text="⏸️ All Channels Paused",
            callback_data="cb:noop",
        )

    if paused_count > 0:
        resume_all_btn = IKB(
            text=f"▶️ Resume All ({paused_count})",
            callback_data=f"cb:bulk_resume:{page}",
        )
    else:
        resume_all_btn = IKB(
            text="🟢 All Channels Active",
            callback_data="cb:noop",
        )

    keyboard.row(pause_all_btn, resume_all_btn)

    for item in page_channels:
        ch = item["channel_id"]
        paused = item["is_paused"]
        group_count = item["group_count"]
        title = item.get("title")
        status_icon = "⏸️" if paused else "🟢"
        state_str = "Paused" if paused else f"{group_count} grp{'s' if group_count != 1 else ''}"
        display_name = title if title else ch
        btn_text = f"{status_icon} {display_name} ({state_str})"
        keyboard.add(IKB(text=btn_text, callback_data=f"cb:view:{ch}:{page}"))

    # Navigation row
    nav_buttons = []
    if page > 1:
        nav_buttons.append(IKB(text="◀️ Prev", callback_data=f"cb:page:{page - 1}"))
    else:
        nav_buttons.append(IKB(text="⏹️ Start", callback_data="cb:noop"))

    nav_buttons.append(IKB(text=f"📄 {page}/{total_pages}", callback_data="cb:noop"))

    if page < total_pages:
        nav_buttons.append(IKB(text="Next ▶️", callback_data=f"cb:page:{page + 1}"))
    else:
        nav_buttons.append(IKB(text="⏹️ End", callback_data="cb:noop"))

    keyboard.row(*nav_buttons)

    # Action / Refresh row
    keyboard.row(
        IKB(text="🔄 Refresh", callback_data=f"cb:page:{page}"),
        IKB(text="🛡️ Health Stats", callback_data="cb:health"),
    )

    text = (
        "📢 <b>Monitored Channels Management</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Channels:</b> <code>{total_channels}</code>\n"
        f"• <b>Active:</b> <code>{active_count}</code> 🟢 | <b>Paused:</b> <code>{paused_count}</code> ⏸️\n"
        f"• <b>Page:</b> <code>{page} of {total_pages}</code>\n\n"
        "🚨 <b>Emergency Controls:</b> Use the top buttons to freeze or resume all channels at once.\n"
        "👇 <i>Tap any channel below to view its mapped WhatsApp destinations or toggle Pause / Resume:</i>"
    )
    return text, keyboard


def build_channel_detail_keyboard(channel_id: str, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the detail view for a specific channel with its mapped groups,
    one-tap Group Mapper Wizard launcher, and Pause/Resume / Delete action buttons.
    """
    IKM = _get_ikm()
    IKB = _get_ikb()

    details = get_channel_details(channel_id)
    paused = details["is_paused"]
    groups = details["groups"]
    last_post = details.get("last_post_at") or "None (No activity logged)"

    status_badge = "⏸️ PAUSED / DEACTIVATED (Forwarding Paused)" if paused else "🟢 ACTIVE (Forwarding Running)"
    toggle_btn_text = "▶️ Activate Channel (Resume Forwarding)" if paused else "⏸️ Deactivate Channel (Pause Forwarding)"
    toggle_action = "unpause" if paused else "pause"

    title = details.get("title")
    title_str = f" <b>{html.escape(title)}</b>" if title else ""

    groups_text = ""
    if groups:
        for idx, g in enumerate(groups, 1):
            kind = "📢 Newsletter" if "@newsletter" in g else "👥 Group"
            try:
                from services.whatsapp import resolve_group_name
                rname = resolve_group_name(g)
            except Exception:
                rname = g
            if rname and rname != g and not rname.endswith("@newsletter") and not rname.endswith("@g.us"):
                g_display = f"<b>{html.escape(rname)}</b> (<code>{html.escape(g)}</code>)"
            else:
                g_display = f"<code>{html.escape(g)}</code>"
            groups_text += f"\n  <code>{idx}.</code> <b>{kind}:</b> {g_display}"
    else:
        groups_text = "\n  <i>No WhatsApp groups mapped yet. Tap '➕ Map WhatsApp Destination' below!</i>"

    text = (
        f"📢 <b>Channel:</b>{title_str} (<code>{html.escape(channel_id)}</code>)\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> <b>{status_badge}</b>\n"
        f"• <b>Last Activity:</b> <code>{last_post}</code>\n"
        f"• <b>Mapped WhatsApp Destinations ({len(groups)}):</b>"
        f"{groups_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Tap 'Map WhatsApp Destination' to bind groups with one tap without typing JIDs.</i>"
    )

    keyboard = IKM(row_width=1)
    keyboard.add(
        IKB(
            text="➕ Map WhatsApp Destination",
            callback_data=f"m:p:{channel_id}:1:{page}",
        )
    )
    keyboard.add(
        IKB(
            text=toggle_btn_text,
            callback_data=f"cb:toggle:{channel_id}:{toggle_action}:{page}",
        )
    )

    if groups:
        if len(groups) > 1:
            keyboard.add(
                IKB(
                    text=f"🔗 Unlink All Destinations ({len(groups)})",
                    callback_data=f"cb:unlink_all_confirm:{channel_id}:{page}",
                )
            )
        for g in groups:
            kind = "📢" if "@newsletter" in g else "👥"
            short_g = (g[:18] + "...") if len(g) > 21 else g
            keyboard.add(
                IKB(
                    text=f"🔗 Unlink {kind} {short_g}",
                    callback_data=f"u:{channel_id}:{g}",
                )
            )

    keyboard.row(
        IKB(
            text="🗑️ Delete Channel",
            callback_data=f"cb:del_confirm:{channel_id}:{page}",
        ),
        IKB(
            text="◀️ Back to Channels",
            callback_data=f"cb:page:{page}",
        ),
    )
    return text, keyboard


def build_unlink_confirm_keyboard(channel_id: str, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds a confirmation dialog before unlinking all mapped WhatsApp destinations from a channel.
    """
    IKM = _get_ikm()
    IKB = _get_ikb()
    details = get_channel_details(channel_id)
    groups_count = len(details.get("groups", []))
    text = (
        f"⚠️ <b>Confirm Unlink All Destinations</b>\n\n"
        f"Are you sure you want to unlink all <code>{groups_count}</code> WhatsApp destination(s) from channel <code>{channel_id}</code>?\n\n"
        "• The channel will remain registered in your monitored list.\n"
        "• No messages will be forwarded until you map new destinations."
    )
    keyboard = IKM(row_width=2)
    keyboard.row(
        IKB(
            text=f"🔗 Yes, Unlink All ({groups_count})",
            callback_data=f"cb:unlink_all_exec:{channel_id}:{page}",
        ),
        IKB(
            text="❌ Cancel",
            callback_data=f"cb:view:{channel_id}:{page}",
        ),
    )
    return text, keyboard


def build_delete_confirm_keyboard(channel_id: str, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds a confirmation dialog before permanently deleting a channel.
    """
    IKM = _get_ikm()
    IKB = _get_ikb()
    text = (
        f"⚠️ <b>Confirm Channel Deletion</b>\n\n"
        f"Are you sure you want to delete channel <code>{channel_id}</code>?\n"
        "This will permanently unregister it and remove all mapped WhatsApp groups!"
    )
    keyboard = IKM(row_width=2)
    keyboard.row(
        IKB(
            text="⚠️ Yes, Delete",
            callback_data=f"cb:del_exec:{channel_id}:{page}",
        ),
        IKB(
            text="❌ Cancel",
            callback_data=f"cb:view:{channel_id}:{page}",
        ),
    )
    return text, keyboard


async def channels_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    kb_fn = getattr(bot_mod, "build_channels_keyboard", build_channels_keyboard) if bot_mod else build_channels_keyboard
    text, kb = kb_fn(page=1)
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def pause_all_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return
    count = await asyncio.to_thread(set_all_channels_paused, True)
    await message.reply(
        f"🚨 <b>Emergency Kill Switch Engaged</b>\n"
        f"All <code>{count}</code> monitored channels have been paused.\n"
        "Forwarding pipeline is frozen.\n\n"
        "Use /resume_all or /channels to restore message forwarding.",
        parse_mode=ParseMode.HTML,
    )


async def resume_all_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return
    count = await asyncio.to_thread(set_all_channels_paused, False)
    await message.reply(
        f"▶️ <b>Forwarding Resumed</b>\n"
        f"All <code>{count}</code> channels are now active and broadcasting.",
        parse_mode=ParseMode.HTML,
    )


async def deactivate_channel_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        overview = get_channels_overview()
        active = [c for c in overview if not c["is_paused"]]
        if not active:
            await message.reply(
                "⏸️ <b>Deactivate Channel Forwarding</b>\n\n"
                "All channels are currently paused.\n"
                "Usage: <code>/deactivate &lt;channel_id&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        text = (
            "⏸️ <b>Deactivate Channel Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Specify a channel ID to pause forwarding:\n"
            "<code>/deactivate &lt;channel_id&gt;</code>\n\n"
            "<b>Active Channels:</b>\n"
        )
        for item in active[:10]:
            text += f"• <code>{item['channel_id']}</code> ({item['group_count']} groups)\n"
        if len(active) > 10:
            text += f"\n<i>...and {len(active) - 10} more channels. Use /channels to view all.</i>"
        await message.reply(text, parse_mode=ParseMode.HTML)
        return

    channel_id = clean_id(args.strip())
    set_channel_paused(channel_id, True)
    await message.reply(
        f"⏸️ <b>Channel Deactivated</b>\n\n"
        f"Forwarding for channel <code>{channel_id}</code> is now <b>PAUSED</b>.\n"
        "Posts from this channel will NOT be forwarded to WhatsApp.\n\n"
        f"Use <code>/activate {channel_id}</code> or /channels to restore forwarding.",
        parse_mode=ParseMode.HTML,
    )


async def activate_channel_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        overview = get_channels_overview()
        paused = [c for c in overview if c["is_paused"]]
        if not paused:
            await message.reply(
                "▶️ <b>Activate Channel Forwarding</b>\n\n"
                "All channels are currently active.\n"
                "Usage: <code>/activate &lt;channel_id&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        text = (
            "▶️ <b>Activate Channel Forwarding</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Specify a channel ID to resume forwarding:\n"
            "<code>/activate &lt;channel_id&gt;</code>\n\n"
            "<b>Paused Channels:</b>\n"
        )
        for item in paused[:10]:
            text += f"• <code>{item['channel_id']}</code> ({item['group_count']} groups)\n"
        if len(paused) > 10:
            text += f"\n<i>...and {len(paused) - 10} more channels. Use /channels to view all.</i>"
        await message.reply(text, parse_mode=ParseMode.HTML)
        return

    channel_id = clean_id(args.strip())
    set_channel_paused(channel_id, False)
    await message.reply(
        f"▶️ <b>Channel Activated</b>\n\n"
        f"Forwarding for channel <code>{channel_id}</code> is now <b>ACTIVE</b>.\n"
        "Posts from this channel will be forwarded to mapped WhatsApp destinations.\n\n"
        f"Use <code>/deactivate {channel_id}</code> or /channels to pause forwarding.",
        parse_mode=ParseMode.HTML,
    )


async def unlink_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        await message.reply(
            "🔗 <b>Unlink Channel Destinations</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Remove WhatsApp destination mapping(s) from a Telegram channel.\n\n"
            "<b>Usage:</b>\n"
            "• <code>/unlink &lt;channel_id&gt;</code> - Unlink ALL WhatsApp destinations from this channel\n"
            "• <code>/unlink &lt;channel_id&gt; &lt;group_id&gt;</code> - Unlink a specific WhatsApp group/newsletter\n\n"
            "💡 <i>Tip: The channel remains registered in your bot. Use /channels to manage interactively.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    parts = args.strip().split(maxsplit=1)
    channel_id = clean_id(parts[0])

    if len(parts) >= 2:
        group_id = parts[1].strip()
        count = await asyncio.to_thread(unlink_channel, channel_id, group_id)
        if count > 0:
            await message.reply(
                f"🔗 <b>Destination Unlinked</b>\n\n"
                f"Removed WhatsApp destination <code>{group_id}</code> from channel <code>{channel_id}</code>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.reply(
                f"⚠️ Destination <code>{group_id}</code> was not mapped to channel <code>{channel_id}</code>.",
                parse_mode=ParseMode.HTML,
            )
    else:
        count = await asyncio.to_thread(unlink_channel, channel_id)
        if count > 0:
            await message.reply(
                f"🔗 <b>Channel Unlinked</b>\n\n"
                f"Unlinked all <code>{count}</code> WhatsApp destination(s) from channel <code>{channel_id}</code>.\n"
                "The channel remains registered in your monitored channels list.\n\n"
                f"Use <code>/map {channel_id}</code> or /channels to assign new destinations.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.reply(
                f"ℹ️ Channel <code>{channel_id}</code> has no mapped WhatsApp destinations to unlink.",
                parse_mode=ParseMode.HTML,
            )


async def add_channel_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        await message.reply("Usage: /add_channel <channel_id>")
        return

    channel_id = args.strip()
    title = None
    if getattr(message, "bot", None):
        try:
            chat = await message.bot.get_chat(clean_id(channel_id))
            if chat and getattr(chat, "title", None):
                title = chat.title
        except Exception:
            pass
    await asyncio.to_thread(add_channel, channel_id, title)
    title_str = f" (<b>{html.escape(title)}</b>)" if title else ""
    await message.reply(
        f"✅ Channel <code>{clean_id(channel_id)}</code>{title_str} registered successfully.",
        parse_mode=ParseMode.HTML,
    )


async def delete_channel_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        await message.reply("Usage: /delete_channel <channel_id>")
        return

    channel_id = args.strip()
    await asyncio.to_thread(delete_channel, channel_id)
    await message.reply(
        f"🗑️ Channel <code>{clean_id(channel_id)}</code> and all its group mappings deleted.",
        parse_mode=ParseMode.HTML,
    )


async def handle_channels_callbacks(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    data = call.data or ""
    if data == "cb:noop":
        await call.answer()
        return

    bot_mod = _get_bot_module()
    build_ch_kb = getattr(bot_mod, "build_channels_keyboard", build_channels_keyboard) if bot_mod else build_channels_keyboard
    build_det_kb = getattr(bot_mod, "build_channel_detail_keyboard", build_channel_detail_keyboard) if bot_mod else build_channel_detail_keyboard
    build_del_kb = getattr(bot_mod, "build_delete_confirm_keyboard", build_delete_confirm_keyboard) if bot_mod else build_delete_confirm_keyboard
    build_unl_kb = getattr(bot_mod, "build_unlink_confirm_keyboard", build_unlink_confirm_keyboard) if bot_mod else build_unlink_confirm_keyboard

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = build_ch_kb(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()

    elif action == "view":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = build_det_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()

    elif action == "toggle":
        cid = parts[2] if len(parts) > 2 else ""
        subaction = parts[3] if len(parts) > 3 else "pause"
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1

        new_paused = (subaction == "pause")
        set_channel_paused(cid, new_paused)

        toast = f"⏸️ Channel {cid} deactivated!" if new_paused else f"▶️ Channel {cid} activated!"
        await call.answer(toast, show_alert=False)

        text, kb = build_det_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "unlink_one":
        cid = parts[2] if len(parts) > 2 else ""
        gid = parts[3] if len(parts) > 3 else ""
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        delete_group_for_channel(cid, gid)
        await call.answer(f"🔗 Unlinked {gid}!", show_alert=False)
        text, kb = build_det_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "unlink_all_confirm":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = build_unl_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()

    elif action == "unlink_all_exec":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        count = unlink_channel(cid)
        await call.answer(f"🔗 Unlinked all {count} destinations!", show_alert=True)
        text, kb = build_det_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "del_confirm":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = build_del_kb(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()

    elif action == "del_exec":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        delete_channel(cid)
        await call.answer(f"🗑️ Channel {cid} deleted successfully!", show_alert=True)
        text, kb = build_ch_kb(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "bulk_pause":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        count = set_all_channels_paused(True)
        await call.answer(f"🚨 Emergency Stop: All {count} channels have been paused!", show_alert=True)
        text, kb = build_ch_kb(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "bulk_resume":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        count = set_all_channels_paused(False)
        await call.answer(f"▶️ Resumed: All {count} channels are active!", show_alert=False)
        text, kb = build_ch_kb(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "health":
        rc = getattr(bot_mod, "rate_controller", None)
        stats = rc.get_stats() if rc else {"status": "HEALTHY", "hour_count": 0, "max_per_hour": 120, "day_count": 0, "max_per_day": 1000, "active_queue_depth": 0}
        text = (
            f"Account Health: {stats['status']}\n"
            f"Hourly Volume: {stats['hour_count']}/{stats['max_per_hour']}\n"
            f"Daily Volume: {stats['day_count']}/{stats['max_per_day']}\n"
            f"Active Queue: {stats['active_queue_depth']} in-flight"
        )
        await call.answer(text, show_alert=True)


def register_channels_handlers(dp):
    dp.register_message_handler(channels_command, commands=["channels", "menu"])
    dp.register_message_handler(pause_all_command, commands=["pause_all", "freeze_all"])
    dp.register_message_handler(resume_all_command, commands=["resume_all", "unfreeze_all"])
    dp.register_message_handler(deactivate_channel_command, commands=["deactivate", "pause"])
    dp.register_message_handler(activate_channel_command, commands=["activate", "resume", "unpause"])
    dp.register_message_handler(unlink_command, commands=["unlink", "unmap"])
    dp.register_message_handler(add_channel_command, commands=["add_channel"])
    dp.register_message_handler(delete_channel_command, commands=["delete_channel"])
    dp.register_callback_query_handler(
        handle_channels_callbacks,
        lambda c: c.data and (
            c.data == "cb:noop"
            or c.data == "cb:health"
            or c.data.startswith("cb:page:")
            or c.data.startswith("cb:view:")
            or c.data.startswith("cb:toggle:")
            or c.data.startswith("cb:unlink_one:")
            or c.data.startswith("cb:unlink_all_confirm:")
            or c.data.startswith("cb:unlink_all_exec:")
            or c.data.startswith("cb:del_confirm:")
            or c.data.startswith("cb:del_exec:")
            or c.data.startswith("cb:bulk_pause:")
            or c.data.startswith("cb:bulk_resume:")
        ),
    )
