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
from account_pool import ACCOUNT_INDICES
from services.whatsapp import (
    post_whatsapp_json,
    generate_qr_code,
    _sync_account_pool_now,
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


def build_accounts_keyboard() -> InlineKeyboardMarkup:
    IKM = _get_ikm()
    IKB = _get_ikb()
    keyboard = IKM(row_width=2)
    keyboard.row(
        IKB("🔄 Refresh Status", callback_data="cb:acc:refresh")
    )
    keyboard.row(
        IKB("🔑 Login Account...", callback_data="cb:acc:login_menu"),
        IKB("🚪 Logout Account...", callback_data="cb:acc:logout_menu"),
    )
    keyboard.row(
        IKB("📊 System Status", callback_data="cb:acc:status")
    )
    return keyboard


def build_account_select_keyboard(action_prefix: str) -> InlineKeyboardMarkup:
    IKM = _get_ikm()
    IKB = _get_ikb()
    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    accounts = pool.get_all_accounts() if pool else []

    keyboard = IKM(row_width=2)
    btns = []
    for acc in accounts:
        idx = acc["index"]
        status_icon = "🟢" if acc["is_ready"] else ("🟡" if acc["status"] == "waiting_qr_scan" else "⚪")
        phone_hint = f" ({acc['phone'][-4:]})" if acc.get("phone") else ""
        btns.append(
            IKB(
                f"{status_icon} Account {idx}{phone_hint}",
                callback_data=f"{action_prefix}:{idx}",
            )
        )
    keyboard.add(*btns)
    keyboard.row(IKB("🔙 Back to Pool", callback_data="cb:acc:back"))
    return keyboard


async def _perform_login_for_account(message_or_call, account_index: int):
    target_id = ACCOUNT_INDICES.get(account_index, "user")
    data = {"clientId": target_id}

    send_reply = (
        message_or_call.reply
        if isinstance(message_or_call, Message)
        else message_or_call.message.reply
    )
    send_photo = (
        message_or_call.reply_photo
        if isinstance(message_or_call, Message)
        else message_or_call.message.reply_photo
    )

    bot_mod = _get_bot_module()
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    gen_qr_fn = getattr(bot_mod, "generate_qr_code", generate_qr_code) if bot_mod else generate_qr_code

    try:
        status_code, res_data = await post_fn("createsession", data, timeout_sec=30)
        if status_code == 200:
            if res_data.get("ready"):
                await send_reply(
                    f"✅ <b>Account {account_index}</b> is already authorized and ready.",
                    parse_mode=ParseMode.HTML,
                )
                await sync_fn()
                return

            qr_data = res_data.get("qrcode")
            if qr_data:
                qr_path = gen_qr_fn(qr_data)
                try:
                    with open(qr_path, "rb") as qr_fp:
                        await send_photo(
                            qr_fp,
                            caption=(
                                f"📱 <b>WhatsApp Login QR Code</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Target Slot: <b>Account {account_index}</b>\n\n"
                                f"Open WhatsApp on the device you want to link:\n"
                                f"<b>Settings ➔ Linked Devices ➔ Link a Device</b>\n\n"
                                f"Once scanned, Account {account_index} will automatically join the active round-robin pool."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                finally:
                    if os.path.exists(qr_path):
                        os.remove(qr_path)
                await sync_fn()
            else:
                await send_reply(f"ℹ️ Account {account_index}: {res_data.get('message')}")
        elif status_code == 202:
            await send_reply(
                f"⏳ Account {account_index} session is initializing in the background. Please wait a few moments and run <code>/login {account_index}</code> or <code>/accounts</code>.",
                parse_mode=ParseMode.HTML,
            )
        elif status_code == 400:
            await send_reply(f"ℹ️ Account {account_index}: {res_data.get('message')}")
        else:
            await send_reply(f"❌ Failed to create session for Account {account_index}.")
    except Exception as e:
        logger.error(f"Error in _perform_login_for_account({account_index}): {e}")
        await send_reply(f"❌ Error contacting WhatsApp service for Account {account_index}: {e}")


async def _perform_logout_for_account(message_or_call, account_index: int):
    target_id = ACCOUNT_INDICES.get(account_index, "user")
    data = {"clientId": target_id}
    send_reply = (
        message_or_call.reply
        if isinstance(message_or_call, Message)
        else message_or_call.message.reply
    )

    bot_mod = _get_bot_module()
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now

    try:
        status_code, res_data = await post_fn("logout", data, timeout_sec=30)
        if status_code == 200:
            await send_reply(f"✅ Account {account_index} logged out and session cleared successfully.")
            await sync_fn()
        elif status_code == 400:
            await send_reply(f"ℹ️ Account {account_index}: {res_data.get('message')}")
        else:
            await send_reply(f"❌ Failed to log out Account {account_index}. Please try again later.")
    except Exception as e:
        logger.error(f"Error in _perform_logout_for_account({account_index}): {e}")
        await send_reply(f"❌ Failed to connect to WhatsApp service: {e}")


async def accounts_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    await sync_fn()

    pool = getattr(bot_mod, "account_pool", None)
    text = pool.format_dashboard_card() if pool else "Account pool not available."
    kb_fn = getattr(bot_mod, "build_accounts_keyboard", build_accounts_keyboard) if bot_mod else build_accounts_keyboard
    kb = kb_fn()
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def account_pool_callback_handler(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return

    action = call.data[len("cb:acc:"):]
    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    kb_fn = getattr(bot_mod, "build_accounts_keyboard", build_accounts_keyboard) if bot_mod else build_accounts_keyboard
    kb_sel_fn = getattr(bot_mod, "build_account_select_keyboard", build_account_select_keyboard) if bot_mod else build_account_select_keyboard

    if action == "refresh":
        await call.answer("🔄 Refreshing pool...", show_alert=False)
        await sync_fn()
        text = pool.format_dashboard_card() if pool else ""
        kb = kb_fn()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    elif action == "login_menu":
        await sync_fn()
        kb = kb_sel_fn("cb:acc:login")
        try:
            await call.message.edit_text(
                "🔑 <b>Select WhatsApp Account to Log In:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Choose an account slot to generate a QR code:\n"
                "• 🟢 = Already Active & Ready\n"
                "• 🟡 = Waiting for Scan\n"
                "• ⚪ = Not Logged In",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await call.answer()
        return

    elif action == "logout_menu":
        await sync_fn()
        kb = kb_sel_fn("cb:acc:logout")
        try:
            await call.message.edit_text(
                "🚪 <b>Select WhatsApp Account to Log Out:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Choose which account session to disconnect and wipe:",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await call.answer()
        return

    elif action == "back":
        await sync_fn()
        text = pool.format_dashboard_card() if pool else ""
        kb = kb_fn()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()
        return

    elif action == "status":
        await call.answer("Opening system status...", show_alert=False)
        status_cmd = getattr(bot_mod, "status_command", None)
        if status_cmd:
            await status_cmd(call.message)
        return

    elif action.startswith("login:"):
        idx_str = action.split(":", 1)[1]
        try:
            idx = int(idx_str)
        except ValueError:
            idx = 1
        await call.answer(f"Initiating login for Account {idx}...")
        login_fn = getattr(bot_mod, "_perform_login_for_account", _perform_login_for_account) if bot_mod else _perform_login_for_account
        await login_fn(call, idx)
        return

    elif action.startswith("logout:"):
        idx_str = action.split(":", 1)[1]
        try:
            idx = int(idx_str)
        except ValueError:
            idx = 1
        await call.answer(f"Logging out Account {idx}...")
        logout_fn = getattr(bot_mod, "_perform_logout_for_account", _perform_logout_for_account) if bot_mod else _perform_logout_for_account
        await logout_fn(call, idx)
        return


async def login_whatsapp(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    login_fn = getattr(bot_mod, "_perform_login_for_account", _perform_login_for_account) if bot_mod else _perform_login_for_account
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    kb_sel_fn = getattr(bot_mod, "build_account_select_keyboard", build_account_select_keyboard) if bot_mod else build_account_select_keyboard

    args = message.get_args()
    if args and args.strip() and pool:
        account_id = pool.resolve_account_id(args.strip())
        account_idx = pool.get_index_for_id(account_id)
        await login_fn(message, account_idx)
    else:
        await sync_fn()
        kb = kb_sel_fn("cb:acc:login")
        await message.reply(
            "🔑 <b>Select WhatsApp Account to Log In:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "You can run up to 4 parallel WhatsApp accounts simultaneously.\n"
            "Choose an account slot below (or use <code>/login 1</code> to <code>/login 4</code>):",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


async def listen_whatsapp(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    args = message.get_args()
    client_id = pool.resolve_account_id(args.strip()) if (args and args.strip() and pool) else "user"
    data = {"clientId": client_id}

    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

    try:
        status_code, res_data = await post_fn("startlistening", data, timeout_sec=30)
        if status_code in (200, 400):
            await message.reply(f"{res_data.get('message')}")
        else:
            await message.reply("Something went wrong.")
    except Exception as e:
        logger.error(f"Error in listen_whatsapp: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


async def logout_whatsapp(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    logout_fn = getattr(bot_mod, "_perform_logout_for_account", _perform_logout_for_account) if bot_mod else _perform_logout_for_account
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    kb_sel_fn = getattr(bot_mod, "build_account_select_keyboard", build_account_select_keyboard) if bot_mod else build_account_select_keyboard

    args = message.get_args()
    if args and args.strip() and pool:
        account_id = pool.resolve_account_id(args.strip())
        account_idx = pool.get_index_for_id(account_id)
        await logout_fn(message, account_idx)
    else:
        await sync_fn()
        kb = kb_sel_fn("cb:acc:logout")
        await message.reply(
            "🚪 <b>Select WhatsApp Account to Log Out:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose which account session to disconnect and clear:\n"
            "(Or use <code>/logout 1</code> to <code>/logout 4</code>)",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


def register_accounts_handlers(dp):
    dp.register_message_handler(accounts_command, commands=["accounts", "sessions", "pool"])
    dp.register_message_handler(login_whatsapp, commands=["login"])
    dp.register_message_handler(logout_whatsapp, commands=["logout"])
    dp.register_message_handler(listen_whatsapp, commands=["listen"])
    dp.register_callback_query_handler(
        account_pool_callback_handler,
        lambda c: c.data and c.data.startswith("cb:acc:"),
    )
