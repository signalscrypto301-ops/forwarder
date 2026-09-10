import sys
import html
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
    add_channel,
    add_group_for_channel,
    get_all_channels,
    get_groups_for_channel,
    delete_group_for_channel,
    clean_id,
    get_channels_overview,
)
from services.whatsapp import (
    post_whatsapp_json,
    fetch_whatsapp_chats,
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


async def get_chat_id_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        await message.reply(
            "Please provide a group or channel name, link, or JID.\n\n"
            "<b>Examples:</b>\n"
            "• <code>/get_chat_id FOREX</code>\n"
            "• <code>/get_chat_id Bitcoin crypto Forex gold & Stock traders</code>\n"
            "• <code>/get_chat_id https://whatsapp.com/channel/0029Va...</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    clean_query = args.strip().strip("<>\"'`")
    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    active_sender = pool.get_ready_accounts()[0] if pool else "user"
    data = {
        "chatName": clean_query,
        "clientId": active_sender,
    }

    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

    try:
        status_code, res_data = await post_fn("getChatId", data, timeout_sec=30)
        if status_code == 200:
            matches = res_data.get("matches")
            if matches and len(matches) > 1:
                lines = [f"🔍 <b>Found {len(matches)} matching chats for \"{clean_query}\":</b>\n"]
                for i, m in enumerate(matches, 1):
                    icon = "📢" if m.get("isChannel") else "👥"
                    kind = "Channel" if m.get("isChannel") else "Group"
                    lines.append(
                        f"{i}. {icon} <b>{m.get('name')}</b> ({kind})\n"
                        f"   ID: <code>{m.get('groupId')}</code>\n"
                    )
                lines.append("💡 <i>Tap any ID above to copy, then map it with:</i>")
                lines.append("<code>/add_group &lt;channel_id&gt; &lt;group_id&gt;</code>")
                await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
            else:
                kind = "Channel / Newsletter" if res_data.get("isChannel") else "Group / Chat"
                icon = "📢" if res_data.get("isChannel") else "👥"
                await message.reply(
                    f"{icon} <b>{kind} Found:</b>\n"
                    f"Name: <b>{res_data.get('name', clean_query)}</b>\n"
                    f"ID: <code>{res_data.get('groupId')}</code>\n\n"
                    f"💡 <i>To map this to a channel, use:</i>\n"
                    f"<code>/add_group &lt;channel_id&gt; {res_data.get('groupId')}</code>",
                    parse_mode=ParseMode.HTML,
                )
        elif status_code in (400, 404):
            err_msg = res_data.get("message", "Group or channel not found")
            await message.reply(
                f"❌ <b>{err_msg}</b>\n\n"
                f"💡 <b>Tips to find your WhatsApp channel:</b>\n"
                f"• Try a shorter keyword: e.g. <code>/get_chat_id FOREX</code> or <code>/get_chat_id Bitcoin</code>\n"
                f"• Paste the channel link directly: <code>/get_chat_id https://whatsapp.com/channel/...</code>\n"
                f"• Use the interactive wizard: /map\n"
                f"• Make sure your WhatsApp account is an admin or follower of the channel.",
                parse_mode=ParseMode.HTML,
            )
        else:
            err_detail = res_data.get("error", res_data.get("message", "Unknown error"))
            await message.reply(f"❌ Failed: {err_detail}")
    except Exception as e:
        logger.error(f"Error in get_chat_id: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


async def add_group_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /add_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    await asyncio.to_thread(add_group_for_channel, channel_id, group_id)
    if "@newsletter" in group_id:
        bot_mod = _get_bot_module()
        post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
        asyncio.create_task(
            post_fn("registerNewsletters", {"newsletters": [group_id]}, timeout_sec=5)
        )
    await message.reply(
        f"✅ Group <code>{group_id}</code> mapped to channel <code>{clean_id(channel_id)}</code>.",
        parse_mode=ParseMode.HTML,
    )


async def delete_group_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /delete_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    await asyncio.to_thread(delete_group_for_channel, channel_id, group_id)
    await message.reply(
        f"🗑️ Group <code>{group_id}</code> removed from channel <code>{clean_id(channel_id)}</code>.",
        parse_mode=ParseMode.HTML,
    )


async def view_groups_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    channels = await asyncio.to_thread(get_all_channels)
    if not channels:
        await message.reply("No channels found.")
        return

    IKM = _get_ikm()
    IKB = _get_ikb()
    btn_kb = IKM().add(
        IKB(text="📱 Open Interactive Channel Browser", callback_data="cb:page:1")
    )

    reply = "<b>Channels and WhatsApp Mappings:</b>\n\n"
    for ch in channels:
        groups = await asyncio.to_thread(get_groups_for_channel, ch)
        dest_items = []
        for g in (groups or []):
            try:
                from services.whatsapp import resolve_group_name
                rname = resolve_group_name(g)
            except Exception:
                rname = g
            if rname and rname != g and not rname.endswith("@newsletter") and not rname.endswith("@g.us"):
                dest_items.append(f"{html.escape(rname)} (<code>{html.escape(g)}</code>)")
            else:
                dest_items.append(f"<code>{html.escape(g)}</code>")

        group_list = ", ".join(dest_items) if dest_items else "<i>None</i>"
        try:
            from database import get_channel_title
            ch_title = await asyncio.to_thread(get_channel_title, ch)
        except Exception:
            ch_title = None

        if ch_title:
            reply += f"📢 <b>{html.escape(ch_title)}</b> (<code>{html.escape(ch)}</code>)\n🔗 Destinations: {group_list}\n\n"
        else:
            reply += f"📢 Channel: <code>{html.escape(ch)}</code>\n🔗 Destinations: {group_list}\n\n"

    if len(reply) > 4000:
        chunks = [reply[i : i + 4000] for i in range(0, len(reply), 4000)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await message.reply(chunk, reply_markup=btn_kb, parse_mode=ParseMode.HTML)
            else:
                await message.reply(chunk, parse_mode=ParseMode.HTML)
    else:
        await message.reply(reply, reply_markup=btn_kb, parse_mode=ParseMode.HTML)


async def build_group_mapper_keyboard(
    channel_id: str, wizard_page: int = 1, channel_page: int = 1, per_page: int = 6
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the interactive WhatsApp group mapping wizard keyboard.
    Lists discovered WhatsApp groups & newsletters with one-tap bind/unbind toggles.
    """
    channel_id = clean_id(channel_id)
    mapped_groups = await asyncio.to_thread(get_groups_for_channel, channel_id)
    mapped_set = set(mapped_groups)

    bot_mod = _get_bot_module()
    fetch_chats_fn = getattr(bot_mod, "fetch_whatsapp_chats", fetch_whatsapp_chats) if bot_mod else fetch_whatsapp_chats
    chats = await fetch_chats_fn()
    total_chats = len(chats)
    total_pages = max(1, (total_chats + per_page - 1) // per_page)
    wizard_page = max(1, min(wizard_page, total_pages))

    start_idx = (wizard_page - 1) * per_page
    end_idx = start_idx + per_page
    page_chats = chats[start_idx:end_idx]

    IKM = _get_ikm()
    IKB = _get_ikb()
    keyboard = IKM(row_width=1)

    if not page_chats:
        keyboard.add(
            IKB(
                text="⚠️ No WhatsApp Groups Found (Check /status)",
                callback_data="cb:noop",
            )
        )
    else:
        for chat in page_chats:
            cid = chat.get("id", "")
            cname = chat.get("name", cid)
            ctype = chat.get("type", "group")
            icon = "📢" if ctype == "newsletter" else "👥"

            display_name = (cname[:20] + "...") if len(cname) > 23 else cname

            if cid in mapped_set:
                btn_text = f"✅ {icon} {display_name} (Mapped)"
                cb_data = f"u:{channel_id}:{cid}"
            else:
                btn_text = f"➕ {icon} {display_name}"
                cb_data = f"b:{channel_id}:{cid}"

            keyboard.add(IKB(text=btn_text, callback_data=cb_data))

    # Navigation row
    nav_buttons = []
    if wizard_page > 1:
        nav_buttons.append(
            IKB(
                text="◀️ Prev",
                callback_data=f"m:p:{channel_id}:{wizard_page - 1}:{channel_page}",
            )
        )
    else:
        nav_buttons.append(IKB(text="⏹️ Start", callback_data="cb:noop"))

    nav_buttons.append(
        IKB(
            text=f"📄 {wizard_page}/{total_pages}",
            callback_data="cb:noop",
        )
    )

    if wizard_page < total_pages:
        nav_buttons.append(
            IKB(
                text="Next ▶️",
                callback_data=f"m:p:{channel_id}:{wizard_page + 1}:{channel_page}",
            )
        )
    else:
        nav_buttons.append(IKB(text="⏹️ End", callback_data="cb:noop"))

    keyboard.row(*nav_buttons)

    # Action / Back row
    keyboard.row(
        IKB(
            text="🔄 Refresh",
            callback_data=f"m:p:{channel_id}:{wizard_page}:{channel_page}",
        ),
        IKB(
            text="◀️ Back to Channel",
            callback_data=f"cb:view:{channel_id}:{channel_page}",
        ),
    )

    text = (
        f"🪄 <b>WhatsApp Group Mapper Wizard</b>\n"
        f"<b>Target Channel:</b> <code>{channel_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Discovered WhatsApp Chats:</b> <code>{total_chats}</code> (Page {wizard_page}/{total_pages})\n"
        f"• <b>Currently Mapped:</b> <code>{len(mapped_groups)}</code> destinations\n\n"
        "👇 <i>Tap any WhatsApp group or newsletter below to instantly map or unmap it without typing JIDs:</i>"
    )
    return text, keyboard


async def map_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    build_wizard_fn = getattr(bot_mod, "build_group_mapper_keyboard", build_group_mapper_keyboard) if bot_mod else build_group_mapper_keyboard

    args = message.get_args().strip() if hasattr(message, "get_args") else ""
    if args:
        channel_id = clean_id(args)
        add_channel(channel_id)
        text, kb = await build_wizard_fn(channel_id, wizard_page=1, channel_page=1)
        await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        overview = await asyncio.to_thread(get_channels_overview)
        if not overview:
            await message.reply("No channels found. Use /add_channel <channel_id> first.")
            return

        IKM = _get_ikm()
        IKB = _get_ikb()
        kb = IKM(row_width=1)
        for item in overview[:8]:
            ch = item["channel_id"]
            paused = item["is_paused"]
            status_icon = "⏸️" if paused else "🟢"
            kb.add(
                IKB(
                    text=f"{status_icon} Map Channel {ch}",
                    callback_data=f"m:p:{ch}:1:1",
                )
            )
        if len(overview) > 8:
            kb.add(
                IKB(
                    text="📱 Browse All Channels",
                    callback_data="cb:page:1",
                )
            )

        text = (
            "🪄 <b>One-Tap WhatsApp Group Mapper</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Select which Telegram channel you want to map WhatsApp groups to:"
        )
        await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_group_mapper_callbacks(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    data = call.data or ""
    bot_mod = _get_bot_module()
    build_wizard_fn = getattr(bot_mod, "build_group_mapper_keyboard", build_group_mapper_keyboard) if bot_mod else build_group_mapper_keyboard
    build_det_fn = getattr(bot_mod, "build_channel_detail_keyboard", None)

    # Handle wizard pagination callback (m:p:<channel_id>:<wizard_page>:<channel_page>)
    if data.startswith("m:p:"):
        parts = data.split(":")
        cid = parts[2] if len(parts) > 2 else ""
        w_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        ch_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        text, kb = await build_wizard_fn(cid, wizard_page=w_page, channel_page=ch_page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()
        return

    # Handle one-tap group binding (b:<channel_id>:<group_id>)
    elif data.startswith("b:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            cid, gid = parts[1], parts[2]
            add_group_for_channel(cid, gid)
            await call.answer(f"✅ Mapped to {gid}!", show_alert=False)
            text, kb = await build_wizard_fn(cid, wizard_page=1, channel_page=1)
            try:
                await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    # Handle one-tap group unbinding (u:<channel_id>:<group_id>)
    elif data.startswith("u:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            cid, gid = parts[1], parts[2]
            delete_group_for_channel(cid, gid)
            await call.answer(f"🗑️ Unmapped {gid}", show_alert=False)
            msg_text = getattr(call.message, "text", "") or ""
            if "WhatsApp Group Mapper Wizard" in msg_text:
                text, kb = await build_wizard_fn(cid, wizard_page=1, channel_page=1)
            elif build_det_fn:
                text, kb = build_det_fn(cid, page=1)
            else:
                from handlers.channels import build_channel_detail_keyboard
                text, kb = build_channel_detail_keyboard(cid, page=1)
            try:
                await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return


def register_groups_handlers(dp):
    dp.register_message_handler(get_chat_id_command, commands=["get_chat_id"])
    dp.register_message_handler(add_group_command, commands=["add_group"])
    dp.register_message_handler(delete_group_command, commands=["delete_group"])
    dp.register_message_handler(view_groups_command, commands=["view_groups"])
    dp.register_message_handler(map_command, commands=["map", "map_group"])
    dp.register_callback_query_handler(
        handle_group_mapper_callbacks,
        lambda c: c.data and (
            c.data.startswith("m:p:")
            or c.data.startswith("b:")
            or c.data.startswith("u:")
        ),
    )
