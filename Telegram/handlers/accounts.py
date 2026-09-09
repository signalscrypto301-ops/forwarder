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
from account_pool import ACCOUNT_INDICES, ACCOUNT_LABELS
from services.whatsapp import (
    post_whatsapp_json,
    generate_qr_code,
    _sync_account_pool_now,
)

try:
    import aiohttp
except ImportError:
    aiohttp = None


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
        IKB("🔄 Refresh Status", callback_data="cb:acc:refresh"),
        IKB("🌐 Test Proxies", callback_data="cb:acc:proxy_test"),
    )
    keyboard.row(
        IKB("🔑 Login Account...", callback_data="cb:acc:login_menu"),
        IKB("🚪 Logout Account...", callback_data="cb:acc:logout_menu"),
    )
    keyboard.row(
        IKB("📊 System Status", callback_data="cb:acc:status")
    )
    return keyboard


def _get_pool():
    bot_mod = _get_bot_module()
    if bot_mod and hasattr(bot_mod, "account_pool") and bot_mod.account_pool:
        return bot_mod.account_pool
    try:
        from account_pool import get_account_pool, default_account_pool
        return get_account_pool() or default_account_pool
    except Exception:
        return None


def build_account_select_keyboard(action_prefix: str) -> InlineKeyboardMarkup:
    IKM = _get_ikm()
    IKB = _get_ikb()
    pool = _get_pool()
    accounts = pool.get_all_accounts() if pool else []

    # If pool is not ready or returned empty, fallback to 4 canonical accounts
    if not accounts:
        accounts = [
            {"index": i, "is_ready": False, "status": "not_logged_in", "phone": None}
            for i in (1, 2, 3, 4)
        ]

    keyboard = IKM(row_width=2)
    btns = []
    for acc in accounts:
        idx = acc["index"]
        status_icon = "🟢" if acc.get("is_ready") else ("🟡" if acc.get("status") == "waiting_qr_scan" else "⚪")
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
        if hasattr(message_or_call, "reply")
        else getattr(getattr(message_or_call, "message", None), "reply", None)
    )
    send_photo = (
        message_or_call.reply_photo
        if hasattr(message_or_call, "reply_photo")
        else getattr(getattr(message_or_call, "message", None), "reply_photo", None)
    )

    bot_mod = _get_bot_module()
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    gen_qr_fn = getattr(bot_mod, "generate_qr_code", generate_qr_code) if bot_mod else generate_qr_code

    # Provide immediate visual feedback that login QR generation has started
    status_msg = None
    try:
        status_msg = await send_reply(
            f"⏳ <b>Generating WhatsApp Login QR for Account {account_index}...</b>\n"
            f"Connecting to WhatsApp network, please wait...",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        status_code, res_data = await post_fn("createsession", data, timeout_sec=40)
        if status_code == 200:
            if res_data.get("ready"):
                if status_msg:
                    try:
                        await status_msg.edit_text(
                            f"✅ <b>Account {account_index}</b> is already authorized and ready.",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        await send_reply(
                            f"✅ <b>Account {account_index}</b> is already authorized and ready.",
                            parse_mode=ParseMode.HTML,
                        )
                else:
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
                                f"Open WhatsApp on Phone {account_index}:\n"
                                f"<b>Settings ➔ Linked Devices ➔ Link a Device</b>\n\n"
                                f"Scan this QR code within 30 seconds to link Account {account_index}."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                finally:
                    if os.path.exists(qr_path):
                        os.remove(qr_path)
                await sync_fn()
            else:
                msg_text = f"ℹ️ Account {account_index}: {res_data.get('message', 'No QR code returned')}"
                if status_msg:
                    try:
                        await status_msg.edit_text(msg_text)
                    except Exception:
                        await send_reply(msg_text)
                else:
                    await send_reply(msg_text)
        elif status_code == 202:
            # Poll once more after a brief pause
            await asyncio.sleep(2.5)
            c2, r2 = await post_fn("createsession", data, timeout_sec=20)
            if c2 == 200 and r2.get("qrcode"):
                qr_path = gen_qr_fn(r2["qrcode"])
                try:
                    with open(qr_path, "rb") as qr_fp:
                        await send_photo(
                            qr_fp,
                            caption=(
                                f"📱 <b>WhatsApp Login QR Code</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"Target Slot: <b>Account {account_index}</b>\n\n"
                                f"Open WhatsApp on Phone {account_index}:\n"
                                f"<b>Settings ➔ Linked Devices ➔ Link a Device</b>\n\n"
                                f"Scan this QR code within 30 seconds to link Account {account_index}."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                finally:
                    if os.path.exists(qr_path):
                        os.remove(qr_path)
                await sync_fn()
                return

            wait_msg = f"⏳ Account {account_index} session is initializing in the background. Tap <code>/login {account_index}</code> in a few seconds."
            if status_msg:
                try:
                    await status_msg.edit_text(wait_msg, parse_mode=ParseMode.HTML)
                except Exception:
                    await send_reply(wait_msg, parse_mode=ParseMode.HTML)
            else:
                await send_reply(wait_msg, parse_mode=ParseMode.HTML)
        elif status_code == 400:
            err_msg = f"ℹ️ Account {account_index}: {res_data.get('message', 'Bad request')}"
            if status_msg:
                try:
                    await status_msg.edit_text(err_msg)
                except Exception:
                    await send_reply(err_msg)
            else:
                await send_reply(err_msg)
        else:
            fail_msg = f"❌ Failed to create session for Account {account_index} (HTTP {status_code})."
            if status_msg:
                try:
                    await status_msg.edit_text(fail_msg)
                except Exception:
                    await send_reply(fail_msg)
            else:
                await send_reply(fail_msg)
    except Exception as e:
        logger.error(f"Error in _perform_login_for_account({account_index}): {e}")
        err_out = f"❌ Error contacting WhatsApp service for Account {account_index}: {e}"
        if status_msg:
            try:
                await status_msg.edit_text(err_out)
            except Exception:
                await send_reply(err_out)
        else:
            await send_reply(err_out)


async def _perform_logout_for_account(message_or_call, account_index: int):
    target_id = ACCOUNT_INDICES.get(account_index, "user")
    data = {"clientId": target_id}
    send_reply = (
        message_or_call.reply
        if hasattr(message_or_call, "reply")
        else getattr(getattr(message_or_call, "message", None), "reply", None)
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

    elif action == "proxy_test":
        await call.answer("🔍 Probing proxies...", show_alert=False)
        proxy_cmd = getattr(bot_mod, "proxy_command", proxy_command) if bot_mod else proxy_command
        await proxy_cmd(call.message)
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


async def proxy_command(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    await sync_fn()

    args = message.get_args() if hasattr(message, "get_args") else ""
    target_idx = None
    if args and args.strip():
        try:
            target_idx = int(args.strip())
        except ValueError:
            target_idx = None

    url_base = config.whatsapp_service.rstrip("/")
    headers = {"x-api-key": config.api_secret} if config.api_secret else {}

    lines = [
        "🌐 <b>WhatsApp Multi-Account Proxy Diagnostics</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    accounts_to_check = [target_idx] if target_idx in (1, 2, 3, 4) else [1, 2, 3, 4]

    for idx in accounts_to_check:
        acc_id = ACCOUNT_INDICES[idx]
        label = ACCOUNT_LABELS[acc_id]
        test_url = f"{url_base}/proxy-test?clientId={acc_id}"

        if not aiohttp:
            lines.append(f"• <b>{label}</b>: ℹ️ aiohttp unavailable for live diagnostic query")
            continue

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(test_url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("proxyConfigured"):
                            status_icon = "🟢" if data.get("status") == "healthy" else "🔴"
                            country_flag = ""
                            cc = (data.get("countryCode") or "").upper()
                            if cc == "US": country_flag = "🇺🇸 "
                            elif cc in ("GB", "UK"): country_flag = "🇬🇧 "
                            elif cc == "DE": country_flag = "🇩🇪 "
                            elif cc == "CA": country_flag = "🇨🇦 "
                            elif cc == "FR": country_flag = "🇫🇷 "
                            elif cc == "IN": country_flag = "🇮🇳 "

                            lines.append(f"• <b>{label}</b>: {status_icon} <b>{str(data.get('status', 'unknown')).upper()}</b>")
                            lines.append(f"  └ 🔗 Host: <code>{data.get('maskedHost')}</code> ({data.get('protocol', 'proxy')})")
                            if data.get("exitIp"):
                                lines.append(f"  └ 🌍 Exit IP: <code>{data.get('exitIp')}</code> ({country_flag}{data.get('country', 'Online')})")
                            if data.get("latencyMs"):
                                lines.append(f"  └ ⚡ Latency: <b>{data.get('latencyMs')}ms</b>")
                            if data.get("error"):
                                lines.append(f"  └ ⚠️ Error: <i>{data.get('error')}</i>")
                        else:
                            lines.append(f"• <b>{label}</b>: 🏠 <b>DIRECT</b> (Host VPS IP)")
                            lines.append("  └ ℹ️ <i>No proxy configured. Using host broadband IP.</i>")
                    else:
                        lines.append(f"• <b>{label}</b>: ⚠️ Service returned HTTP {resp.status}")
        except Exception as e:
            lines.append(f"• <b>{label}</b>: ⚠️ Probe error ({e})")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>Tip: Set residential SOCKS5/HTTP proxies in docker-compose.yml or proxies.json to isolate IPs per account.</i>")

    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)


async def login_whatsapp(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = _get_pool()
    login_fn = getattr(bot_mod, "_perform_login_for_account", _perform_login_for_account) if bot_mod else _perform_login_for_account
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    kb_sel_fn = getattr(bot_mod, "build_account_select_keyboard", build_account_select_keyboard) if bot_mod else build_account_select_keyboard

    # Support multiple formats: "/login 2", "/login2", "/login account2", "2"
    args = message.get_args() if hasattr(message, "get_args") else ""
    raw_text = (message.text or "").strip()

    target_idx = None
    if args and args.strip():
        token = args.strip().split()[0]
        if pool:
            account_id = pool.resolve_account_id(token)
            target_idx = pool.get_index_for_id(account_id)
        else:
            try:
                target_idx = int(token)
            except ValueError:
                pass
    elif raw_text:
        import re
        m = re.search(r"^/login\s*([1-4])\b", raw_text, re.IGNORECASE)
        if m:
            target_idx = int(m.group(1))

    if target_idx in (1, 2, 3, 4):
        await login_fn(message, target_idx)
    else:
        await sync_fn()
        kb = kb_sel_fn("cb:acc:login")
        await message.reply(
            "🔑 <b>Select WhatsApp Account to Log In:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "You can run up to 4 parallel WhatsApp accounts simultaneously.\n"
            "Tap an account button below (or use <code>/login 1</code> to <code>/login 4</code>):",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


async def listen_whatsapp(message: Message):
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return

    bot_mod = _get_bot_module()
    pool = _get_pool()
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
    pool = _get_pool()
    logout_fn = getattr(bot_mod, "_perform_logout_for_account", _perform_logout_for_account) if bot_mod else _perform_logout_for_account
    sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
    kb_sel_fn = getattr(bot_mod, "build_account_select_keyboard", build_account_select_keyboard) if bot_mod else build_account_select_keyboard

    args = message.get_args() if hasattr(message, "get_args") else ""
    raw_text = (message.text or "").strip()

    target_idx = None
    if args and args.strip():
        token = args.strip().split()[0]
        if pool:
            account_id = pool.resolve_account_id(token)
            target_idx = pool.get_index_for_id(account_id)
        else:
            try:
                target_idx = int(token)
            except ValueError:
                pass
    elif raw_text:
        import re
        m = re.search(r"^/logout\s*([1-4])\b", raw_text, re.IGNORECASE)
        if m:
            target_idx = int(m.group(1))

    if target_idx in (1, 2, 3, 4):
        await logout_fn(message, target_idx)
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


async def account_number_quick_login(message: Message):
    """If an admin sends just '1', '2', '3', or '4' in private chat, initiate login for that slot."""
    if not message.chat.type == "private" or not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text in ("1", "2", "3", "4"):
        idx = int(text)
        bot_mod = _get_bot_module()
        login_fn = getattr(bot_mod, "_perform_login_for_account", _perform_login_for_account) if bot_mod else _perform_login_for_account
        await login_fn(message, idx)


def register_accounts_handlers(dp):
    dp.register_message_handler(accounts_command, commands=["accounts", "sessions", "pool"])
    dp.register_message_handler(proxy_command, commands=["proxy", "proxies"])
    dp.register_message_handler(login_whatsapp, commands=["login", "login1", "login2", "login3", "login4"])
    dp.register_message_handler(logout_whatsapp, commands=["logout", "logout1", "logout2", "logout3", "logout4"])
    dp.register_message_handler(listen_whatsapp, commands=["listen"])
    dp.register_callback_query_handler(
        account_pool_callback_handler,
        lambda c: c.data and c.data.startswith("cb:acc:"),
    )
    dp.register_message_handler(
        account_number_quick_login,
        lambda m: m.text and m.text.strip() in ("1", "2", "3", "4"),
    )
