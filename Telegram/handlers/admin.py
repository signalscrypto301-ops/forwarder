import os
import sys
import time
import io
import asyncio
from datetime import datetime

try:
    import psutil as _psutil
    HAS_PSUTIL = True
except ImportError:
    _psutil = None
    HAS_PSUTIL = False

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

import requests
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
    get_channels_overview,
    get_failed_messages_count,
    get_stale_channels,
    get_top_active_channels,
    get_hourly_traffic_distribution,
    get_channel_volume_summary,
    get_recent_auto_heals,
)
from account_pool import ACCOUNT_LABELS
from analytics_card import generate_analytics_infographic, generate_analytics_text_report
from services.whatsapp import (
    get_http_headers,
    get_http_session,
    post_whatsapp_json,
    _fetch_baileys_ram_mb,
    fetch_audience_metadata,
    audit_channel_admins,
)
from tasks.scheduler import generate_daily_report_text, generate_stale_channels_digest

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, "media")


from bot_context import get_bot_module as _get_bot_module


def _get_ikm():
    bot_mod = _get_bot_module()
    return getattr(bot_mod, "InlineKeyboardMarkup", InlineKeyboardMarkup) if bot_mod else InlineKeyboardMarkup


def _get_ikb():
    bot_mod = _get_bot_module()
    return getattr(bot_mod, "InlineKeyboardButton", InlineKeyboardButton) if bot_mod else InlineKeyboardButton


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def start_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(message.from_user.id):
        return
    await message.reply(
        "👋 Forwarder bot is operational.\nSend /help for available administrative commands."
    )


def build_help_keyboard() -> InlineKeyboardMarkup:
    """
    Creates an interactive inline keyboard for fast jumping to core administrative panels.
    """
    IKM = _get_ikm()
    IKB = _get_ikb()
    kb = IKM(row_width=2)
    kb.row(
        IKB("📱 Channels (/channels)", callback_data="cb:ch:refresh"),
        IKB("📊 Status (/status)", callback_data="cb:hl:refresh"),
    )
    kb.row(
        IKB("🛡️ Audit Admins", callback_data="cb:audit_admins"),
        IKB("📱 Accounts (/accounts)", callback_data="cb:acc:refresh"),
    )
    kb.row(
        IKB("📈 Analytics (/analytics)", callback_data="cb:ana:main"),
        IKB("📬 DLQ (/failed)", callback_data="cb:dlq_refresh"),
    )
    kb.row(
        IKB("🩺 Auto-Healer (/healer)", callback_data="cb:hl:refresh"),
        IKB("🔄 Refresh Help", callback_data="cb:help:refresh"),
    )
    return kb


def get_help_text() -> str:
    """
    Returns the comprehensive master slash command directory and documentation.
    """
    return (
        "🤖 <b>Forwarder Master Command Directory & Guide</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Tap any slash command below to execute it immediately, or use the quick buttons below:</i>\n\n"
        "⚡ <b>CHANNEL MANAGEMENT & FORWARDING:</b>\n"
        "• 📱 /channels <i>(alias: /menu)</i> — Interactive channel browser with status badges, Pause/Resume & Unlink buttons\n"
        "• ⏸️ /deactivate &lt;channel_id&gt; <i>(alias: /pause)</i> — Pause forwarding for a channel (retains all mappings). Without args: lists active channels\n"
        "• ▶️ /activate &lt;channel_id&gt; <i>(alias: /resume, /unpause)</i> — Resume forwarding for a paused channel. Without args: lists paused channels\n"
        "• 🔗 /unlink &lt;channel_id&gt; [group_id] <i>(alias: /unmap)</i> —\n"
        "   └ <code>/unlink &lt;id&gt; &lt;group_id&gt;</code>: Unlink a specific WhatsApp group/newsletter\n"
        "   └ <code>/unlink &lt;id&gt;</code>: Unlink ALL destinations (keeps channel registered)\n"
        "• 🚨 /pause_all <i>(alias: /freeze_all)</i> — Emergency kill switch (freeze forwarding on ALL channels)\n"
        "• ▶️ /resume_all <i>(alias: /unfreeze_all)</i> — Resume forwarding across all channels\n"
        "• 📢 /add_channel &lt;channel_id&gt; — Register new Telegram channel to monitor\n"
        "• 🗑️ /delete_channel &lt;channel_id&gt; — Unregister channel and remove all its mappings\n\n"
        "🪄 <b>DESTINATION MAPPING & WIZARDS:</b>\n"
        "• 🪄 /map [channel_id] <i>(alias: /map_group)</i> — One-tap interactive WhatsApp group mapper wizard\n"
        "• 🔍 /get_chat_id &lt;group_name&gt; — Find WhatsApp group or newsletter JID by title\n"
        "• ➕ /add_group &lt;channel_id&gt; &lt;group_id&gt; — Manually map channel to WhatsApp group\n"
        "• ➖ /delete_group &lt;channel_id&gt; &lt;group_id&gt; — Manually remove group mapping\n"
        "• 📋 /view_groups — List all registered channels and their mapped destinations\n\n"
        "🛡️ <b>ADMIN AUDIT & WHATSAPP ACCOUNTS:</b>\n"
        "• 🛡️ /audit_admins <i>(alias: /check_admins, /verify_admins, /admin_check)</i> — Audit Admin/Owner rights across all connected WhatsApp accounts and forwarded channels\n"
        "• 📱 /accounts <i>(alias: /sessions, /pool)</i> — Multi-account pool dashboard & sender manager\n"
        "• 🌐 /proxy [1-4] <i>(alias: /proxies)</i> — Test and inspect active residential proxy connection per slot\n"
        "• 🔑 /login [1-4] — Generate WhatsApp QR code for Account 1, 2, 3, or 4\n"
        "• 🚪 /logout [1-4] — Disconnect and clean a specific WhatsApp session\n"
        "• 🎧 /listen — Toggle listening to incoming WhatsApp messages\n\n"
        "📊 <b>MONITORING, HEALTH & PERFORMANCE:</b>\n"
        "• 📊 /status — Live health dashboard & channel statistics\n"
        "• 🩺 /healer <i>(alias: /autoheal, /ram)</i> — Automated System Health & RAM Auto-Healer dashboard\n"
        "• 🖥️ /telemetry <i>(alias: /server_health, /resources)</i> — Real-time server, RAM, CPU & process metrics\n"
        "• 📬 /failed <i>(alias: /dlq)</i> — Dead-Letter Queue (DLQ) inspector & retry manager\n"
        "• 🩺 /watchdog — WhatsApp socket health monitor & auto-reconnect status\n"
        "• 🛡️ /health_stats <i>(alias: /rate_status)</i> — WhatsApp deliverability rate & safety metrics\n\n"
        "📈 <b>ANALYTICS & INTELLIGENCE:</b>\n"
        "• 📈 /analytics <i>(alias: /audience, /traffic)</i> — Visual audience intelligence, top channels & heatmap\n"
        "• 📈 /report [YYYY-MM-DD] <i>(alias: /daily_report)</i> — Daily delivery report & performance summary\n"
        "• ⚠️ /stale [hours] <i>(alias: /stale_channels, /inactive_channels)</i> — Detect inactive channels with no posts (default 72h)\n\n"
        "⚙️ <b>SYSTEM & UTILITIES:</b>\n"
        "• 🚀 /start — Verify bot operational status and administrator authentication\n"
        "• ❓ /help — Show this complete master command directory and user guide\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌟 <b>INTERACTIVE PANEL FEATURES (/channels):</b>\n"
        "• <b>Status Badge:</b> 🟢 ACTIVE (Running) vs ⏸️ PAUSED / DEACTIVATED (Paused)\n"
        "• <b>One-Tap Toggle:</b> ⏸️ Deactivate / ▶️ Activate channel with instant toast feedback\n"
        "• <b>Granular Unlink:</b> 🔗 Unlink individual groups directly from the channel card\n"
        "• <b>Safe Unlink All:</b> Two-step confirmation prompt displaying destination count before unlinking"
    )


async def help_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    help_fn = getattr(bot_mod, "get_help_text", get_help_text) if bot_mod else get_help_text
    kb_fn = getattr(bot_mod, "build_help_keyboard", build_help_keyboard) if bot_mod else build_help_keyboard
    await message.reply(help_fn(), parse_mode=ParseMode.HTML, reply_markup=kb_fn())


async def handle_help_callback(call: CallbackQuery):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return

    await call.answer("📖 Command Directory Refreshed")
    help_fn = getattr(bot_mod, "get_help_text", get_help_text) if bot_mod else get_help_text
    kb_fn = getattr(bot_mod, "build_help_keyboard", build_help_keyboard) if bot_mod else build_help_keyboard
    try:
        await call.message.edit_text(help_fn(), parse_mode=ParseMode.HTML, reply_markup=kb_fn())
    except Exception:
        pass


async def setup_bot_commands(bot_instance):
    """
    Registers slash commands with Telegram Bot API so typing '/' in chat
    displays the full interactive auto-completion command list.
    """
    try:
        from aiogram.types import BotCommand as _RealBotCommand
    except Exception:
        _RealBotCommand = None

    class _SimpleCmd:
        def __init__(self, command, description):
            self.command = command
            self.description = description

    raw_commands = [
        ("help", "Master command directory & guide"),
        ("channels", "Interactive channel browser & menu"),
        ("status", "Live health dashboard & uptime"),
        ("audit_admins", "Audit WhatsApp Admin/Owner permissions"),
        ("accounts", "Multi-account pool & sender manager"),
        ("proxy", "Test residential proxy per slot [1-4]"),
        ("login", "Generate QR code for WhatsApp slot [1-4]"),
        ("logout", "Disconnect a WhatsApp slot session [1-4]"),
        ("map", "One-tap group/newsletter mapper wizard"),
        ("get_chat_id", "Find WhatsApp group/newsletter JID"),
        ("view_groups", "List all channels and mapped groups"),
        ("add_group", "Map channel to WhatsApp group"),
        ("delete_group", "Remove WhatsApp group mapping"),
        ("add_channel", "Register new Telegram channel"),
        ("delete_channel", "Unregister channel & remove mappings"),
        ("pause_all", "Emergency freeze on all channels"),
        ("resume_all", "Resume forwarding on all channels"),
        ("deactivate", "Pause forwarding for a channel"),
        ("activate", "Resume forwarding for a channel"),
        ("unlink", "Unlink destination from channel"),
        ("healer", "Auto-Healer & RAM cleanup dashboard"),
        ("telemetry", "Server CPU, RAM & process metrics"),
        ("analytics", "Audience analytics & traffic heatmap"),
        ("report", "Daily delivery summary report"),
        ("failed", "Dead-Letter Queue (DLQ) retry manager"),
        ("stale", "Detect inactive channels with no posts"),
        ("watchdog", "WhatsApp socket monitor & reconnect"),
        ("health_stats", "Deliverability rate & safety metrics"),
        ("listen", "Toggle incoming message listener"),
        ("start", "Start bot & verify admin access"),
    ]

    is_mock = hasattr(_RealBotCommand, "_mock_return_value") or "Mock" in type(_RealBotCommand).__name__
    cmd_cls = _SimpleCmd if is_mock or not _RealBotCommand else _RealBotCommand
    commands = [cmd_cls(c, d) for c, d in raw_commands]

    try:
        await bot_instance.set_my_commands(commands)
        logger.info(f"Registered {len(commands)} slash commands with Telegram Bot API.")
    except Exception as e:
        logger.warning(f"Could not register Telegram slash commands: {e}")


def get_server_telemetry(baileys_ram_mb: int | None = None) -> str:
    """
    Collects real-time host and container metrics using psutil.
    """
    bot_mod = _get_bot_module()
    psutil = getattr(bot_mod, "psutil", _psutil) if bot_mod else _psutil
    media_dir = getattr(bot_mod, "MEDIA_DIR", MEDIA_DIR) if bot_mod else MEDIA_DIR

    # 1. Host RAM
    if psutil:
        try:
            vm = psutil.virtual_memory()
            host_ram_pct = round(vm.percent)
            used_gb = vm.used / (1024 ** 3)
            total_gb = vm.total / (1024 ** 3)
            host_ram_str = f"{host_ram_pct}% used ({used_gb:.1f}GB / {total_gb:.1f}GB)"
        except Exception:
            host_ram_str = "Unavailable"
    else:
        host_ram_str = "Unavailable"

    # 2. CPU Load
    if psutil:
        try:
            cpu_pct = round(psutil.cpu_percent(interval=0.1))
            cpu_str = f"{cpu_pct}%"
        except Exception:
            cpu_str = "Unavailable"
    else:
        cpu_str = "Unavailable"

    # 3. Disk Space
    if psutil:
        try:
            target_path = os.path.dirname(os.path.dirname(__file__)) or "."
            disk = psutil.disk_usage(target_path)
            free_pct = round(100.0 - disk.percent)
            free_gb = disk.free / (1024 ** 3)
            disk_str = f"{free_pct}% free ({round(free_gb)}GB available)"
        except Exception:
            disk_str = "Unavailable"
    else:
        disk_str = "Unavailable"

    # 4. Baileys Socket RAM
    if baileys_ram_mb is not None:
        baileys_str = f"{baileys_ram_mb} MB"
    else:
        baileys_str = "N/A"

    # 5. Telegram Bot RAM
    if psutil:
        try:
            proc = psutil.Process(os.getpid())
            bot_ram_mb = round(proc.memory_info().rss / (1024 * 1024))
            bot_ram_str = f"{bot_ram_mb} MB"
        except Exception:
            bot_ram_str = "Unavailable"
    else:
        bot_ram_str = "Unavailable"

    # 6. Media Temp Directory
    try:
        if os.path.exists(media_dir):
            media_files = [
                f for f in os.listdir(media_dir)
                if os.path.isfile(os.path.join(media_dir, f))
            ]
            count = len(media_files)
        else:
            count = 0
        if count == 0:
            media_str = "0 files (Clean)"
        else:
            media_str = f"{count} files"
    except Exception:
        media_str = "0 files (Clean)"

    lines = [
        "🖥️ <b>Server & Engine Telemetry</b>",
        f"• Host RAM: {host_ram_str}",
        f"• CPU Load: {cpu_str}",
        f"• Disk Space: {disk_str}",
        f"• Baileys Socket RAM: {baileys_str}",
        f"• Telegram Bot RAM: {bot_ram_str}",
        f"• Media Temp Directory: {media_str}",
    ]
    return "\n".join(lines)


async def status_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    start_time = time.time()
    wa_status = "🔴 Disconnected"
    wa_details = ""
    latency_ms = 0
    baileys_ram_mb = None

    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session

    try:
        session = await get_session_fn()
        if session:
            async with session.get(f"{config.whatsapp_service}/health", timeout=5) as res:
                latency_ms = int((time.time() - start_time) * 1000)
                if res.status == 200:
                    data = await res.json()
                    wa_status = "🟢 Ready & Authorized"
                    wa_details = f"Active Sessions: {data.get('sessionsCount', 1)}"
                    baileys_ram_mb = data.get("memory", {}).get("rssMb")
                else:
                    wa_status = f"🟡 Initializing ({res.status})"
        else:
            res_sync = await asyncio.to_thread(
                requests.get,
                url=f"{config.whatsapp_service}/health",
                headers=get_http_headers(),
                timeout=5,
            )
            latency_ms = int((time.time() - start_time) * 1000)
            if res_sync.status_code == 200:
                data = res_sync.json()
                wa_status = "🟢 Ready & Authorized"
                wa_details = f"Active Sessions: {data.get('sessionsCount', 1)}"
                baileys_ram_mb = data.get("memory", {}).get("rssMb")
            else:
                wa_status = f"🟡 Initializing ({res_sync.status_code})"
    except Exception as e:
        wa_status = f"🔴 Unreachable: {e}"

    def _count_channels_and_mappings():
        overview = get_channels_overview()
        mappings_cnt = sum(ch["group_count"] for ch in overview)
        return len(overview), mappings_cnt

    total_channels, total_mappings = await asyncio.to_thread(_count_channels_and_mappings)
    rc = getattr(bot_mod, "rate_controller", None)
    rate_stats = rc.get_stats() if rc else {
        "status": "HEALTHY", "hour_count": 0, "max_per_hour": 120, "hour_percent": 0,
        "day_count": 0, "max_per_day": 1000, "day_percent": 0, "active_queue_depth": 0,
    }
    dlq_count = await asyncio.to_thread(get_failed_messages_count)
    telemetry_fn = getattr(bot_mod, "get_server_telemetry", get_server_telemetry) if bot_mod else get_server_telemetry
    telemetry_card = telemetry_fn(baileys_ram_mb=baileys_ram_mb)
    pool = getattr(bot_mod, "account_pool", None)
    pool_summary = pool.get_pool_summary() if pool else "N/A"

    status_text = (
        "📊 <b>Forwarder System Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>WhatsApp Gateway</b>: {wa_status} ({latency_ms} ms)\n"
        f"<b>WhatsApp Sender Pool</b>: <code>{pool_summary}</code>\n"
        f"<b>Account Health</b>: <code>{rate_stats['status']}</code>\n"
        f"<b>Hourly Volume</b>: <code>{rate_stats['hour_count']} / {rate_stats['max_per_hour']}</code> ({rate_stats['hour_percent']}%)\n"
        f"<b>Daily Volume</b>: <code>{rate_stats['day_count']} / {rate_stats['max_per_day']}</code> ({rate_stats['day_percent']}%)\n"
        f"<b>Active Queue Depth</b>: <code>{rate_stats['active_queue_depth']}</code> in-flight\n"
        f"<b>Dead-Letter Queue (DLQ)</b>: <code>{dlq_count} failed items</code>\n"
        f"<b>Monitored Channels</b>: <code>{total_channels}</code>\n"
        f"<b>Active Group Mappings</b>: <code>{total_mappings}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{telemetry_card}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ <i>System operational, rate-throttled and secure.</i>"
    )
    IKM = _get_ikm()
    IKB = _get_ikb()
    kb = IKM(row_width=2)
    kb.row(
        IKB("🛡️ Audit Channel Admins", callback_data="cb:audit_admins"),
        IKB("📱 Accounts Pool", callback_data="cb:acc:refresh"),
    )
    kb.row(
        IKB("🩺 Auto-Healer", callback_data="cb:hlr:refresh"),
        IKB("📈 Analytics", callback_data="cb:ana:main"),
    )
    await message.reply(status_text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def telemetry_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    fetch_ram_fn = getattr(bot_mod, "_fetch_baileys_ram_mb", _fetch_baileys_ram_mb) if bot_mod else _fetch_baileys_ram_mb
    baileys_ram_mb = await fetch_ram_fn()
    telemetry_fn = getattr(bot_mod, "get_server_telemetry", get_server_telemetry) if bot_mod else get_server_telemetry

    text = (
        f"{telemetry_fn(baileys_ram_mb)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Telemetry refreshed live.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


def build_healer_keyboard() -> InlineKeyboardMarkup:
    """Builds interactive management keyboard for the Auto-Healer dashboard."""
    IKM = _get_ikm()
    IKB = _get_ikb()
    kb = IKM(row_width=2)
    kb.row(
        IKB("🧹 Clean Caches Now", callback_data="cb:hlr:clean_now"),
    )
    kb.row(
        IKB("🔁 Soft-Restart WhatsApp", callback_data="cb:hlr:refresh_wa"),
        IKB("📋 Heal History", callback_data="cb:hlr:history"),
    )
    kb.row(
        IKB("🔄 Refresh Dashboard", callback_data="cb:hlr:refresh"),
    )
    return kb


async def healer_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    fetch_ram_fn = getattr(bot_mod, "_fetch_baileys_ram_mb", _fetch_baileys_ram_mb) if bot_mod else _fetch_baileys_ram_mb
    baileys_ram_mb = await fetch_ram_fn()
    healer = getattr(bot_mod, "auto_healer", None)
    dashboard_text = healer.format_dashboard_card(wa_memory_mb=baileys_ram_mb) if healer else "AutoHealer not available."
    kb_fn = getattr(bot_mod, "build_healer_keyboard", build_healer_keyboard) if bot_mod else build_healer_keyboard
    kb = kb_fn()
    await message.reply(dashboard_text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_healer_callbacks(call: CallbackQuery):
    """Handles inline button interactions for the /healer dashboard."""
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    action = call.data[7:]  # Strip 'cb:hlr:'
    healer = getattr(bot_mod, "auto_healer", None)
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
    fetch_ram_fn = getattr(bot_mod, "_fetch_baileys_ram_mb", _fetch_baileys_ram_mb) if bot_mod else _fetch_baileys_ram_mb
    kb_fn = getattr(bot_mod, "build_healer_keyboard", build_healer_keyboard) if bot_mod else build_healer_keyboard

    if action == "clean_now":
        await call.answer("🧹 Purging temporary caches & reclaiming memory...", show_alert=False)
        wa_res = {}
        try:
            status, wa_data = await post_fn("cleanup", {}, timeout_sec=10)
            if status == 200:
                wa_res = wa_data
        except Exception as e:
            logger.debug(f"[Healer] WhatsApp cleanup notice: {e}")

        result = healer.perform_heal(
            reason="Manual Admin Trigger", force=True, wa_cleanup_result=wa_res
        ) if healer else {}
        reclaimed_mb = result.get("mb_reclaimed", 0.0)
        files_purged = result.get("files_purged", 0)

        baileys_ram_mb = await fetch_ram_fn()
        dashboard_text = healer.format_dashboard_card(wa_memory_mb=baileys_ram_mb) if healer else ""
        kb = kb_fn()
        try:
            await call.message.edit_text(dashboard_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer(f"✅ Cleaned {files_purged} files! Freed {reclaimed_mb} MB.", show_alert=True)
        return

    elif action == "refresh_wa":
        await call.answer("🔁 Triggering WhatsApp soft-reconnect...", show_alert=False)
        try:
            status, data = await post_fn("createsession", {"clientId": "user"}, timeout_sec=30)
            if status == 200 and data.get("ready"):
                msg = "✅ WhatsApp session is already ready and active."
            elif data.get("qrcode"):
                msg = "⚠️ WhatsApp returned a QR code — check /login."
            else:
                msg = "🔁 WhatsApp socket reconnect initiated successfully."
        except Exception as e:
            msg = f"❌ WhatsApp reconnect failed: {e}"

        baileys_ram_mb = await fetch_ram_fn()
        dashboard_text = healer.format_dashboard_card(wa_memory_mb=baileys_ram_mb) if healer else ""
        kb = kb_fn()
        try:
            await call.message.edit_text(dashboard_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer(msg, show_alert=True)
        return

    elif action == "history":
        recent = get_recent_auto_heals(limit=8)
        if not recent:
            history_text = (
                "📋 <b>Auto-Healer Event History</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>No auto-heal events recorded yet. System has operated within healthy bounds.</i>"
            )
        else:
            lines = [
                "📋 <b>Auto-Healer Event History (Recent Events)</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for r in recent:
                reclaimed_str = f"{round(r['bytes_reclaimed'] / (1024*1024), 1)} MB" if r['bytes_reclaimed'] else "0 MB"
                lines.append(
                    f"• <b>{r['timestamp']}</b>\n"
                    f"  Trigger: <code>{r['trigger_reason']}</code>\n"
                    f"  Metrics: RAM {r['ram_percent']}%, Disk {r['disk_percent']}%\n"
                    f"  Reclaimed: {reclaimed_str} ({r['files_purged']} files)\n"
                    f"  Action: <i>{r['action_taken']}</i>\n"
                )
            history_text = "\n".join(lines)

        IKM = _get_ikm()
        IKB = _get_ikb()
        kb = IKM()
        kb.add(IKB("🔙 Back to Dashboard", callback_data="cb:hlr:refresh"))
        try:
            await call.message.edit_text(history_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()
        return

    elif action == "refresh":
        baileys_ram_mb = await fetch_ram_fn()
        dashboard_text = healer.format_dashboard_card(wa_memory_mb=baileys_ram_mb) if healer else ""
        kb = kb_fn()
        try:
            await call.message.edit_text(dashboard_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer("Dashboard refreshed.")
        return


async def health_stats_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    rc = getattr(bot_mod, "rate_controller", None)
    stats = rc.get_stats() if rc else {"status": "HEALTHY", "hour_count": 0, "max_per_hour": 120, "hour_percent": 0, "day_count": 0, "max_per_day": 1000, "day_percent": 0, "active_queue_depth": 0, "tracked_recipients": 0}
    status_icon = {
        "HEALTHY": "🟢 Healthy",
        "WARNING": "🟡 Warning (Adaptive 1.5x pacing active)",
        "CRITICAL": "🔴 Critical Throttle (Emergency 2.5x pacing active)",
    }.get(stats["status"], stats["status"])

    accounts_breakdown = ""
    if "accounts" in stats and len(stats["accounts"]) > 1:
        accounts_breakdown = "\n\n👥 <b>Per-Account Throughput:</b>\n"
        for acc_id, acc_stats in stats["accounts"].items():
            acc_icon = "🟢" if acc_stats["status"] == "HEALTHY" else ("🟡" if acc_stats["status"] == "WARNING" else "🔴")
            acc_label = ACCOUNT_LABELS.get(acc_id, acc_id)
            accounts_breakdown += (
                f"• <b>{acc_label}:</b> {acc_icon} "
                f"<code>{acc_stats['hour_count']} / {acc_stats['max_per_hour']} hr</code> ({acc_stats['hour_percent']}%) | "
                f"<code>{acc_stats['day_count']} / {acc_stats['max_per_day']} day</code>\n"
            )

    text = (
        "🛡️ <b>WhatsApp Anti-Ban & Deliverability Health</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Health State:</b> {status_icon}\n\n"
        "📈 <b>Volume Tracking (Pool Total):</b>\n"
        f"• <b>Hourly:</b> <code>{stats['hour_count']} / {stats['max_per_hour']}</code> ({stats['hour_percent']}%)\n"
        f"• <b>Daily:</b> <code>{stats['day_count']} / {stats['max_per_day']}</code> ({stats['day_percent']}%)\n"
        f"• <b>Warning Alert Threshold:</b> <code>{config.ALERT_THRESHOLD_PERCENT}%</code>\n\n"
        "⚙️ <b>Queue & Pacing Configuration:</b>\n"
        f"• <b>Active Queue Depth:</b> <code>{stats['active_queue_depth']}</code> in-flight\n"
        f"• <b>Tracked Recipient Buckets:</b> <code>{stats['tracked_recipients']}</code>\n"
        f"• <b>Token Refill Rate:</b> <code>{config.PER_RECIPIENT_RATE} tok/s (1 every {1/max(0.01, config.PER_RECIPIENT_RATE):.1f}s)</code>\n"
        f"• <b>Token Burst Capacity:</b> <code>{config.PER_RECIPIENT_BURST} tokens</code>"
        f"{accounts_breakdown}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Delays dynamically adjust based on queue depth and group/newsletter type to prevent automated spam detection.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


async def daily_report_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(message.from_user.id):
        return

    args = message.get_args().strip() if hasattr(message, "get_args") else ""
    target_date = None
    if args:
        try:
            datetime.strptime(args, "%Y-%m-%d")
            target_date = args
        except ValueError:
            await message.reply(
                "⚠️ Invalid date format. Please use <code>YYYY-MM-DD</code> (e.g., <code>/report 2026-09-07</code>).",
                parse_mode=ParseMode.HTML,
            )
            return

    gen_report_fn = getattr(bot_mod, "generate_daily_report_text", generate_daily_report_text) if bot_mod else generate_daily_report_text
    report_text = gen_report_fn(target_date)
    await message.reply(report_text)


async def stale_channels_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    threshold = 72
    args = message.get_args().strip() if hasattr(message, "get_args") else ""
    if args and args.isdigit():
        val = int(args)
        if 1 <= val <= 8760:
            threshold = val

    stale = await asyncio.to_thread(get_stale_channels, threshold)
    if not stale:
        await message.reply(
            f"✅ <b>No Stale Channels Detected</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"All monitored channels have received posts within the last <b>{threshold} hours</b>.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
        )
        return

    gen_digest_fn = getattr(bot_mod, "generate_stale_channels_digest", generate_stale_channels_digest) if bot_mod else generate_stale_channels_digest
    digest = gen_digest_fn(stale, threshold_hours=threshold)
    await message.reply(digest, parse_mode=ParseMode.HTML)


def build_analytics_keyboard() -> InlineKeyboardMarkup:
    IKM = _get_ikm()
    IKB = _get_ikb()
    kb = IKM(row_width=2)
    kb.row(
        IKB("🔄 Refresh", callback_data="cb:ana:ref"),
        IKB("📢 Top Channels", callback_data="cb:ana:top"),
    )
    kb.row(
        IKB("👥 Audience Breakdown", callback_data="cb:ana:aud"),
        IKB("📱 Channels Menu", callback_data="cb:page:1"),
    )
    return kb


def build_analytics_detail_keyboard() -> InlineKeyboardMarkup:
    IKM = _get_ikm()
    IKB = _get_ikb()
    kb = IKM(row_width=1)
    kb.add(IKB("🔙 Back to Analytics Overview", callback_data="cb:ana:main"))
    return kb


async def analytics_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
    hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
    summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
    fetch_aud_fn = getattr(bot_mod, "fetch_audience_metadata", fetch_audience_metadata) if bot_mod else fetch_audience_metadata
    audience_stats = await fetch_aud_fn()

    kb_fn = getattr(bot_mod, "build_analytics_keyboard", build_analytics_keyboard) if bot_mod else build_analytics_keyboard
    kb = kb_fn()

    img_bytes = generate_analytics_infographic(
        today_str,
        top_channels,
        hourly_dist,
        audience_stats,
        summary,
    )

    caption = (
        f"📊 <b>Audience &amp; Channel Intelligence ({today_str})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Audience:</b> <b>{audience_stats.get('totalAudience', 0):,}</b> "
        f"({audience_stats.get('groupsCount', 0)} groups, {audience_stats.get('newslettersCount', 0)} newsletters)\n"
        f"📈 <b>Posts Forwarded:</b> <b>{summary.get('total_posts', 0)}</b> across <b>{summary.get('active_channels', 0)}</b> channels\n"
        f"🔥 <b>Peak Traffic Window:</b> <b>{summary.get('peak_hour_label', 'N/A')}</b> ({summary.get('peak_hour_volume', 0)} posts)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>Tap 'Top Channels' or 'Audience Breakdown' below for details.</i>"
    )

    if img_bytes:
        photo = io.BytesIO(img_bytes)
        photo.name = f"analytics_{today_str}.png"
        try:
            await message.reply_photo(photo, caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            logger.warning(f"Failed to send analytics photo: {e}, falling back to text.")

    text_report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)
    await message.reply(text_report, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_analytics_callbacks(callback_query: CallbackQuery):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(callback_query.from_user.id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    action = callback_query.data[7:]
    today_str = datetime.now().strftime("%Y-%m-%d")
    fetch_aud_fn = getattr(bot_mod, "fetch_audience_metadata", fetch_audience_metadata) if bot_mod else fetch_audience_metadata
    kb_fn = getattr(bot_mod, "build_analytics_keyboard", build_analytics_keyboard) if bot_mod else build_analytics_keyboard
    kb_det_fn = getattr(bot_mod, "build_analytics_detail_keyboard", build_analytics_detail_keyboard) if bot_mod else build_analytics_detail_keyboard

    if action == "ref":
        await callback_query.answer("Refreshing analytics...", show_alert=False)
        top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
        hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
        summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
        audience_stats = await fetch_aud_fn(force_refresh=True)

        text_report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)
        try:
            await callback_query.message.edit_text(
                text_report,
                reply_markup=kb_fn(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.answer("Analytics refreshed! Send /analytics for new infographic.", show_alert=True)

    elif action == "top":
        await callback_query.answer()
        top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 15)
        lines = [
            "📢 <b>Top Active Channels Breakdown</b>",
            f"<i>Activity rankings for {today_str}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if top_channels:
            for c in top_channels:
                r = c.get("rank", 1)
                cid = c.get("channel_id")
                cnt = c.get("post_count")
                pct = c.get("percent")
                grps = c.get("groups_count", 0)
                lines.append(f"<code>{r}.</code> <code>{cid}</code>: <b>{cnt}</b> posts (<code>{pct}%</code>) • {grps} groups")
        else:
            lines.append("<i>No channel post activity recorded yet today.</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        try:
            await callback_query.message.edit_text(
                "\n".join(lines),
                reply_markup=kb_det_fn(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply("\n".join(lines), reply_markup=kb_det_fn(), parse_mode=ParseMode.HTML)

    elif action == "aud":
        await callback_query.answer()
        audience_stats = await fetch_aud_fn()
        destinations = audience_stats.get("destinations", [])
        lines = [
            "👥 <b>WhatsApp Audience Reach Breakdown</b>",
            f"Total Reach: <b>{audience_stats.get('totalAudience', 0):,}</b>",
            f"Newsletters: <b>{audience_stats.get('newsletterSubscribers', 0):,}</b> ({audience_stats.get('newslettersCount', 0)})",
            f"Groups: <b>{audience_stats.get('groupMembers', 0):,}</b> ({audience_stats.get('groupsCount', 0)})",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if destinations:
            for idx, d in enumerate(destinations[:20], 1):
                icon = "📢" if d.get("type") == "newsletter" else "💬"
                name = d.get("name") or d.get("id")
                cnt = d.get("count", 0)
                lines.append(f"{icon} <b>{name}</b>: <b>{cnt:,}</b>")
            if len(destinations) > 20:
                lines.append(f"<i>... and {len(destinations) - 20} more destinations.</i>")
        else:
            lines.append("<i>No WhatsApp destination metadata available.</i>")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        try:
            await callback_query.message.edit_text(
                "\n".join(lines),
                reply_markup=kb_det_fn(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply("\n".join(lines), reply_markup=kb_det_fn(), parse_mode=ParseMode.HTML)

    elif action == "main":
        await callback_query.answer()
        top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
        hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
        summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
        audience_stats = await fetch_aud_fn()

        text_report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)
        try:
            await callback_query.message.edit_text(
                text_report,
                reply_markup=kb_fn(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply(text_report, reply_markup=kb_fn(), parse_mode=ParseMode.HTML)


def format_admin_audit_report(data: dict) -> tuple[str, bool]:
    """
    Formats the WhatsApp admin audit report into human-readable HTML for Telegram.
    Returns (formatted_text, all_compliant).
    """
    all_compliant = data.get("allCompliant", False)
    total_destinations = data.get("totalDestinations", 0)
    total_ready = data.get("totalReadyAccounts", 0)
    total_configured = data.get("totalConfiguredAccounts", 4)
    issues = data.get("issues", [])

    if all_compliant:
        text = (
            "✅ <b>All Accounts Verified as Admins!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• <b>Active WhatsApp Accounts:</b> <code>{total_ready} / {total_configured}</code>\n"
            f"• <b>Forwarding Destinations Audited:</b> <code>{total_destinations}</code>\n"
            "• <b>Compliance Status:</b> <b>100% OK (All Admins/Owners)</b>\n\n"
            "🎉 <i>All connected WhatsApp accounts have verified Admin or Owner permissions across all forwarded channels and groups. Forwarding is fully authorized with zero permission restrictions.</i>"
        )
        return text, True

    header = (
        "⚠️ <b>WhatsApp Admin Permission Issues Detected!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "The following WhatsApp accounts are <b>NOT Admin or Owner</b> in the connected forwarding channels.\n"
        "<i>Forwarding messages from these accounts will fail until promoted!</i>\n\n"
    )

    issue_lines = []
    for idx, issue in enumerate(issues, 1):
        dest_id = issue.get("destinationId", "Unknown")
        dest_name = issue.get("destinationName") or dest_id
        dest_type = issue.get("destinationType", "channel").upper()
        acc = issue.get("account", {})
        phone = acc.get("phone") or "No Phone (Not Logged In)"
        label = acc.get("label") or f"Account {acc.get('index', '?')}"
        role = acc.get("role", "NOT ADMIN").upper()
        err_detail = f" <i>({acc.get('error')})</i>" if acc.get("error") and "session is not" not in acc.get("error", "").lower() else ""

        type_emoji = "📢" if dest_type == "NEWSLETTER" else "👥"
        card = (
            f"❌ <b>#{idx} {type_emoji} {dest_name}</b>\n"
            f"   └ 🆔 <b>ID:</b> <code>{dest_id}</code>\n"
            f"   └ 📱 <b>Mobile:</b> <code>{phone}</code> ({label})\n"
            f"   └ ⚠️ <b>Status:</b> <b>{role}</b>{err_detail}\n"
        )
        issue_lines.append(card)

    footer = (
        "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Summary:</b> <code>{len(issues)} issue(s)</code> across <code>{total_destinations} destinations</code>.\n\n"
        "👉 <b>Action Required:</b>\n"
        "1. Open WhatsApp.\n"
        "2. Open the respective Channel or Group settings.\n"
        "3. Promote the listed <b>Mobile Number(s)</b> to <b>Admin</b> or <b>Channel Owner</b>.\n"
    )

    full_text = header + "\n".join(issue_lines) + footer
    if len(full_text) > 4000:
        truncated_lines = issue_lines[:15]
        footer_trunc = f"\n<i>...and {len(issue_lines) - 15} more issues.</i>\n" + footer
        full_text = header + "\n".join(truncated_lines) + footer_trunc

    return full_text, False


async def audit_admins_command(message: Message):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not message.chat.type == "private" or not admin_fn(message.from_user.id):
        return

    status_msg = await message.reply(
        "🔍 <b>Auditing WhatsApp Admin Permissions...</b>\n"
        "<i>Inspecting all connected WhatsApp accounts against forwarded channels & groups...</i>",
        parse_mode=ParseMode.HTML,
    )

    audit_fn = getattr(bot_mod, "audit_channel_admins", audit_channel_admins) if bot_mod else audit_channel_admins

    try:
        status_code, data = await audit_fn()
        if status_code != 200:
            err_msg = data.get("message") or data.get("error") or f"HTTP {status_code}"
            await status_msg.edit_text(
                f"❌ <b>Admin Audit Failed:</b> {err_msg}",
                parse_mode=ParseMode.HTML,
            )
            return

        fmt_fn = getattr(bot_mod, "format_admin_audit_report", format_admin_audit_report) if bot_mod else format_admin_audit_report
        report_text, _ = fmt_fn(data)

        IKM = _get_ikm()
        IKB = _get_ikb()
        kb = IKM(row_width=2)
        kb.row(
            IKB("🔄 Re-Audit Now", callback_data="cb:audit_admins"),
            IKB("📱 Accounts Pool", callback_data="cb:acc:refresh"),
        )
        await status_msg.edit_text(report_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    except Exception as e:
        logger.error(f"Error during audit_admins_command: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ <b>Error running admin audit:</b> <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


async def handle_audit_admins_callback(call: CallbackQuery):
    bot_mod = _get_bot_module()
    admin_fn = getattr(bot_mod, "is_admin", is_admin) if bot_mod else is_admin
    if not admin_fn(call.from_user.id):
        await call.answer("Unauthorized", show_alert=True)
        return

    await call.answer("🔍 Auditing permissions...", show_alert=False)
    audit_fn = getattr(bot_mod, "audit_channel_admins", audit_channel_admins) if bot_mod else audit_channel_admins

    try:
        status_code, data = await audit_fn()
        if status_code != 200:
            await call.message.edit_text(
                f"❌ <b>Admin Audit Failed:</b> HTTP {status_code}",
                parse_mode=ParseMode.HTML,
            )
            return

        fmt_fn = getattr(bot_mod, "format_admin_audit_report", format_admin_audit_report) if bot_mod else format_admin_audit_report
        report_text, _ = fmt_fn(data)

        IKM = _get_ikm()
        IKB = _get_ikb()
        kb = IKM(row_width=2)
        kb.row(
            IKB("🔄 Re-Audit Now", callback_data="cb:audit_admins"),
            IKB("📱 Accounts Pool", callback_data="cb:acc:refresh"),
        )
        await call.message.edit_text(report_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    except Exception as e:
        logger.error(f"Error handling cb:audit_admins: {e}", exc_info=True)
        await call.message.edit_text(
            f"❌ <b>Error running admin audit:</b> <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


def register_admin_handlers(dp):
    dp.register_message_handler(start_command, commands=["start"])
    dp.register_message_handler(help_command, commands=["help"])
    dp.register_message_handler(status_command, commands=["status"])
    dp.register_message_handler(audit_admins_command, commands=["audit_admins", "check_admins", "verify_admins", "admin_check"])
    dp.register_message_handler(telemetry_command, commands=["telemetry", "server_health", "resources"])
    dp.register_message_handler(healer_command, commands=["healer", "autoheal", "ram"])
    dp.register_message_handler(health_stats_command, commands=["health_stats", "rate_status"])
    dp.register_message_handler(daily_report_command, commands=["report", "daily_report"])
    dp.register_message_handler(stale_channels_command, commands=["stale", "stale_channels", "inactive_channels"])
    dp.register_message_handler(analytics_command, commands=["analytics", "audience", "traffic"])
    dp.register_callback_query_handler(
        handle_healer_callbacks,
        lambda c: c.data and c.data.startswith("cb:hlr:"),
    )
    dp.register_callback_query_handler(
        handle_analytics_callbacks,
        lambda c: c.data and c.data.startswith("cb:ana:"),
    )
    dp.register_callback_query_handler(
        handle_audit_admins_callback,
        lambda c: c.data and c.data == "cb:audit_admins",
    )
    dp.register_callback_query_handler(
        handle_help_callback,
        lambda c: c.data and c.data == "cb:help:refresh",
    )

