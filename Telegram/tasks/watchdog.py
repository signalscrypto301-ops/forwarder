import sys
import time
import asyncio
import requests
from aiogram.types import Message, ParseMode

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

import config
from logger import logger
from services.whatsapp import get_http_headers, get_http_session, post_whatsapp_json, notify_admins_auth_required

# ---------- Session Watchdog State ----------
_watchdog_consecutive_failures: int = 0   # consecutive /health checks returning not-ready
_watchdog_outage_start: float | None = None  # timestamp when outage was first detected
_watchdog_reconnect_lock = asyncio.Lock()   # prevents concurrent reconnect attempts
_watchdog_last_reconnect_at: float = 0.0   # last time we called /createsession

WATCHDOG_CHECK_INTERVAL = 5 * 60        # ping /health every 5 minutes
WATCHDOG_FAILURE_THRESHOLD = 3          # raise alert after this many consecutive failures
WATCHDOG_RECONNECT_COOLDOWN = 10 * 60   # minimum seconds between /createsession calls


from bot_context import get_bot_module as _get_bot_module


def _is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def _get_whatsapp_health() -> bool:
    """
    Pings the WhatsApp service /health endpoint.
    Returns True if the session is ready, False on any failure or not-ready status.
    """
    bot_mod = _get_bot_module()
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session

    try:
        session = await get_session_fn()
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
    bot_mod = _get_bot_module()
    lock = getattr(bot_mod, "_watchdog_reconnect_lock", _watchdog_reconnect_lock) if bot_mod else _watchdog_reconnect_lock
    last_reconnect = getattr(bot_mod, "_watchdog_last_reconnect_at", _watchdog_last_reconnect_at) if bot_mod else _watchdog_last_reconnect_at
    cooldown = getattr(bot_mod, "WATCHDOG_RECONNECT_COOLDOWN", WATCHDOG_RECONNECT_COOLDOWN) if bot_mod else WATCHDOG_RECONNECT_COOLDOWN
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

    if lock.locked():
        logger.debug("[Watchdog] Reconnect already in progress, skipping.")
        return

    now = time.time()
    if now - last_reconnect < cooldown:
        remaining = int(cooldown - (now - last_reconnect))
        logger.debug(f"[Watchdog] Reconnect cooldown active, {remaining}s remaining.")
        return

    async with lock:
        _watchdog_last_reconnect_at = time.time()
        if bot_mod:
            bot_mod._watchdog_last_reconnect_at = _watchdog_last_reconnect_at
        logger.info("[Watchdog] Triggering /createsession to auto-reconnect WhatsApp socket...")
        try:
            status, data = await post_fn("createsession", {"clientId": "user"}, timeout_sec=30)
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
    """
    global _watchdog_consecutive_failures, _watchdog_outage_start

    await asyncio.sleep(90)  # give Baileys time to initialize before first check
    logger.info("[Watchdog] Session watchdog started. Checking every 5 minutes.")

    while True:
        bot_mod = _get_bot_module()
        bot_instance = getattr(bot_mod, "bot", None)
        threshold = getattr(bot_mod, "WATCHDOG_FAILURE_THRESHOLD", WATCHDOG_FAILURE_THRESHOLD) if bot_mod else WATCHDOG_FAILURE_THRESHOLD
        interval = getattr(bot_mod, "WATCHDOG_CHECK_INTERVAL", WATCHDOG_CHECK_INTERVAL) if bot_mod else WATCHDOG_CHECK_INTERVAL
        health_fn = getattr(bot_mod, "_get_whatsapp_health", _get_whatsapp_health) if bot_mod else _get_whatsapp_health
        reconnect_fn = getattr(bot_mod, "_watchdog_attempt_reconnect", _watchdog_attempt_reconnect) if bot_mod else _watchdog_attempt_reconnect
        notify_fn = getattr(bot_mod, "notify_admins_auth_required", notify_admins_auth_required) if bot_mod else notify_admins_auth_required

        consecutive_failures = getattr(bot_mod, "_watchdog_consecutive_failures", _watchdog_consecutive_failures) if bot_mod else _watchdog_consecutive_failures
        outage_start = getattr(bot_mod, "_watchdog_outage_start", _watchdog_outage_start) if bot_mod else _watchdog_outage_start

        try:
            is_ready = await health_fn()

            if is_ready:
                if consecutive_failures > 0:
                    outage_secs = (
                        int(time.time() - outage_start)
                        if outage_start
                        else 0
                    )
                    outage_str = (
                        f"{outage_secs // 60}m {outage_secs % 60}s"
                        if outage_secs >= 60
                        else f"{outage_secs}s"
                    )
                    logger.info(
                        f"[Watchdog] ✅ WhatsApp session recovered after {outage_str} "
                        f"({consecutive_failures} failed checks)."
                    )
                    if bot_instance:
                        for admin_id in config.admin_ids:
                            try:
                                await bot_instance.send_message(
                                    admin_id,
                                    f"✅ <b>WhatsApp Session Recovered</b>\n\n"
                                    f"The Baileys socket reconnected successfully.\n"
                                    f"⏱ Outage duration: <b>{outage_str}</b>\n"
                                    f"🔄 Failed health checks: {consecutive_failures}",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception as e:
                                logger.error(f"[Watchdog] Failed to send recovery alert to admin {admin_id}: {e}")

                    _watchdog_consecutive_failures = 0
                    _watchdog_outage_start = None
                    if bot_mod:
                        bot_mod._watchdog_consecutive_failures = 0
                        bot_mod._watchdog_outage_start = None
                else:
                    logger.debug("[Watchdog] /health OK — session is ready.")

            else:
                consecutive_failures += 1
                _watchdog_consecutive_failures = consecutive_failures
                if outage_start is None:
                    outage_start = time.time()
                    _watchdog_outage_start = outage_start

                if bot_mod:
                    bot_mod._watchdog_consecutive_failures = consecutive_failures
                    bot_mod._watchdog_outage_start = outage_start

                logger.warning(
                    f"[Watchdog] WhatsApp session NOT ready "
                    f"(consecutive failures: {consecutive_failures}/{threshold})."
                )

                if consecutive_failures >= threshold:
                    outage_secs = int(time.time() - outage_start)
                    outage_str = (
                        f"{outage_secs // 60}m {outage_secs % 60}s"
                        if outage_secs >= 60
                        else f"{outage_secs}s"
                    )
                    logger.error(
                        f"[Watchdog] 🚨 {consecutive_failures} consecutive failures "
                        f"({outage_str} outage). Alerting admins and attempting reconnect."
                    )

                    await notify_fn()

                    if bot_instance:
                        for admin_id in config.admin_ids:
                            try:
                                await bot_instance.send_message(
                                    admin_id,
                                    f"🚨 <b>WhatsApp Session Watchdog Alert</b>\n\n"
                                    f"The Baileys socket has been <b>unreachable for {outage_str}</b> "
                                    f"({consecutive_failures} consecutive health check failures).\n\n"
                                    f"🔄 Attempting automatic reconnection...\n"
                                    f"If reconnection fails, please use /login to re-authenticate.",
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception as e:
                                logger.error(f"[Watchdog] Failed to send alert to admin {admin_id}: {e}")

                    await reconnect_fn()

        except asyncio.CancelledError:
            logger.info("[Watchdog] Session watchdog loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[Watchdog] Unexpected error in watchdog loop: {e}", exc_info=True)

        await asyncio.sleep(interval)


async def cmd_watchdog(message: Message):
    """
    /watchdog — shows the current session watchdog status.
    """
    bot_mod = _get_bot_module()
    check_admin_fn = getattr(bot_mod, "is_admin", _is_admin) if bot_mod else _is_admin
    if not check_admin_fn(message.from_user.id):
        return

    health_fn = getattr(bot_mod, "_get_whatsapp_health", _get_whatsapp_health) if bot_mod else _get_whatsapp_health
    failures = getattr(bot_mod, "_watchdog_consecutive_failures", _watchdog_consecutive_failures) if bot_mod else _watchdog_consecutive_failures
    outage_start = getattr(bot_mod, "_watchdog_outage_start", _watchdog_outage_start) if bot_mod else _watchdog_outage_start
    last_reconnect_at = getattr(bot_mod, "_watchdog_last_reconnect_at", _watchdog_last_reconnect_at) if bot_mod else _watchdog_last_reconnect_at
    threshold = getattr(bot_mod, "WATCHDOG_FAILURE_THRESHOLD", WATCHDOG_FAILURE_THRESHOLD) if bot_mod else WATCHDOG_FAILURE_THRESHOLD
    check_interval = getattr(bot_mod, "WATCHDOG_CHECK_INTERVAL", WATCHDOG_CHECK_INTERVAL) if bot_mod else WATCHDOG_CHECK_INTERVAL
    cooldown = getattr(bot_mod, "WATCHDOG_RECONNECT_COOLDOWN", WATCHDOG_RECONNECT_COOLDOWN) if bot_mod else WATCHDOG_RECONNECT_COOLDOWN

    is_ready = await health_fn()

    if is_ready:
        session_status = "✅ <b>Ready</b>"
    elif failures > 0:
        session_status = f"🔴 <b>Not Ready</b> ({failures} consecutive failures)"
    else:
        session_status = "🟡 <b>Checking...</b>"

    outage_info = ""
    if outage_start and failures > 0:
        outage_secs = int(time.time() - outage_start)
        outage_str = (
            f"{outage_secs // 60}m {outage_secs % 60}s"
            if outage_secs >= 60
            else f"{outage_secs}s"
        )
        outage_info = f"\n⏱ <b>Outage Duration:</b> {outage_str}"

    last_reconnect = ""
    if last_reconnect_at > 0:
        ago = int(time.time() - last_reconnect_at)
        ago_str = f"{ago // 60}m {ago % 60}s ago" if ago >= 60 else f"{ago}s ago"
        last_reconnect = f"\n🔄 <b>Last Reconnect Attempt:</b> {ago_str}"

    text = (
        f"🩺 <b>Session Watchdog Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Session:</b> {session_status}{outage_info}{last_reconnect}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Check Interval:</b> {check_interval // 60} min\n"
        f"⚠️ <b>Alert Threshold:</b> {threshold} consecutive failures\n"
        f"🔁 <b>Reconnect Cooldown:</b> {cooldown // 60} min"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


def register_watchdog_handlers(dp):
    dp.register_message_handler(cmd_watchdog, commands=["watchdog"])
