import os
import sys
import shutil
import asyncio
import random
import time
import io
from datetime import datetime, timedelta

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

import requests

import aiogram
from aiogram import types
from aiogram.utils import executor
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ParseMode,
    ContentType,
    MessageEntity,
)
import qrcode
from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from logger import logger
from database import (
    add_channel,
    add_group_for_channel,
    get_all_channels,
    get_groups_for_channel,
    delete_group_for_channel,
    delete_channel,
    clean_id,
    is_channel_paused,
    set_channel_paused,
    set_all_channels_paused,
    get_channel_details,
    get_active_channels_count,
    record_delivery_metric,
    get_daily_metrics,
    add_failed_message,
    get_failed_messages,
    delete_failed_message,
    clear_failed_messages,
    get_failed_messages_count,
    update_channel_last_post,
    get_channel_last_post,
    get_stale_channels,
    record_channel_post_activity,
    get_top_active_channels,
    get_hourly_traffic_distribution,
    get_channel_volume_summary,
)
from rate_limiter import DeliveryRateController
from analytics_card import (
    generate_analytics_infographic,
    generate_analytics_text_report,
)

bot = aiogram.Bot(config.bot_token)
dp = aiogram.Dispatcher(bot)

bot_client: TelegramClient | None = None
http_session: aiohttp.ClientSession | None = None

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
DLQ_MEDIA_DIR = os.path.join(MEDIA_DIR, "dlq")
os.makedirs(DLQ_MEDIA_DIR, exist_ok=True)

# Concurrency limiter to protect network and socket resources
UPLOAD_SEMAPHORE = asyncio.Semaphore(3)

# Anti-Ban Rate Limiter & Deliverability Controller (Optimization 2.A)
rate_controller = DeliveryRateController(
    max_per_hour=config.MAX_MSGS_PER_HOUR,
    max_per_day=config.MAX_MSGS_PER_DAY,
    alert_threshold_percent=config.ALERT_THRESHOLD_PERCENT,
    per_recipient_rate=config.PER_RECIPIENT_RATE,
    per_recipient_burst=config.PER_RECIPIENT_BURST,
)

# Debounced alert timestamp to prevent admin notification spam
last_auth_alert_time = 0.0

# Album debouncer state: maps media_group_id -> {"timestamp": float, "caption_sent": bool}
ALBUM_LOCK = asyncio.Lock()
PROCESSED_MEDIA_GROUPS: dict[str, dict] = {}

# ---------- Session Watchdog State ----------
_watchdog_consecutive_failures: int = 0   # consecutive /health checks returning not-ready
_watchdog_outage_start: float | None = None  # timestamp when outage was first detected
_watchdog_reconnect_lock = asyncio.Lock()   # prevents concurrent reconnect attempts
_watchdog_last_reconnect_at: float = 0.0   # last time we called /createsession


def is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


def get_http_headers() -> dict[str, str]:
    headers = {}
    if config.api_secret:
        headers["x-api-key"] = config.api_secret
    return headers


async def get_http_session():
    global http_session
    if not HAS_AIOHTTP:
        return None
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=90)
        http_session = aiohttp.ClientSession(
            headers=get_http_headers(), timeout=timeout
        )
    return http_session


async def post_whatsapp_json(endpoint: str, data: dict, timeout_sec: int = 30) -> tuple[int, dict]:
    """Unified POST helper supporting both aiohttp and requests fallback with authentication."""
    session = await get_http_session()
    url = f"{config.whatsapp_service}/{endpoint}"
    if session:
        async with session.post(url, json=data, timeout=timeout_sec) as res:
            try:
                res_data = await res.json()
            except Exception:
                res_data = {"message": await res.text()}
            return res.status, res_data
    else:
        def post_sync():
            return requests.post(
                url, json=data, headers=get_http_headers(), timeout=timeout_sec
            )
        res_sync = await asyncio.to_thread(post_sync)
        try:
            res_data = res_sync.json()
        except Exception:
            res_data = {"message": res_sync.text}
        return res_sync.status_code, res_data


def cleanup_stale_media(max_age_seconds: int = 600):
    """Purge temporary media files older than max_age_seconds (Fix 2.6)."""
    now = time.time()
    if not os.path.exists(MEDIA_DIR):
        return
    for fname in os.listdir(MEDIA_DIR):
        fpath = os.path.join(MEDIA_DIR, fname)
        try:
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > max_age_seconds):
                os.remove(fpath)
                logger.debug(f"Cleaned up stale media file: {fname}")
        except Exception as e:
            logger.error(f"Error removing {fpath}: {e}")

    # Also clean up any lingering qr_*.png files in working directory
    for fname in os.listdir(os.path.dirname(__file__)):
        if fname.startswith("qr_") and fname.endswith(".png"):
            fpath = os.path.join(os.path.dirname(__file__), fname)
            try:
                if now - os.path.getmtime(fpath) > 300:
                    os.remove(fpath)
            except Exception:
                pass


async def init_telethon_client():
    """Asynchronously initialize Telethon MTProto client with retry logic (Fix 2.3)."""
    global bot_client
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
            logger.info("Telethon MTProto bot client successfully connected.")
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

    async with ALBUM_LOCK:
        now = time.time()
        # Purge entries older than 120 seconds
        expired = [
            k for k, v in PROCESSED_MEDIA_GROUPS.items() if now - v["timestamp"] > 120
        ]
        for k in expired:
            PROCESSED_MEDIA_GROUPS.pop(k, None)

        album_info = PROCESSED_MEDIA_GROUPS.get(media_group_id)
        if album_info and album_info.get("caption_sent"):
            return False

        PROCESSED_MEDIA_GROUPS[media_group_id] = {
            "timestamp": now,
            "caption_sent": True,
        }
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
                    "⚠️ <b>WhatsApp Session Error</b>: The WhatsApp Web session is unauthorized or disconnected. Please send /login to re-authenticate.",
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
    image_path = os.path.join(
        os.path.dirname(__file__), f"qr_{int(datetime.now().timestamp())}.png"
    )
    img.save(image_path)
    return image_path


# ---------- Command Handlers ----------


@dp.message_handler(commands=["start"])
async def start_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply(
        "👋 Forwarder bot is operational.\nSend /help for available administrative commands."
    )


@dp.message_handler(commands=["help"])
async def help_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    help_text = (
        "🤖 <b>Forwarder Admin Commands</b>:\n\n"
        "📊 /status - Live health dashboard & channel statistics\n"
        "📈 /analytics - Visual audience intelligence, top channels & traffic heatmap\n"
        "🖥️ /telemetry - Real-time server and container resource metrics\n"
        "📈 /report [YYYY-MM-DD] - Daily delivery report & performance summary\n"
        "📬 /failed - Dead-Letter Queue (DLQ) inspector & retry manager\n"
        "⚠️ /stale [hours] - Detect inactive channels with no posts (default 72h)\n"
        "📱 /channels - Interactive channel browser with Pause/Resume buttons\n"
        "🚨 /pause_all - Emergency kill switch (freeze all channels)\n"
        "▶️ /resume_all - Resume forwarding on all channels\n"
        "🪄 /map [channel_id] - One-tap interactive WhatsApp group mapper wizard\n"
        "🛡️ /health_stats - WhatsApp deliverability rate & safety metrics\n"
        "🩺 /watchdog - WhatsApp socket health monitor & auto-reconnect status\n"
        "🔑 /login - Generate WhatsApp login QR code\n"
        "🚪 /logout - Disconnect and clean WhatsApp session\n"
        "🔍 /get_chat_id &lt;group_name&gt; - Find WhatsApp group or newsletter JID\n"
        "➕ /add_group &lt;channel_id&gt; &lt;group_id&gt; - Map channel to WhatsApp group\n"
        "➖ /delete_group &lt;channel_id&gt; &lt;group_id&gt; - Remove group mapping\n"
        "🗑️ /delete_channel &lt;channel_id&gt; - Unregister channel and all its mappings\n"
        "📋 /view_groups - List all channels and mapped groups\n"
        "📢 /add_channel &lt;channel_id&gt; - Register new Telegram channel\n"
        "🎧 /listen - Toggle listening to incoming WhatsApp messages"
    )
    await message.reply(help_text, parse_mode=ParseMode.HTML)


def get_server_telemetry(baileys_ram_mb: int | None = None) -> str:
    """
    Collects real-time host and container metrics using psutil:
    🖥️ Server & Engine Telemetry
    • Host RAM: 42% used (3.4GB / 8.0GB)
    • CPU Load: 12%
    • Disk Space: 78% free (45GB available)
    • Baileys Socket RAM: 48 MB
    • Telegram Bot RAM: 62 MB
    • Media Temp Directory: 0 files (Clean)
    """
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
            target_path = os.path.dirname(__file__) or "."
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
        if os.path.exists(MEDIA_DIR):
            media_files = [
                f for f in os.listdir(MEDIA_DIR)
                if os.path.isfile(os.path.join(MEDIA_DIR, f))
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


@dp.message_handler(commands=["status"])
async def status_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    start_time = time.time()
    wa_status = "🔴 Disconnected"
    wa_details = ""
    latency_ms = 0
    baileys_ram_mb = None

    try:
        session = await get_http_session()
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

    channels = await asyncio.to_thread(get_all_channels)
    total_channels = len(channels)
    total_mappings = sum(
        len(await asyncio.to_thread(get_groups_for_channel, ch)) for ch in channels
    )
    rate_stats = rate_controller.get_stats()
    dlq_count = await asyncio.to_thread(get_failed_messages_count)
    telemetry_card = get_server_telemetry(baileys_ram_mb=baileys_ram_mb)

    status_text = (
        "📊 <b>Forwarder System Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>WhatsApp Gateway</b>: {wa_status} ({latency_ms} ms)\n"
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
    await message.reply(status_text, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["telemetry", "server_health", "resources"])
async def telemetry_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    baileys_ram_mb = None
    try:
        session = await get_http_session()
        if session:
            async with session.get(f"{config.whatsapp_service}/health", timeout=3) as res:
                if res.status == 200:
                    data = await res.json()
                    baileys_ram_mb = data.get("memory", {}).get("rssMb")
        else:
            res_sync = await asyncio.to_thread(
                requests.get,
                url=f"{config.whatsapp_service}/health",
                headers=get_http_headers(),
                timeout=3,
            )
            if res_sync.status_code == 200:
                data = res_sync.json()
                baileys_ram_mb = data.get("memory", {}).get("rssMb")
    except Exception:
        pass

    text = (
        f"{get_server_telemetry(baileys_ram_mb)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Telemetry refreshed live.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["health_stats", "rate_status"])
async def health_stats_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    stats = rate_controller.get_stats()
    status_icon = {
        "HEALTHY": "🟢 Healthy",
        "WARNING": "🟡 Warning (Adaptive 1.5x pacing active)",
        "CRITICAL": "🔴 Critical Throttle (Emergency 2.5x pacing active)",
    }.get(stats["status"], stats["status"])

    text = (
        "🛡️ <b>WhatsApp Anti-Ban & Deliverability Health</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Health State:</b> {status_icon}\n\n"
        "📈 <b>Volume Tracking:</b>\n"
        f"• <b>Hourly:</b> <code>{stats['hour_count']} / {stats['max_per_hour']}</code> ({stats['hour_percent']}%)\n"
        f"• <b>Daily:</b> <code>{stats['day_count']} / {stats['max_per_day']}</code> ({stats['day_percent']}%)\n"
        f"• <b>Warning Alert Threshold:</b> <code>{config.ALERT_THRESHOLD_PERCENT}%</code>\n\n"
        "⚙️ <b>Queue & Pacing Configuration:</b>\n"
        f"• <b>Active Queue Depth:</b> <code>{stats['active_queue_depth']}</code> in-flight\n"
        f"• <b>Tracked Recipient Buckets:</b> <code>{stats['tracked_recipients']}</code>\n"
        f"• <b>Token Refill Rate:</b> <code>{config.PER_RECIPIENT_RATE} tok/s (1 every {1/max(0.01, config.PER_RECIPIENT_RATE):.1f}s)</code>\n"
        f"• <b>Token Burst Capacity:</b> <code>{config.PER_RECIPIENT_BURST} tokens</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Delays dynamically adjust based on queue depth and group/newsletter type to prevent automated spam detection.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


def generate_daily_report_text(date_str: str | None = None) -> str:
    """
    Generates formatted daily delivery report string matching:
    📊 Daily Forwarder Report (Sep 07)
    ━━━━━━━━━━━━━━━━━━━━━━
    ✅ Forwarded: 420 messages (310 text, 85 photos, 25 videos)
    ❌ Failed: 2 (retried successfully)
    ⏱️ Avg Delivery Latency: 1.2s
    🔋 Active Channels: 65
    ━━━━━━━━━━━━━━━━━━━━━━
    """
    today_default = datetime.now().strftime("%Y-%m-%d")
    target_date = date_str or today_default

    metrics = get_daily_metrics(target_date)
    active_channels = get_active_channels_count()

    try:
        date_obj = datetime.strptime(target_date, "%Y-%m-%d")
        date_display = date_obj.strftime("%b %d")
    except Exception:
        date_display = target_date

    parts = []
    if metrics["text"] > 0:
        parts.append(f"{metrics['text']} text")
    if metrics["photos"] > 0:
        parts.append(f"{metrics['photos']} photos")
    if metrics["videos"] > 0:
        parts.append(f"{metrics['videos']} videos")
    if metrics["docs"] > 0:
        parts.append(f"{metrics['docs']} docs")
    if metrics["audio"] > 0:
        parts.append(f"{metrics['audio']} audio")

    if metrics["total_forwarded"] == 0:
        breakdown_str = " (0 text, 0 photos, 0 videos)"
    elif parts:
        breakdown_str = f" ({', '.join(parts)})"
    else:
        breakdown_str = ""

    failed_c = metrics["failed"]
    retried_c = metrics["retried_success"]
    if failed_c == 0 and retried_c == 0:
        failed_str = "❌ Failed: 0"
    elif failed_c == 0 and retried_c > 0:
        failed_str = f"❌ Failed: {retried_c} (retried successfully)"
    elif failed_c > 0 and retried_c > 0:
        failed_str = f"❌ Failed: {failed_c} ({retried_c} retried successfully)"
    else:
        failed_str = f"❌ Failed: {failed_c}"

    latency_s = metrics["avg_latency_s"]

    lines = [
        f"📊 Daily Forwarder Report ({date_display})",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ Forwarded: {metrics['total_forwarded']} messages{breakdown_str}",
        failed_str,
        f"⏱️ Avg Delivery Latency: {latency_s:.1f}s",
        f"🔋 Active Channels: {active_channels}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


@dp.message_handler(commands=["report", "daily_report"])
async def daily_report_command(message: Message):
    if not is_admin(message.from_user.id):
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

    report_text = generate_daily_report_text(target_date)
    await message.reply(report_text)


def generate_stale_channels_digest(stale_channels: list[dict], threshold_hours: int = 72) -> str:
    """
    Generates an alert digest for channels with no activity for > threshold_hours:
    ⚠️ Stale Channel Alert: 3 channels have had no new posts in 72 hours.
    ━━━━━━━━━━━━━━━━━━━━━━
    The following monitored channels have had zero activity for > 72h:
    • <code>-1001234567890</code> — Inactive for 78h (2 groups)
    • <code>-1009876543210</code> — Inactive for 94h (1 group) [PAUSED]
    ━━━━━━━━━━━━━━━━━━━━━━
    💡 Check if bot permissions were revoked or if the source is abandoned.
    """
    count = len(stale_channels)
    channel_word = "channel" if count == 1 else "channels"
    has_have = "has" if count == 1 else "have"

    lines = [
        f"⚠️ <b>Stale Channel Alert: {count} {channel_word} {has_have} had no new posts in {threshold_hours} hours.</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not stale_channels:
        lines.append("✅ All monitored channels are active and posting normally.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    lines.append(f"The following monitored channels have had zero activity for &gt; {threshold_hours}h:")
    for item in stale_channels[:25]:
        cid = item.get("channel_id", "Unknown")
        hrs = item.get("hours_inactive", threshold_hours)
        groups_count = item.get("groups_count", 0)
        paused_tag = " <b>[PAUSED]</b>" if item.get("is_paused") else ""
        g_label = f"({groups_count} groups)" if groups_count != 1 else "(1 group)"
        lines.append(f"• <code>{cid}</code> — Inactive for {hrs}h {g_label}{paused_tag}")

    if count > 25:
        lines.append(f"<i>... and {count - 25} more inactive channels.</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 <i>Check if bot permissions were revoked or if the source is abandoned.</i>")
    return "\n".join(lines)


@dp.message_handler(commands=["stale", "stale_channels", "inactive_channels"])
async def stale_channels_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
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

    digest = generate_stale_channels_digest(stale, threshold_hours=threshold)
    await message.reply(digest, parse_mode=ParseMode.HTML)


# ---------- Audience & Channel Intelligence (Optimization 6.A) ----------

AUDIENCE_CACHE: dict = {"timestamp": 0.0, "data": None}
AUDIENCE_CACHE_LOCK = asyncio.Lock()


async def fetch_audience_metadata(force_refresh: bool = False) -> dict:
    """
    Fetches aggregate audience statistics (groups, newsletters, subscribers)
    from the Baileys WhatsApp microservice. Cached for 15 minutes to reduce socket queries.
    """
    global AUDIENCE_CACHE
    now = time.time()
    if (
        not force_refresh
        and AUDIENCE_CACHE["data"] is not None
        and (now - AUDIENCE_CACHE["timestamp"]) < 900
    ):
        return AUDIENCE_CACHE["data"]

    async with AUDIENCE_CACHE_LOCK:
        if (
            not force_refresh
            and AUDIENCE_CACHE["data"] is not None
            and (now - AUDIENCE_CACHE["timestamp"]) < 900
        ):
            return AUDIENCE_CACHE["data"]

        session = await get_http_session()
        data = None
        if session:
            try:
                url = f"{config.whatsapp_service}/audience-stats"
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
            except Exception as e:
                logger.warning(f"Failed to fetch /audience-stats from WhatsApp service: {e}")

        # Fallback if /audience-stats is unavailable: compute from /groups
        if not data or not isinstance(data, dict) or "totalAudience" not in data:
            try:
                url = f"{config.whatsapp_service}/groups"
                if session:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            res_json = await resp.json()
                            chats = res_json.get("chats", [])
                            groups_count = sum(1 for c in chats if c.get("type") == "group")
                            nl_count = sum(1 for c in chats if c.get("type") == "newsletter")
                            grp_members = sum(c.get("participantsCount", 0) for c in chats if c.get("type") == "group")
                            nl_subs = sum(c.get("participantsCount", 0) for c in chats if c.get("type") == "newsletter")
                            data = {
                                "totalAudience": grp_members + nl_subs,
                                "groupsCount": groups_count,
                                "groupMembers": grp_members,
                                "newslettersCount": nl_count,
                                "newsletterSubscribers": nl_subs,
                                "destinations": [
                                    {
                                        "id": c.get("id"),
                                        "name": c.get("name"),
                                        "type": c.get("type"),
                                        "count": c.get("participantsCount", 0),
                                    }
                                    for c in chats
                                ],
                            }
            except Exception as e:
                logger.warning(f"Failed to fetch /groups fallback: {e}")

        if not data:
            data = {
                "totalAudience": 0,
                "groupsCount": 0,
                "groupMembers": 0,
                "newslettersCount": 0,
                "newsletterSubscribers": 0,
                "destinations": [],
            }

        AUDIENCE_CACHE = {"timestamp": now, "data": data}
        return data


def build_analytics_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🔄 Refresh", callback_data="cb:ana:ref"),
        InlineKeyboardButton("📢 Top Channels", callback_data="cb:ana:top"),
    )
    kb.row(
        InlineKeyboardButton("👥 Audience Breakdown", callback_data="cb:ana:aud"),
        InlineKeyboardButton("📱 Channels Menu", callback_data="p:1"),
    )
    return kb


def build_analytics_detail_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔙 Back to Analytics Overview", callback_data="cb:ana:main"))
    return kb


@dp.message_handler(commands=["analytics", "audience", "traffic"])
async def analytics_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
    hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
    summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
    audience_stats = await fetch_audience_metadata()

    kb = build_analytics_keyboard()

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


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("cb:ana:"))
async def handle_analytics_callbacks(callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return

    action = callback_query.data[7:]
    today_str = datetime.now().strftime("%Y-%m-%d")

    if action == "ref":
        await callback_query.answer("Refreshing analytics...", show_alert=False)
        top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
        hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
        summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
        audience_stats = await fetch_audience_metadata(force_refresh=True)

        text_report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)
        try:
            await callback_query.message.edit_text(
                text_report,
                reply_markup=build_analytics_keyboard(),
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
                reply_markup=build_analytics_detail_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply("\n".join(lines), reply_markup=build_analytics_detail_keyboard(), parse_mode=ParseMode.HTML)

    elif action == "aud":
        await callback_query.answer()
        audience_stats = await fetch_audience_metadata()
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
                reply_markup=build_analytics_detail_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply("\n".join(lines), reply_markup=build_analytics_detail_keyboard(), parse_mode=ParseMode.HTML)

    elif action == "main":
        await callback_query.answer()
        top_channels = await asyncio.to_thread(get_top_active_channels, today_str, 5)
        hourly_dist = await asyncio.to_thread(get_hourly_traffic_distribution, today_str)
        summary = await asyncio.to_thread(get_channel_volume_summary, today_str)
        audience_stats = await fetch_audience_metadata()

        text_report = generate_analytics_text_report(top_channels, hourly_dist, audience_stats, summary)
        try:
            await callback_query.message.edit_text(
                text_report,
                reply_markup=build_analytics_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback_query.message.reply(text_report, reply_markup=build_analytics_keyboard(), parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["get_chat_id"])
async def get_chat_id_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
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
    data = {
        "chatName": clean_query,
        "clientId": "user",
    }

    try:
        status_code, res_data = await post_whatsapp_json("getChatId", data, timeout_sec=30)
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


@dp.message_handler(commands=["login"])
async def login_whatsapp(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    data = {"clientId": "user"}

    try:
        status_code, res_data = await post_whatsapp_json("createsession", data, timeout_sec=30)

        if status_code == 200:
            if res_data.get("ready"):
                await message.reply("✅ WhatsApp session is already authorized and ready.")
                return

            qr_data = res_data.get("qrcode")
            if qr_data:
                qr_path = generate_qr_code(qr_data)
                try:
                    with open(qr_path, "rb") as qr_fp:
                        await message.reply_photo(
                            qr_fp, caption="Scan this QR code with WhatsApp."
                        )
                finally:
                    if os.path.exists(qr_path):
                        os.remove(qr_path)
            else:
                await message.reply(f"ℹ️ {res_data.get('message')}")
        elif status_code == 202:
            await message.reply(
                "⏳ Session is initializing in the background. Please wait a few seconds and run /status or /login again."
            )
        elif status_code == 400:
            await message.reply(f"ℹ️ {res_data.get('message')}")
        else:
            await message.reply("Failed to create session.")
    except Exception as e:
        logger.error(f"Error in login_whatsapp: {e}")
        await message.reply("Error contacting WhatsApp service.")


@dp.message_handler(commands=["listen"])
async def listen_whatsapp(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    data = {"clientId": "user"}

    try:
        status_code, res_data = await post_whatsapp_json("startlistening", data, timeout_sec=30)
        if status_code in (200, 400):
            await message.reply(f"{res_data.get('message')}")
        else:
            await message.reply("Something went wrong.")
    except Exception as e:
        logger.error(f"Error in listen_whatsapp: {e}")
        await message.reply("Failed to connect to WhatsApp service.")


@dp.message_handler(commands=["logout"])
async def logout_whatsapp(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    data = {"clientId": "user"}

    try:
        status_code, res_data = await post_whatsapp_json("logout", data, timeout_sec=30)
        if status_code == 200:
            await message.reply("Logged out and session files removed successfully.")
        elif status_code == 400:
            await message.reply(f"ℹ️ {res_data.get('message')}")
        else:
            await message.reply("Failed to log out. Please try again later.")
    except Exception as e:
        logger.error(f"Error in logout_whatsapp: {e}")
        await message.reply("Failed to connect to WhatsApp service.")



@dp.message_handler(commands=["add_group"])
async def add_group_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args or len(args.split()) != 2:
        await message.reply("Usage: /add_group <channel_id> <group_id>")
        return

    channel_id, group_id = args.split()
    await asyncio.to_thread(add_group_for_channel, channel_id, group_id)
    if "@newsletter" in group_id:
        asyncio.create_task(
            post_whatsapp_json("registerNewsletters", {"newsletters": [group_id]}, timeout_sec=5)
        )
    await message.reply(
        f"✅ Group <code>{group_id}</code> mapped to channel <code>{clean_id(channel_id)}</code>.",
        parse_mode=ParseMode.HTML,
    )


@dp.message_handler(commands=["delete_group"])
async def delete_group_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
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


@dp.message_handler(commands=["delete_channel"])
async def delete_channel_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
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


# ---------- Interactive WhatsApp Group Mapper Wizard (Optimization 1.A) ----------

WHATSAPP_CHATS_CACHE: dict[str, any] = {"timestamp": 0.0, "chats": []}


async def fetch_whatsapp_chats(search_query: str = "") -> list[dict]:
    """
    Fetches participating WhatsApp groups and subscribed newsletters from the WhatsApp service.
    Caches results in memory for 15s to make inline button pagination instantaneous.
    """
    now = time.time()
    if WHATSAPP_CHATS_CACHE["chats"] and (now - WHATSAPP_CHATS_CACHE["timestamp"] < 15.0):
        chats = WHATSAPP_CHATS_CACHE["chats"]
    else:
        try:
            status_code, res_data = await post_whatsapp_json(
                "getGroups", {"clientId": "user"}, timeout_sec=10
            )
            if status_code == 200 and isinstance(res_data.get("chats"), list):
                chats = res_data["chats"]
                WHATSAPP_CHATS_CACHE["timestamp"] = now
                WHATSAPP_CHATS_CACHE["chats"] = chats
            else:
                logger.warning(f"Failed to fetch WhatsApp groups: {res_data}")
                chats = WHATSAPP_CHATS_CACHE.get("chats") or []
        except Exception as e:
            logger.error(f"Error calling getGroups: {e}")
            chats = WHATSAPP_CHATS_CACHE.get("chats") or []

    if search_query:
        sq = search_query.lower().strip()
        return [c for c in chats if sq in c.get("name", "").lower() or sq in c.get("id", "").lower()]
    return chats


def resolve_group_name(group_id: str) -> str:
    """
    Resolves human-readable WhatsApp group/newsletter name from chat cache.
    Falls back to group_id if not found.
    """
    if not group_id:
        return ""
    chats = WHATSAPP_CHATS_CACHE.get("chats") or []
    for c in chats:
        if c.get("id") == group_id and c.get("name"):
            return c.get("name")
    return group_id


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

    chats = await fetch_whatsapp_chats()
    total_chats = len(chats)
    total_pages = max(1, (total_chats + per_page - 1) // per_page)
    wizard_page = max(1, min(wizard_page, total_pages))

    start_idx = (wizard_page - 1) * per_page
    end_idx = start_idx + per_page
    page_chats = chats[start_idx:end_idx]

    keyboard = InlineKeyboardMarkup(row_width=1)

    if not page_chats:
        keyboard.add(
            InlineKeyboardButton(
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

            # Cleanly truncate long names for neat inline buttons
            display_name = (cname[:20] + "...") if len(cname) > 23 else cname

            if cid in mapped_set:
                btn_text = f"✅ {icon} {display_name} (Mapped)"
                cb_data = f"u:{channel_id}:{cid}"
            else:
                btn_text = f"➕ {icon} {display_name}"
                cb_data = f"b:{channel_id}:{cid}"

            keyboard.add(InlineKeyboardButton(text=btn_text, callback_data=cb_data))

    # Navigation row
    nav_buttons = []
    if wizard_page > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Prev",
                callback_data=f"m:p:{channel_id}:{wizard_page - 1}:{channel_page}",
            )
        )
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹️ Start", callback_data="cb:noop"))

    nav_buttons.append(
        InlineKeyboardButton(
            text=f"📄 {wizard_page}/{total_pages}",
            callback_data="cb:noop",
        )
    )

    if wizard_page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"m:p:{channel_id}:{wizard_page + 1}:{channel_page}",
            )
        )
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹️ End", callback_data="cb:noop"))

    keyboard.row(*nav_buttons)

    # Action / Back row
    keyboard.row(
        InlineKeyboardButton(
            text="🔄 Refresh",
            callback_data=f"m:p:{channel_id}:{wizard_page}:{channel_page}",
        ),
        InlineKeyboardButton(
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


# ---------- Interactive Inline Keyboards (Optimization 4.A) ----------


def build_channels_keyboard(page: int = 1, per_page: int = 6) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds a paginated inline keyboard displaying all monitored channels
    with status indicators (Active vs Paused), mapped group counts,
    and Master Emergency Kill Switch / Resume All controls.
    """
    channels = get_all_channels()
    total_channels = len(channels)
    total_pages = max(1, (total_channels + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    paused_count = sum(1 for ch in channels if is_channel_paused(ch))
    active_count = total_channels - paused_count

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = channels[start_idx:end_idx]

    keyboard = InlineKeyboardMarkup(row_width=1)

    # Master Emergency Control Row (Kill Switch / Resume All)
    if active_count > 0:
        pause_all_btn = InlineKeyboardButton(
            text=f"🚨 Emergency Pause All ({active_count})",
            callback_data=f"cb:bulk_pause:{page}",
        )
    else:
        pause_all_btn = InlineKeyboardButton(
            text="⏸️ All Channels Paused",
            callback_data="cb:noop",
        )

    if paused_count > 0:
        resume_all_btn = InlineKeyboardButton(
            text=f"▶️ Resume All ({paused_count})",
            callback_data=f"cb:bulk_resume:{page}",
        )
    else:
        resume_all_btn = InlineKeyboardButton(
            text="🟢 All Channels Active",
            callback_data="cb:noop",
        )

    keyboard.row(pause_all_btn, resume_all_btn)

    for ch in page_channels:
        paused = is_channel_paused(ch)
        groups = get_groups_for_channel(ch)
        group_count = len(groups)
        status_icon = "⏸️" if paused else "🟢"
        state_str = "Paused" if paused else f"{group_count} grp{'s' if group_count != 1 else ''}"
        btn_text = f"{status_icon} {ch} ({state_str})"
        keyboard.add(InlineKeyboardButton(text=btn_text, callback_data=f"cb:view:{ch}:{page}"))

    # Navigation row
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"cb:page:{page - 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹️ Start", callback_data="cb:noop"))

    nav_buttons.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="cb:noop"))

    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"cb:page:{page + 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text="⏹️ End", callback_data="cb:noop"))

    keyboard.row(*nav_buttons)

    # Action / Refresh row
    keyboard.row(
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"cb:page:{page}"),
        InlineKeyboardButton(text="🛡️ Health Stats", callback_data="cb:health"),
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
    details = get_channel_details(channel_id)
    paused = details["is_paused"]
    groups = details["groups"]
    last_post = details.get("last_post_at") or "None (No activity logged)"

    status_badge = "⏸️ PAUSED (Forwarding Suspended)" if paused else "🟢 ACTIVE (Forwarding Enabled)"
    toggle_btn_text = "▶️ Resume Forwarding" if paused else "⏸️ Pause Forwarding"
    toggle_action = "unpause" if paused else "pause"

    groups_text = ""
    if groups:
        for idx, g in enumerate(groups, 1):
            kind = "📢 Newsletter" if "@newsletter" in g else "👥 Group"
            groups_text += f"\n  <code>{idx}.</code> <b>{kind}:</b> <code>{g}</code>"
    else:
        groups_text = "\n  <i>No WhatsApp groups mapped yet. Tap '➕ Map WhatsApp Destination' below!</i>"

    text = (
        f"📢 <b>Channel:</b> <code>{channel_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> <b>{status_badge}</b>\n"
        f"• <b>Last Activity:</b> <code>{last_post}</code>\n"
        f"• <b>Mapped WhatsApp Destinations ({len(groups)}):</b>"
        f"{groups_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Tap 'Map WhatsApp Destination' to bind groups with one tap without typing JIDs.</i>"
    )

    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton(
            text="➕ Map WhatsApp Destination",
            callback_data=f"m:p:{channel_id}:1:{page}",
        )
    )
    keyboard.add(
        InlineKeyboardButton(
            text=toggle_btn_text,
            callback_data=f"cb:toggle:{channel_id}:{toggle_action}:{page}",
        )
    )

    if groups:
        for g in groups:
            kind = "📢" if "@newsletter" in g else "👥"
            short_g = (g[:18] + "...") if len(g) > 21 else g
            keyboard.add(
                InlineKeyboardButton(
                    text=f"❌ Unmap {kind} {short_g}",
                    callback_data=f"u:{channel_id}:{g}",
                )
            )

    keyboard.row(
        InlineKeyboardButton(
            text="🗑️ Delete Channel",
            callback_data=f"cb:del_confirm:{channel_id}:{page}",
        ),
        InlineKeyboardButton(
            text="◀️ Back to Channels",
            callback_data=f"cb:page:{page}",
        ),
    )
    return text, keyboard


def build_delete_confirm_keyboard(channel_id: str, page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds a confirmation dialog before permanently deleting a channel.
    """
    text = (
        f"⚠️ <b>Confirm Channel Deletion</b>\n\n"
        f"Are you sure you want to delete channel <code>{channel_id}</code>?\n"
        "This will permanently unregister it and remove all mapped WhatsApp groups!"
    )
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        InlineKeyboardButton(
            text="⚠️ Yes, Delete",
            callback_data=f"cb:del_exec:{channel_id}:{page}",
        ),
        InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"cb:view:{channel_id}:{page}",
        ),
    )
    return text, keyboard


@dp.message_handler(commands=["channels", "menu"])
async def channels_command(message: Message):
    """
    Primary interactive hub for browsing channels with inline buttons.
    """
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    text, kb = build_channels_keyboard(page=1)
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["map", "map_group"])
async def map_command(message: Message):
    """
    Opens the interactive One-Tap WhatsApp Group Mapper Wizard.
    If channel_id is provided (/map <channel_id>), jumps directly to chat discovery.
    Otherwise displays channel selection.
    """
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    args = message.get_args().strip() if hasattr(message, "get_args") else ""
    if args:
        channel_id = clean_id(args)
        add_channel(channel_id)
        text, kb = await build_group_mapper_keyboard(channel_id, wizard_page=1, channel_page=1)
        await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        channels = await asyncio.to_thread(get_all_channels)
        if not channels:
            await message.reply("No channels found. Use /add_channel <channel_id> first.")
            return

        kb = InlineKeyboardMarkup(row_width=1)
        for ch in channels[:8]:
            paused = is_channel_paused(ch)
            status_icon = "⏸️" if paused else "🟢"
            kb.add(
                InlineKeyboardButton(
                    text=f"{status_icon} Map Channel {ch}",
                    callback_data=f"m:p:{ch}:1:1",
                )
            )
        if len(channels) > 8:
            kb.add(
                InlineKeyboardButton(
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


@dp.callback_query_handler()
async def handle_callback_query(call: CallbackQuery):
    """
    Dispatches inline keyboard actions: pagination, details view,
    pause/resume toggles, deletion confirmation, and group mapper wizard.
    """
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    data = call.data or ""
    if data == "cb:noop":
        await call.answer()
        return

    if data == "cb:dlq_refresh":
        text, kb = build_failed_queue_keyboard()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer("Queue refreshed.")
        return

    elif data == "cb:dlq_purge":
        # Remove any preserved media in DLQ
        items = get_failed_messages(limit=500)
        for it in items:
            m_path = it.get("media_path")
            if m_path and os.path.exists(m_path):
                try:
                    os.remove(m_path)
                except Exception:
                    pass
        if os.path.exists(DLQ_MEDIA_DIR):
            for f in os.listdir(DLQ_MEDIA_DIR):
                try:
                    fp = os.path.join(DLQ_MEDIA_DIR, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass
        clear_failed_messages()
        await call.answer("🗑️ Failed queue purged!", show_alert=False)
        text, kb = build_failed_queue_keyboard()
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    elif data == "cb:dlq_retry":
        await call.answer("🔄 Re-dispatching failed messages...", show_alert=False)
        recovered, failed = await retry_all_failed_messages()
        text, kb = build_failed_queue_keyboard()
        prefix = f"🔄 <b>Retry Results</b>: {recovered} recovered, {failed} still failing.\n\n"
        try:
            await call.message.edit_text(prefix + text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # Handle wizard pagination callback (m:p:<channel_id>:<wizard_page>:<channel_page>)
    if data.startswith("m:p:"):
        parts = data.split(":")
        cid = parts[2] if len(parts) > 2 else ""
        w_page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        ch_page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 1
        text, kb = await build_group_mapper_keyboard(cid, wizard_page=w_page, channel_page=ch_page)
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
            text, kb = await build_group_mapper_keyboard(cid, wizard_page=1, channel_page=1)
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
                text, kb = await build_group_mapper_keyboard(cid, wizard_page=1, channel_page=1)
            else:
                text, kb = build_channel_detail_keyboard(cid, page=1)
            try:
                await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        text, kb = build_channels_keyboard(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await call.answer()

    elif action == "view":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = build_channel_detail_keyboard(cid, page)
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

        toast = f"⏸️ Channel {cid} paused!" if new_paused else f"▶️ Channel {cid} resumed!"
        await call.answer(toast, show_alert=False)

        text, kb = build_channel_detail_keyboard(cid, page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "del_confirm":
        cid = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = build_delete_confirm_keyboard(cid, page)
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
        text, kb = build_channels_keyboard(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "bulk_pause":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        count = set_all_channels_paused(True)
        await call.answer(f"🚨 Emergency Stop: All {count} channels have been paused!", show_alert=True)
        text, kb = build_channels_keyboard(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "bulk_resume":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        count = set_all_channels_paused(False)
        await call.answer(f"▶️ Resumed: All {count} channels are active!", show_alert=False)
        text, kb = build_channels_keyboard(page)
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif action == "health":
        stats = rate_controller.get_stats()
        text = (
            f"Account Health: {stats['status']}\n"
            f"Hourly Volume: {stats['hour_count']}/{stats['max_per_hour']}\n"
            f"Daily Volume: {stats['day_count']}/{stats['max_per_day']}\n"
            f"Active Queue: {stats['active_queue_depth']} in-flight"
        )
        await call.answer(text, show_alert=True)


@dp.message_handler(commands=["pause_all", "freeze_all"])
async def pause_all_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return
    count = await asyncio.to_thread(set_all_channels_paused, True)
    await message.reply(
        f"🚨 <b>Emergency Kill Switch Engaged</b>\n"
        f"All <code>{count}</code> monitored channels have been paused.\n"
        "Forwarding pipeline is frozen.\n\n"
        "Use /resume_all or /channels to restore message forwarding.",
        parse_mode=ParseMode.HTML,
    )


@dp.message_handler(commands=["resume_all", "unfreeze_all"])
async def resume_all_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return
    count = await asyncio.to_thread(set_all_channels_paused, False)
    await message.reply(
        f"▶️ <b>Forwarding Resumed</b>\n"
        f"All <code>{count}</code> channels are now active and broadcasting.",
        parse_mode=ParseMode.HTML,
    )


# ---------- Dead-Letter Queue (DLQ) & Failed Delivery Manager (Optimization 3.A) ----------


def build_failed_queue_keyboard(limit: int = 20) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the interactive Dead-Letter Queue (DLQ) inspector UI and inline controls.
    Displays failed deliveries in the requested format:
    📬 Failed Messages Queue ({count} items)
    ━━━━━━━━━━━━━━━━━━━━━━
    1. Video (105MB) -> Crypto VIP (Size limit)
    2. Text -> Forex Signals (Connection timeout)
    3. Photo -> News Hub (WhatsApp rate limit)
    ━━━━━━━━━━━━━━━━━━━━━━
    [ 🔄 Retry All Failed ]  [ 🗑️ Purge Queue ]
    """
    messages = get_failed_messages(limit=limit)
    count = get_failed_messages_count()

    keyboard = InlineKeyboardMarkup(row_width=2)

    item_noun = "item" if count == 1 else "items"
    header = f"📬 <b>Failed Messages Queue ({count} {item_noun})</b>\n━━━━━━━━━━━━━━━━━━━━━━"

    if not messages:
        body = "\n✨ <i>Dead-Letter Queue is empty! All deliveries are healthy.</i>\n━━━━━━━━━━━━━━━━━━━━━━"
        keyboard.add(
            InlineKeyboardButton(text="🔄 Refresh", callback_data="cb:dlq_refresh")
        )
        return f"{header}{body}", keyboard

    lines = []
    for idx, item in enumerate(messages, start=1):
        content_type = item.get("content_type", "text").capitalize()

        # Format size if present and >= 1MB
        size_bytes = item.get("file_size_bytes") or 0
        size_str = ""
        if size_bytes >= 1024 * 1024:
            size_mb = round(size_bytes / (1024 * 1024))
            size_str = f" ({size_mb}MB)"

        # Format target name
        target_name = item.get("group_name") or resolve_group_name(item.get("group_id"))
        if len(target_name) > 25:
            target_name = target_name[:22] + "..."

        reason = item.get("reason") or "Delivery failed"
        lines.append(f"{idx}. {content_type}{size_str} -> {target_name} ({reason})")

    footer = "━━━━━━━━━━━━━━━━━━━━━━"
    if count > len(messages):
        footer = f"<i>...and {count - len(messages)} more items</i>\n━━━━━━━━━━━━━━━━━━━━━━"

    content = f"{header}\n" + "\n".join(lines) + f"\n{footer}"

    keyboard.row(
        InlineKeyboardButton(text="🔄 Retry All Failed", callback_data="cb:dlq_retry"),
        InlineKeyboardButton(text="🗑️ Purge Queue", callback_data="cb:dlq_purge"),
    )
    keyboard.add(
        InlineKeyboardButton(text="🔄 Refresh", callback_data="cb:dlq_refresh")
    )

    return content, keyboard


async def retry_all_failed_messages() -> tuple[int, int]:
    """
    Re-dispatches queued failed messages through the token-bucket queue via send_to_single_group.
    Prunes successfully sent items and unlinks their temporary DLQ media files.
    Returns (recovered_count, still_failed_count).
    """
    items = get_failed_messages(limit=50)
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

        # Optimization / Policy: Skip videos if video forwarding is banned
        if getattr(config, "BAN_VIDEO_FORWARDING", True) and content_type == "video":
            logger.info(
                f"🚫 [Video Ban] Skipping DLQ retry for video item {item_id} (video forwarding is banned)."
            )
            continue

        # If it was a size limit rejection or media was not preserved, we cannot re-send media without the file

        if content_type != "text" and (not media_path or not os.path.exists(media_path)):
            logger.warning(
                f"DLQ item {item_id} ({content_type}) cannot be retried: media file not available."
            )
            failed += 1
            continue

        try:
            success = await send_to_single_group(
                group=group,
                downloaded_media=media_path,
                caption=caption,
                original_filename=original_filename,
                content_type=content_type,
                channel_id=item.get("channel_id"),
                is_retry=True,
            )
            if success:
                delete_failed_message(item_id)
                recovered += 1
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except Exception as e:
                        logger.error(f"Failed to remove DLQ media {media_path}: {e}")
            else:
                failed += 1
        except Exception as e:
            logger.error(f"Error retrying DLQ item {item_id}: {e}")
            failed += 1

    return recovered, failed


@dp.message_handler(commands=["failed", "dlq"])
async def failed_queue_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    text, kb = build_failed_queue_keyboard()
    await message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["view_groups"])
async def view_groups_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    channels = await asyncio.to_thread(get_all_channels)
    if not channels:
        await message.reply("No channels found.")
        return

    btn_kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton(text="📱 Open Interactive Channel Browser", callback_data="cb:page:1")
    )

    reply = "<b>Channels and WhatsApp Mappings:</b>\n\n"
    for ch in channels:
        groups = await asyncio.to_thread(get_groups_for_channel, ch)
        group_list = ", ".join(f"<code>{g}</code>" for g in groups) if groups else "<i>None</i>"
        reply += f"📢 Channel: <code>{ch}</code>\n🔗 Groups: {group_list}\n\n"

    # Split reply if exceeding Telegram 4096 char limit
    if len(reply) > 4000:
        chunks = [reply[i : i + 4000] for i in range(0, len(reply), 4000)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await message.reply(chunk, reply_markup=btn_kb, parse_mode=ParseMode.HTML)
            else:
                await message.reply(chunk, parse_mode=ParseMode.HTML)
    else:
        await message.reply(reply, reply_markup=btn_kb, parse_mode=ParseMode.HTML)


@dp.message_handler(commands=["add_channel"])
async def add_channel_command(message: Message):
    if not message.chat.type == "private" or not is_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        await message.reply("Usage: /add_channel <channel_id>")
        return

    channel_id = args.strip()
    await asyncio.to_thread(add_channel, channel_id)
    await message.reply(
        f"✅ Channel <code>{clean_id(channel_id)}</code> registered successfully.",
        parse_mode=ParseMode.HTML,
    )


# ---------- Entity & Caption Formatting (Fix 2.1 & 3.5) ----------


def format_caption_for_whatsapp(caption: str, entities: list[MessageEntity]) -> str:
    """
    Translates Telegram MessageEntity offset formatting to WhatsApp markdown.
    - Preserves emoji and surrogate pair indexing via UTF-16 code units.
    - Supports bold, italic, strikethrough, monospace, blockquotes, and text links.
    - Places URL links OUTSIDE formatting delimiters to prevent underscore italic collisions (Fix 3.5).
    - Uses LIFO suffix insertion for symmetric nesting.
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

    prefix_insertions: dict[int, list[str]] = {}
    suffix_insertions: dict[int, list[str]] = {}
    link_insertions: dict[int, list[str]] = {}

    for ent in entities:
        start = utf16_to_str_idx.get(ent.offset)
        end = utf16_to_str_idx.get(ent.offset + ent.length)

        if start is None or end is None or start > end:
            continue

        prefix = ""
        suffix = ""

        if ent.type == "bold":
            prefix, suffix = "*", "*"
        elif ent.type == "italic":
            prefix, suffix = "_", "_"
        elif ent.type == "strikethrough":
            prefix, suffix = "~", "~"
        elif ent.type == "code":
            prefix, suffix = "`", "`"
        elif ent.type == "pre":
            prefix, suffix = "```\n", "\n```"
        elif ent.type == "spoiler":
            prefix, suffix = "||", "||"
        elif ent.type in ("blockquote", "expandable_blockquote"):
            prefix = "> "
            # Multiline quotes: prefix each line
            for k in range(start, min(end, len(caption))):
                if caption[k] == "\n" and k + 1 < end:
                    prefix_insertions.setdefault(k + 1, []).append("> ")
        elif ent.type == "text_link" and getattr(ent, "url", None):
            # Place link URL outside of formatting delimiters (Fix 3.5)
            link_insertions.setdefault(end, []).append(f" ({ent.url})")
            continue
        else:
            continue

        if prefix:
            prefix_insertions.setdefault(start, []).append(prefix)
        if suffix:
            # LIFO order
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

    return "".join(result)


# ---------- Message Delivery Engine (Fix 3.2) ----------


async def check_and_alert_health():
    """
    Checks rate controller health and alerts admins if 80% warning
    or 100% critical safety threshold is exceeded.
    """
    status, alert_msg = rate_controller.check_health()
    if alert_msg:
        for admin_id in config.admin_ids:
            try:
                await bot.send_message(admin_id, alert_msg, parse_mode=ParseMode.HTML)
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
) -> bool:
    """
    Sends message to a single WhatsApp group/newsletter with:
    - Queue-depth tracking & Adaptive Humanized Pacing (Optimization 2.A)
    - Per-recipient Token Bucket enforcement (Optimization 2.A)
    - Sliding-window volume tracking & Admin Health Alerts (Optimization 2.A)
    - Daily delivery metrics & latency tracking (Optimization 4.B)
    - Dead-Letter Queue (DLQ) automated capture & media preservation (Optimization 3.A)
    - Concurrency bounding via UPLOAD_SEMAPHORE
    - Exponential backoff retry on transient 5xx/connection drops
    - Persistent aiohttp connection pooling
    - Detailed error diagnostic logging
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

    rate_controller.enter_queue()

    try:
        async with UPLOAD_SEMAPHORE:
            # 1. Per-recipient Token Bucket rate limiting
            bucket = rate_controller.get_bucket(group)
            waited = await bucket.acquire()
            if waited > 0:
                logger.debug(f"Token bucket waited {waited:.2f}s for {group}")

            # 2. Dynamic Queue-depth aware humanized pacing with Gaussian jitter
            pacing_delay = rate_controller.calculate_pacing_delay(group)
            await asyncio.sleep(pacing_delay)

            session = await get_http_session()
            max_retries = 2
            success = False
            last_latency_ms = 0.0
            last_err_msg = ""
            last_err_detail = ""
            last_exception: Exception | None = None
            status_code = 500

            for attempt in range(max_retries + 1):
                t_start = time.time()
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
                                group_name=resolve_group_name(group),
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
                                data.add_field("clientId", "user")
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
                                    data = {"clientId": "user", "groupId": group, "caption": caption or ""}
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
                            record_delivery_metric(
                                content_type=content_type,
                                latency_ms=last_latency_ms,
                                retried=(attempt > 0 or is_retry),
                                failed=False,
                            )
                            logger.info(
                                f"Media message sent to group {group} successfully."
                            )
                            rate_controller.record_sent()
                            await check_and_alert_health()
                            break
                        elif status_code in (502, 503, 504) and attempt < max_retries:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.warning(
                                f"WhatsApp service returned {status_code} for {group}. Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        else:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.error(
                                f"Failed to send media message to group {group}: {msg} ({err_detail})"
                            )
                            if "Session is not authorized" in str(msg):
                                await notify_admins_auth_required()
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
                            f"Failed to send media message to group {group}: {e}"
                        )
                        break

                else:
                    data = {"clientId": "user", "groupId": group, "text": caption}
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
                            record_delivery_metric(
                                content_type=content_type,
                                latency_ms=last_latency_ms,
                                retried=(attempt > 0 or is_retry),
                                failed=False,
                            )
                            logger.info(
                                f"Text message sent to group {group} successfully."
                            )
                            rate_controller.record_sent()
                            await check_and_alert_health()
                            break
                        elif status_code in (502, 503, 504) and attempt < max_retries:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.warning(
                                f"WhatsApp service returned {status_code} for {group}. Retrying in {(attempt+1)*2}s (attempt {attempt+1}/{max_retries})..."
                            )
                            await asyncio.sleep((attempt + 1) * 2)
                            continue
                        else:
                            last_err_msg = msg
                            last_err_detail = err_detail
                            logger.error(
                                f"Failed to send text message to group {group}: {msg} ({err_detail})"
                            )
                            if "Session is not authorized" in str(msg):
                                await notify_admins_auth_required()
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
                # Optimization 3.A: Route to Dead-Letter Queue (DLQ) if not already a retry
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
                        dlq_media_path = os.path.join(DLQ_MEDIA_DIR, dlq_fname)
                        try:
                            shutil.copy2(downloaded_media, dlq_media_path)
                        except Exception as copy_err:
                            logger.error(f"Failed to preserve DLQ media: {copy_err}")
                            dlq_media_path = None

                    group_name = resolve_group_name(group)
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
        rate_controller.exit_queue()




# ---------- Video Forwarding Policy (Banning All Video Messages) ----------

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


def is_video_message(message: types.Message) -> bool:
    """
    Checks if a Telegram message is a video or has video attached.
    Identifies:
    1. Standalone video messages (ContentType.VIDEO without caption)
    2. Video messages with text/caption attached (ContentType.VIDEO with caption)
    3. Video round notes (ContentType.VIDEO_NOTE)
    4. Video animations / GIFs (ContentType.ANIMATION)
    5. Document messages containing video files (video/* MIME or video file extension)
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


# ---------- Channel Post Handler (Fix 2.1, 2.4, 2.6, 3.3) ----------


@dp.channel_post_handler(
    content_types=[
        ContentType.TEXT,
        ContentType.PHOTO,
        ContentType.VIDEO,
        getattr(ContentType, "VIDEO_NOTE", "video_note"),
        getattr(ContentType, "ANIMATION", "animation"),
        ContentType.DOCUMENT,
        ContentType.VOICE,
        ContentType.AUDIO,
    ]
)
async def handle_channel_post(message: types.Message):
    channel_id = clean_id(message.chat.id)

    # Ban video forwarding: if any message has video attached or is pure video, do not forward
    if getattr(config, "BAN_VIDEO_FORWARDING", True) and is_video_message(message):
        logger.info(
            f"🚫 [Video Ban] Message {getattr(message, 'message_id', 'unknown')} in channel {channel_id} contains video (type={getattr(message, 'content_type', 'unknown')}). Forwarding is banned. Dropping post."
        )
        return

    # Record channel activity timestamp (Optimization 3.B: Stale Channel Detector)
    await asyncio.to_thread(update_channel_last_post, channel_id)


    # Record channel hourly post volume (Optimization 6.A: Audience & Channel Intelligence)
    await asyncio.to_thread(record_channel_post_activity, channel_id)

    # Check if forwarding is paused for this channel (Optimization 4.A)
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
    }
    content_type_str = content_type_map.get(message.content_type, "text")

    try:
        # 1. Process Text Messages (Fix 2.1: Use message.entities for text!)
        if message.content_type == ContentType.TEXT:
            raw_text = message.text or ""
            entities = message.entities or []
            caption = format_caption_for_whatsapp(raw_text, entities) if raw_text else ""

        # 2. Process Photos
        elif message.content_type == ContentType.PHOTO:
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
            photo_path = os.path.join(MEDIA_DIR, f"{photo.file_unique_id}.jpg")
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

        # 3. Process Videos (Banned when BAN_VIDEO_FORWARDING is True)
        elif message.content_type == ContentType.VIDEO:
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

            # Pre-check video file size before downloading (Fix 2.4)
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
                        group_name=resolve_group_name(group),
                        content_type="video",
                        caption=caption,
                        original_filename=fname,
                        media_path=None,
                        reason="Size limit",
                        file_size_bytes=video_size,
                    )
                for admin_id in config.admin_ids:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"⚠️ <b>Video Skipped</b>: A video in channel <code>{channel_id}</code> ({video_size / (1024*1024):.1f} MB) exceeded the 100 MB limit.",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                return

            video_file_path = os.path.join(
                MEDIA_DIR, f"{message.video.file_unique_id}.mp4"
            )
            original_filename = (
                getattr(message.video, "file_name", None)
                or f"{message.video.file_unique_id}.mp4"
            )

            try:
                if video_size <= 20 * 1024 * 1024:
                    await message.video.download(destination_file=video_file_path)
                elif bot_client:
                    video_message = await bot_client.get_messages(
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

        # 4. Process Documents with Pre-Download Size Guard (Fix 2.4)
        elif message.content_type == ContentType.DOCUMENT:
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

            doc = message.document
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
                        group_name=resolve_group_name(group),
                        content_type="doc",
                        caption=caption,
                        original_filename=fname,
                        media_path=None,
                        reason="Size limit",
                        file_size_bytes=doc_size,
                    )
                for admin_id in config.admin_ids:
                    try:
                        await bot.send_message(
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
                MEDIA_DIR, f"{doc.file_unique_id}_{safe_name}"
            )

            try:
                if doc_size <= 20 * 1024 * 1024:
                    await doc.download(destination_file=doc_file_path)
                elif bot_client:
                    doc_msg = await bot_client.get_messages(
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
        elif message.content_type in (ContentType.VOICE, ContentType.AUDIO):
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
                        group_name=resolve_group_name(group),
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
                MEDIA_DIR, f"{media_obj.file_unique_id}_{safe_name}"
            )

            try:
                if audio_size <= 20 * 1024 * 1024:
                    await media_obj.download(destination_file=audio_file_path)
                elif bot_client:
                    audio_msg = await bot_client.get_messages(
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

        # Parallel fan-out bounded by UPLOAD_SEMAPHORE
        await asyncio.gather(
            *(
                send_to_single_group(
                    group,
                    downloaded_media,
                    caption,
                    original_filename,
                    content_type=content_type_str,
                    channel_id=channel_id,
                )
                for group in groups
            )
        )

    except Exception as e:
        logger.error(f"Unexpected error handling channel post: {e}", exc_info=True)
    finally:
        # Always clean up downloaded temporary media (Fix 2.6)
        if downloaded_media and os.path.exists(downloaded_media):
            try:
                os.remove(downloaded_media)
            except Exception as e:
                logger.error(f"Failed to remove {downloaded_media}: {e}")


# ---------- Session Watchdog (/watchdog) ----------

WATCHDOG_CHECK_INTERVAL = 5 * 60        # ping /health every 5 minutes
WATCHDOG_FAILURE_THRESHOLD = 3          # raise alert after this many consecutive failures
WATCHDOG_RECONNECT_COOLDOWN = 10 * 60   # minimum seconds between /createsession calls


async def _get_whatsapp_health() -> bool:
    """
    Pings the WhatsApp service /health endpoint.
    Returns True if the session is ready, False on any failure or not-ready status.
    Uses the liveness probe flag (?live=1 always 200) intentionally NOT set here —
    we want the real session-ready status, not just HTTP server liveness.
    """
    try:
        session = await get_http_session()
        url = f"{config.whatsapp_service}/health"
        if session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as res:
                if res.status == 200:
                    data = await res.json()
                    return data.get("status") == "ready"
                return False
        else:
            def _sync_get():
                return requests.get(url, headers=get_http_headers(), timeout=10)
            r = await asyncio.to_thread(_sync_get)
            return r.status_code == 200 and r.json().get("status") == "ready"
    except Exception as e:
        logger.debug(f"[Watchdog] /health check failed: {e}")
        return False


async def _watchdog_attempt_reconnect():
    """
    Attempts to trigger Baileys /createsession to auto-reconnect the WhatsApp socket.
    Respects a cooldown to avoid hammering the service.
    """
    global _watchdog_last_reconnect_at
    if _watchdog_reconnect_lock.locked():
        logger.debug("[Watchdog] Reconnect already in progress, skipping.")
        return

    now = time.time()
    if now - _watchdog_last_reconnect_at < WATCHDOG_RECONNECT_COOLDOWN:
        remaining = int(WATCHDOG_RECONNECT_COOLDOWN - (now - _watchdog_last_reconnect_at))
        logger.debug(f"[Watchdog] Reconnect cooldown active, {remaining}s remaining.")
        return

    async with _watchdog_reconnect_lock:
        _watchdog_last_reconnect_at = time.time()
        logger.info("[Watchdog] Triggering /createsession to auto-reconnect WhatsApp socket...")
        try:
            status, data = await post_whatsapp_json("createsession", {"clientId": "user"}, timeout_sec=30)
            if status == 200 and data.get("ready"):
                logger.info("[Watchdog] /createsession reported session is already ready.")
            elif data.get("qrcode"):
                logger.warning("[Watchdog] /createsession returned a QR code — manual login required.")
            elif status == 202:
                logger.info("[Watchdog] /createsession started session initialization in background.")
            else:
                logger.warning(f"[Watchdog] /createsession responded {status}: {data}")
        except Exception as e:
            logger.error(f"[Watchdog] Failed to call /createsession: {e}")


async def scheduled_session_watchdog_loop():
    """
    Background watchdog that monitors the WhatsApp/Baileys session health every 5 minutes.

    Behaviour:
    - Pings /health (real session-ready status, not liveness probe) every 5 min.
    - Tracks consecutive not-ready results.
    - On >= 3 consecutive failures:
        1. Alerts admins via notify_admins_auth_required().
        2. Calls /createsession to attempt automatic reconnect (with 10-min cooldown).
    - On recovery:
        - Logs the outage duration.
        - Sends admins a recovery confirmation message.
    - Resets counters on recovery.

    Initial warm-up: waits 90 seconds after startup to allow Baileys to fully initialize.
    """
    global _watchdog_consecutive_failures, _watchdog_outage_start

    await asyncio.sleep(90)  # give Baileys time to initialize before first check
    logger.info("[Watchdog] Session watchdog started. Checking every 5 minutes.")

    while True:
        try:
            is_ready = await _get_whatsapp_health()

            if is_ready:
                if _watchdog_consecutive_failures > 0:
                    # ---- RECOVERY ----
                    outage_secs = (
                        int(time.time() - _watchdog_outage_start)
                        if _watchdog_outage_start
                        else 0
                    )
                    outage_str = (
                        f"{outage_secs // 60}m {outage_secs % 60}s"
                        if outage_secs >= 60
                        else f"{outage_secs}s"
                    )
                    logger.info(
                        f"[Watchdog] ✅ WhatsApp session recovered after {outage_str} "
                        f"({_watchdog_consecutive_failures} failed checks)."
                    )
                    for admin_id in config.admin_ids:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"✅ <b>WhatsApp Session Recovered</b>\n\n"
                                f"The Baileys socket reconnected successfully.\n"
                                f"⏱ Outage duration: <b>{outage_str}</b>\n"
                                f"🔄 Failed health checks: {_watchdog_consecutive_failures}",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error(f"[Watchdog] Failed to send recovery alert to admin {admin_id}: {e}")

                    _watchdog_consecutive_failures = 0
                    _watchdog_outage_start = None
                else:
                    logger.debug("[Watchdog] /health OK — session is ready.")

            else:
                # ---- FAILURE ----
                _watchdog_consecutive_failures += 1
                if _watchdog_outage_start is None:
                    _watchdog_outage_start = time.time()

                logger.warning(
                    f"[Watchdog] WhatsApp session NOT ready "
                    f"(consecutive failures: {_watchdog_consecutive_failures}/{WATCHDOG_FAILURE_THRESHOLD})."
                )

                if _watchdog_consecutive_failures >= WATCHDOG_FAILURE_THRESHOLD:
                    outage_secs = int(time.time() - _watchdog_outage_start)
                    outage_str = (
                        f"{outage_secs // 60}m {outage_secs % 60}s"
                        if outage_secs >= 60
                        else f"{outage_secs}s"
                    )
                    logger.error(
                        f"[Watchdog] 🚨 {_watchdog_consecutive_failures} consecutive failures "
                        f"({outage_str} outage). Alerting admins and attempting reconnect."
                    )

                    # 1. Alert admins
                    await notify_admins_auth_required()

                    # Send a detailed watchdog-specific alert (different from generic auth alert)
                    for admin_id in config.admin_ids:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🚨 <b>WhatsApp Session Watchdog Alert</b>\n\n"
                                f"The Baileys socket has been <b>unreachable for {outage_str}</b> "
                                f"({_watchdog_consecutive_failures} consecutive health check failures).\n\n"
                                f"🔄 Attempting automatic reconnection...\n"
                                f"If reconnection fails, please use /login to re-authenticate.",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            logger.error(f"[Watchdog] Failed to send alert to admin {admin_id}: {e}")

                    # 2. Attempt auto-reconnect
                    await _watchdog_attempt_reconnect()

        except asyncio.CancelledError:
            logger.info("[Watchdog] Session watchdog loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[Watchdog] Unexpected error in watchdog loop: {e}", exc_info=True)

        await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)


@dp.message_handler(commands=["watchdog"])
async def cmd_watchdog(message: Message):
    """
    /watchdog — shows the current session watchdog status.
    Displays consecutive failure count, outage duration, and last reconnect attempt time.
    """
    if not is_admin(message.from_user.id):
        return

    is_ready = await _get_whatsapp_health()

    if is_ready:
        session_status = "✅ <b>Ready</b>"
    elif _watchdog_consecutive_failures > 0:
        session_status = f"🔴 <b>Not Ready</b> ({_watchdog_consecutive_failures} consecutive failures)"
    else:
        session_status = "🟡 <b>Checking...</b>"

    outage_info = ""
    if _watchdog_outage_start and _watchdog_consecutive_failures > 0:
        outage_secs = int(time.time() - _watchdog_outage_start)
        outage_str = (
            f"{outage_secs // 60}m {outage_secs % 60}s"
            if outage_secs >= 60
            else f"{outage_secs}s"
        )
        outage_info = f"\n⏱ <b>Outage Duration:</b> {outage_str}"

    last_reconnect = ""
    if _watchdog_last_reconnect_at > 0:
        ago = int(time.time() - _watchdog_last_reconnect_at)
        ago_str = f"{ago // 60}m {ago % 60}s ago" if ago >= 60 else f"{ago}s ago"
        last_reconnect = f"\n🔄 <b>Last Reconnect Attempt:</b> {ago_str}"

    text = (
        f"🩺 <b>Session Watchdog Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Session:</b> {session_status}{outage_info}{last_reconnect}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Check Interval:</b> {WATCHDOG_CHECK_INTERVAL // 60} min\n"
        f"⚠️ <b>Alert Threshold:</b> {WATCHDOG_FAILURE_THRESHOLD} consecutive failures\n"
        f"🔁 <b>Reconnect Cooldown:</b> {WATCHDOG_RECONNECT_COOLDOWN // 60} min"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ---------- Lifecycle Hooks ----------


async def scheduled_daily_report_loop():
    """
    Background task that sleeps until midnight every day, compiles
    yesterday's delivery metrics report, and broadcasts it to all admins.
    """
    while True:
        try:
            now = datetime.now()
            # Target next midnight + 5 seconds to guarantee date rollover
            tomorrow_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_duration = (tomorrow_midnight - now).total_seconds()
            logger.info(
                f"Daily report scheduler sleeping for {sleep_duration:.1f}s until midnight ({tomorrow_midnight.strftime('%Y-%m-%d %H:%M:%S')})."
            )
            await asyncio.sleep(sleep_duration)

            # Generate report for yesterday
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            report_text = generate_daily_report_text(yesterday_str)

            for admin_id in config.admin_ids:
                try:
                    await bot.send_message(admin_id, report_text)
                except Exception as e:
                    logger.error(
                        f"Failed to dispatch scheduled daily report to admin {admin_id}: {e}"
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in daily report scheduler loop: {e}")
            await asyncio.sleep(60)


async def scheduled_stale_channel_detector_loop():
    """
    Background cron that periodically inspects last post timestamps across all monitored channels.
    If any channel has had zero activity for > 72 hours, dispatches a gentle digest to admins.
    Runs once every 24 hours (with an initial 60-second warm-up delay).
    """
    await asyncio.sleep(60)
    while True:
        try:
            stale_channels = await asyncio.to_thread(get_stale_channels, 72)
            if stale_channels:
                digest = generate_stale_channels_digest(stale_channels, threshold_hours=72)
                for admin_id in config.admin_ids:
                    try:
                        await bot.send_message(admin_id, digest, parse_mode=ParseMode.HTML)
                    except Exception as e:
                        logger.error(
                            f"Failed to dispatch stale channel alert to admin {admin_id}: {e}"
                        )
            # Proactive digest check every 24 hours
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in stale channel detector loop: {e}")
            await asyncio.sleep(300)


async def periodic_cleanup_task():
    """Periodically purges media directory every 15 minutes."""
    while True:
        try:
            await asyncio.sleep(15 * 60)
            await asyncio.to_thread(cleanup_stale_media)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup task: {e}")


async def sync_database_newsletters_to_whatsapp():
    """
    Scans all mapped groups in SQLite database and registers any newsletters/groups
    with the WhatsApp microservice so they are instantly discoverable via /get_chat_id and /map.
    """
    try:
        channels = await asyncio.to_thread(get_all_channels)
        all_jids = set()
        for ch in channels:
            groups = await asyncio.to_thread(get_groups_for_channel, ch)
            for g in groups:
                if g and ("@newsletter" in g or "@g.us" in g or len(g) > 15):
                    all_jids.add(g)
        if all_jids:
            logger.info(f"Syncing {len(all_jids)} database destinations to WhatsApp service...")
            await post_whatsapp_json("registerNewsletters", {"newsletters": list(all_jids)}, timeout_sec=15)
    except Exception as e:
        logger.debug(f"sync_database_newsletters_to_whatsapp notice: {e}")


async def on_startup(_):
    logger.info("Bot starting up...")
    cleanup_stale_media()
    asyncio.create_task(init_telethon_client())
    asyncio.create_task(periodic_cleanup_task())
    asyncio.create_task(scheduled_daily_report_loop())
    asyncio.create_task(scheduled_stale_channel_detector_loop())
    asyncio.create_task(scheduled_session_watchdog_loop())
    asyncio.create_task(sync_database_newsletters_to_whatsapp())
    print("Bot is up and operational.")


async def on_shutdown(_):
    logger.info("Bot shutting down...")
    global http_session, bot_client
    if http_session and not http_session.closed:
        await http_session.close()
    if bot_client and bot_client.is_connected():
        await bot_client.disconnect()


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )

