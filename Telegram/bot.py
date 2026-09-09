import os
import sys
import inspect
import asyncio
import time

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

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
from telethon import TelegramClient

import config
from logger import logger
from database import *
from rate_limiter import DeliveryRateController
from analytics_card import generate_analytics_infographic, generate_analytics_text_report
from auto_healer import AutoHealer
from account_pool import (
    AccountPool,
    CONFIGURED_ACCOUNTS,
    ID_TO_INDEX,
    ACCOUNT_INDICES,
    ACCOUNT_LABELS,
)

# ---------- Core Bot & Shared State Initialization ----------
bot = aiogram.Bot(config.bot_token)
dp = aiogram.Dispatcher(bot)

auto_healer = AutoHealer()
account_pool = AccountPool()

bot_client: TelegramClient | None = None
http_session: aiohttp.ClientSession | None = None

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
DLQ_MEDIA_DIR = os.path.join(MEDIA_DIR, "dlq")
os.makedirs(DLQ_MEDIA_DIR, exist_ok=True)

UPLOAD_SEMAPHORE = asyncio.Semaphore(3)

rate_controller = DeliveryRateController(
    max_per_hour=config.MAX_MSGS_PER_HOUR,
    max_per_day=config.MAX_MSGS_PER_DAY,
    alert_threshold_percent=config.ALERT_THRESHOLD_PERCENT,
    per_recipient_rate=config.PER_RECIPIENT_RATE,
    per_recipient_burst=config.PER_RECIPIENT_BURST,
    account_ids=CONFIGURED_ACCOUNTS,
)

last_auth_alert_time = 0.0

ALBUM_LOCK = asyncio.Lock()
PROCESSED_MEDIA_GROUPS: dict[str, dict] = {}

# ---------- Session Watchdog State ----------
_watchdog_consecutive_failures: int = 0
_watchdog_outage_start: float | None = None
_watchdog_reconnect_lock = asyncio.Lock()
_watchdog_last_reconnect_at: float = 0.0

WATCHDOG_CHECK_INTERVAL = 5 * 60
WATCHDOG_FAILURE_THRESHOLD = 3
WATCHDOG_RECONNECT_COOLDOWN = 10 * 60

# ---------- Re-export Modular Services, Tasks, and Handlers ----------
from services.whatsapp import (
    get_http_headers,
    get_http_session,
    close_http_session,
    post_whatsapp_json,
    _fetch_baileys_ram_mb,
    generate_qr_code,
    notify_admins_auth_required,
    fetch_audience_metadata,
    fetch_whatsapp_chats,
    resolve_group_name,
    _sync_account_pool_now,
    sync_database_newsletters_to_whatsapp,
    AUDIENCE_CACHE,
    AUDIENCE_CACHE_LOCK,
    WHATSAPP_CHATS_CACHE,
)

from services.forwarder import (
    cleanup_stale_media,
    init_telethon_client,
    should_forward_caption,
    adapt_image_for_whatsapp_channel,
    prepare_image_for_whatsapp_channel,
    format_caption_for_whatsapp,
    check_and_alert_health,
    send_to_single_group,
    is_video_message,
    VIDEO_EXTENSIONS,
    is_audio_message,
    AUDIO_EXTENSIONS,
    handle_channel_post,
    handle_edited_channel_post,
    delete_forwarded_post,
    register_forwarder_handlers,
)

from tasks.watchdog import (
    _get_whatsapp_health,
    _get_all_sessions_status,
    _watchdog_attempt_reconnect,
    scheduled_session_watchdog_loop,
    send_instant_logout_alert,
    scheduled_instant_disconnect_watchdog_loop,
    cmd_watchdog,
    register_watchdog_handlers,
    _watchdog_account_failures,
    _watchdog_account_outage_start,
    _watchdog_account_reconnect_at,
)

from tasks.healer import (
    scheduled_auto_healer_loop,
)

from tasks.scheduler import (
    generate_daily_report_text,
    generate_stale_channels_digest,
    scheduled_daily_report_loop,
    scheduled_stale_channel_detector_loop,
    periodic_cleanup_task,
    scheduled_account_pool_sync_loop,
)

from handlers.admin import (
    is_admin,
    start_command,
    help_command,
    get_server_telemetry,
    status_command,
    telemetry_command,
    build_healer_keyboard,
    healer_command,
    handle_healer_callbacks,
    health_stats_command,
    daily_report_command,
    stale_channels_command,
    build_analytics_keyboard,
    build_analytics_detail_keyboard,
    analytics_command,
    handle_analytics_callbacks,
    register_admin_handlers,
)

from handlers.channels import (
    build_channels_keyboard,
    build_channel_detail_keyboard,
    build_delete_confirm_keyboard,
    build_unlink_confirm_keyboard,
    channels_command,
    pause_all_command,
    resume_all_command,
    deactivate_channel_command,
    activate_channel_command,
    unlink_command,
    add_channel_command,
    delete_channel_command,
    handle_channels_callbacks,
    register_channels_handlers,
)

from handlers.groups import (
    get_chat_id_command,
    add_group_command,
    delete_group_command,
    view_groups_command,
    build_group_mapper_keyboard,
    map_command,
    handle_group_mapper_callbacks,
    register_groups_handlers,
)

from handlers.accounts import (
    build_accounts_keyboard,
    build_account_select_keyboard,
    _perform_login_for_account,
    _perform_logout_for_account,
    accounts_command,
    account_pool_callback_handler,
    proxy_command,
    login_whatsapp,
    logout_whatsapp,
    listen_whatsapp,
    register_accounts_handlers,
)

from handlers.dlq import (
    build_failed_queue_keyboard,
    retry_all_failed_messages,
    failed_queue_command,
    handle_dlq_callbacks,
    register_dlq_handlers,
)


# ---------- Callback Query Router Facade ----------
async def handle_callback_query(call: CallbackQuery):
    """
    Central callback dispatcher delegating to specialized handler modules.
    """
    data = call.data or ""
    if data.startswith("cb:hlr:"):
        return await handle_healer_callbacks(call)
    if data.startswith("cb:ana:"):
        return await handle_analytics_callbacks(call)
    if data.startswith("cb:acc:"):
        return await account_pool_callback_handler(call)
    if data in ("cb:dlq_refresh", "cb:dlq_purge", "cb:dlq_retry"):
        return await handle_dlq_callbacks(call)
    if data.startswith("m:p:") or data.startswith("b:") or data.startswith("u:"):
        return await handle_group_mapper_callbacks(call)
    return await handle_channels_callbacks(call)


# ---------- Register All Handlers with Dispatcher ----------
register_admin_handlers(dp)
register_channels_handlers(dp)
register_groups_handlers(dp)
register_accounts_handlers(dp)
register_dlq_handlers(dp)
register_watchdog_handlers(dp)
register_forwarder_handlers(dp)


# ---------- Lifecycle Hooks ----------
async def on_startup(_):
    logger.info("Bot starting up...")
    cleanup_stale_media()
    asyncio.create_task(init_telethon_client())
    asyncio.create_task(periodic_cleanup_task())
    asyncio.create_task(scheduled_daily_report_loop())
    asyncio.create_task(scheduled_stale_channel_detector_loop())
    asyncio.create_task(scheduled_session_watchdog_loop())
    asyncio.create_task(scheduled_instant_disconnect_watchdog_loop())
    asyncio.create_task(scheduled_auto_healer_loop())
    asyncio.create_task(scheduled_account_pool_sync_loop())
    asyncio.create_task(sync_database_newsletters_to_whatsapp())
    print("Bot is up and operational.")


async def on_shutdown(_):
    logger.info("Bot shutting down...")
    global http_session, bot_client
    if http_session and not http_session.closed:
        await http_session.close()
    if bot_client and bot_client.is_connected():
        await bot_client.disconnect()

from bot_context import bind_active_bot, get_bot_module

_THIS_MODULE = sys.modules.get(__name__)

# Bind re-exported functions to this bot module instance so that all downstream
# handler and service calls resolve mocks and module overrides from the calling bot instance.
for _name, _fn in list(globals().items()):
    if (inspect.isfunction(_fn) or inspect.iscoroutinefunction(_fn)) and not _name.startswith("__"):
        globals()[_name] = bind_active_bot(_fn, _THIS_MODULE)


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )
