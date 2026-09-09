import sys
import time
import asyncio
from collections import defaultdict
import html
import re
import requests
from aiogram.types import Message, ParseMode, InlineKeyboardMarkup, InlineKeyboardButton

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

import config
from logger import logger
from services.whatsapp import get_http_headers, get_http_session, post_whatsapp_json, notify_admins_auth_required
from account_pool import CONFIGURED_ACCOUNTS, ID_TO_INDEX, ACCOUNT_LABELS

# ---------- Session Watchdog State ----------
_watchdog_consecutive_failures: int = 0   # consecutive /health checks returning not-ready (Account 1 / global)
_watchdog_outage_start: float | None = None  # timestamp when outage was first detected (Account 1 / global)
_watchdog_reconnect_lock = asyncio.Lock()   # prevents concurrent reconnect attempts
_watchdog_last_reconnect_at: float = 0.0   # last time we called /createsession for Account 1

# Per-account health tracking for multi-account pools
_watchdog_account_failures: dict[str, int] = defaultdict(int)
_watchdog_account_outage_start: dict[str, float | None] = defaultdict(lambda: None)
_watchdog_account_reconnect_at: dict[str, float] = defaultdict(float)

_last_logout_alert_time: dict[str, float] = defaultdict(float)
LOGOUT_ALERT_COOLDOWN = 300  # 5 minutes debounce per account

WATCHDOG_CHECK_INTERVAL = 5 * 60        # ping /health every 5 minutes
WATCHDOG_FAILURE_THRESHOLD = 3          # raise alert after this many consecutive failures
WATCHDOG_RECONNECT_COOLDOWN = 10 * 60   # minimum seconds between /createsession calls per account


from bot_context import get_bot_module as _get_bot_module


def _is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def _get_whatsapp_health(client_id: str = "user") -> bool:
    """
    Pings the WhatsApp service /health endpoint for a specific client_id.
    Returns True if the session is ready, False on any failure or not-ready status.
    """
    bot_mod = _get_bot_module()
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session

    try:
        session = await get_session_fn()
        if client_id == "user":
            url = f"{config.whatsapp_service}/health"
        else:
            url = f"{config.whatsapp_service}/health?clientId={client_id}"

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
        logger.debug(f"[Watchdog] /health check failed for {client_id}: {e}")
        return False


async def _get_all_sessions_status() -> list[dict] | None:
    """
    Queries the WhatsApp microservice GET /sessions endpoint to inspect all configured accounts at once.
    Returns list of account dictionaries or None if request fails.
    """
    bot_mod = _get_bot_module()
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session

    try:
        session = await get_session_fn()
        url = f"{config.whatsapp_service}/sessions"
        if session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as res:
                if res.status == 200:
                    data = await res.json()
                    return data.get("accounts", [])
                return None
        else:
            def _sync_get():
                return requests.get(url, headers=get_http_headers(), timeout=10)
            r = await asyncio.to_thread(_sync_get)
            if r.status_code == 200:
                return r.json().get("accounts", [])
            return None
    except Exception as e:
        logger.debug(f"[Watchdog] /sessions check failed: {e}")
        return None


async def _watchdog_attempt_reconnect(client_id: str = "user"):
    """
    Attempts to trigger Baileys /createsession to auto-reconnect the WhatsApp socket
    for a specific account (defaults to 'user').
    Respects per-account cooldown to avoid hammering the service.
    """
    global _watchdog_last_reconnect_at
    bot_mod = _get_bot_module()
    lock = getattr(bot_mod, "_watchdog_reconnect_lock", _watchdog_reconnect_lock) if bot_mod else _watchdog_reconnect_lock
    cooldown = getattr(bot_mod, "WATCHDOG_RECONNECT_COOLDOWN", WATCHDOG_RECONNECT_COOLDOWN) if bot_mod else WATCHDOG_RECONNECT_COOLDOWN
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

    if lock.locked():
        logger.debug(f"[Watchdog] Reconnect already in progress, skipping {client_id}.")
        return

    now = time.time()
    last_reconnect = getattr(bot_mod, "_watchdog_last_reconnect_at", _watchdog_last_reconnect_at) if (client_id == "user" and bot_mod) else _watchdog_account_reconnect_at.get(client_id, 0.0)

    if now - last_reconnect < cooldown:
        remaining = int(cooldown - (now - last_reconnect))
        logger.debug(f"[Watchdog] Reconnect cooldown active for {client_id}, {remaining}s remaining.")
        return

    async with lock:
        now_ts = time.time()
        _watchdog_account_reconnect_at[client_id] = now_ts
        if client_id == "user":
            _watchdog_last_reconnect_at = now_ts
            if bot_mod:
                bot_mod._watchdog_last_reconnect_at = now_ts

        acc_label = ACCOUNT_LABELS.get(client_id, client_id)
        logger.info(f"[Watchdog] Triggering /createsession to auto-reconnect WhatsApp socket for {acc_label} ({client_id})...")
        try:
            status, data = await post_fn("createsession", {"clientId": client_id}, timeout_sec=30)
            if status == 200 and data.get("ready"):
                logger.info(f"[Watchdog] /createsession reported {client_id} is already ready.")
            elif data.get("qrcode"):
                logger.warning(f"[Watchdog] /createsession returned a QR code for {client_id} — manual login required.")
            elif status == 202:
                logger.info(f"[Watchdog] /createsession started session initialization for {client_id} in background.")
            else:
                logger.warning(f"[Watchdog] /createsession for {client_id} responded {status}: {data}")
        except Exception as e:
            logger.error(f"[Watchdog] Failed to call /createsession for {client_id}: {e}")


async def scheduled_session_watchdog_loop():
    """
    Background watchdog that monitors the WhatsApp/Baileys session health every 5 minutes
    across all configured accounts in the multi-account pool.
    """
    global _watchdog_consecutive_failures, _watchdog_outage_start

    await asyncio.sleep(90)  # give Baileys time to initialize before first check
    logger.info("[Watchdog] Multi-account session watchdog started. Checking every 5 minutes.")

    while True:
        bot_mod = _get_bot_module()
        bot_instance = getattr(bot_mod, "bot", None)
        threshold = getattr(bot_mod, "WATCHDOG_FAILURE_THRESHOLD", WATCHDOG_FAILURE_THRESHOLD) if bot_mod else WATCHDOG_FAILURE_THRESHOLD
        interval = getattr(bot_mod, "WATCHDOG_CHECK_INTERVAL", WATCHDOG_CHECK_INTERVAL) if bot_mod else WATCHDOG_CHECK_INTERVAL
        reconnect_fn = getattr(bot_mod, "_watchdog_attempt_reconnect", _watchdog_attempt_reconnect) if bot_mod else _watchdog_attempt_reconnect
        notify_fn = getattr(bot_mod, "notify_admins_auth_required", notify_admins_auth_required) if bot_mod else notify_admins_auth_required

        try:
            get_sessions_fn = getattr(bot_mod, "_get_all_sessions_status", _get_all_sessions_status) if bot_mod else _get_all_sessions_status
            sessions_list = await get_sessions_fn()

            if sessions_list and isinstance(sessions_list, list):
                for acc in sessions_list:
                    acc_id = acc.get("id", "user")
                    acc_label = acc.get("label") or ACCOUNT_LABELS.get(acc_id, acc_id)
                    acc_index = acc.get("index") or ID_TO_INDEX.get(acc_id, 1)
                    is_ready = bool(acc.get("isReady", False))
                    has_auth = bool(acc.get("hasAuthFolder", False)) or (acc_id == "user")
                    status = str(acc.get("status", ""))

                    # Only monitor accounts that are logged in or configured (has auth folder or ready)
                    if not has_auth and status == "not_logged_in":
                        continue

                    failures = _watchdog_account_failures[acc_id]
                    outage_start = _watchdog_account_outage_start[acc_id]

                    if is_ready:
                        if failures > 0:
                            outage_secs = int(time.time() - outage_start) if outage_start else 0
                            outage_str = (
                                f"{outage_secs // 60}m {outage_secs % 60}s"
                                if outage_secs >= 60
                                else f"{outage_secs}s"
                            )
                            logger.info(
                                f"[Watchdog] ✅ WhatsApp session for {acc_label} ({acc_id}) recovered after {outage_str} "
                                f"({failures} failed checks)."
                            )
                            if bot_instance:
                                for admin_id in config.admin_ids:
                                    try:
                                        await bot_instance.send_message(
                                            admin_id,
                                            f"✅ <b>WhatsApp Session Recovered</b>\n\n"
                                            f"Account: <b>{acc_label}</b> (<code>{acc_id}</code>)\n"
                                            f"The Baileys socket reconnected successfully.\n"
                                            f"⏱ Outage duration: <b>{outage_str}</b>\n"
                                            f"🔄 Failed health checks: {failures}",
                                            parse_mode=ParseMode.HTML,
                                        )
                                    except Exception as e:
                                        logger.error(f"[Watchdog] Failed to send recovery alert to admin {admin_id}: {e}")

                            _watchdog_account_failures[acc_id] = 0
                            _watchdog_account_outage_start[acc_id] = None
                            if acc_id == "user":
                                _watchdog_consecutive_failures = 0
                                _watchdog_outage_start = None
                                if bot_mod:
                                    bot_mod._watchdog_consecutive_failures = 0
                                    bot_mod._watchdog_outage_start = None
                        else:
                            logger.debug(f"[Watchdog] /sessions OK — {acc_label} is ready.")
                    else:
                        failures += 1
                        _watchdog_account_failures[acc_id] = failures
                        if outage_start is None:
                            outage_start = time.time()
                            _watchdog_account_outage_start[acc_id] = outage_start

                        if acc_id == "user":
                            _watchdog_consecutive_failures = failures
                            _watchdog_outage_start = outage_start
                            if bot_mod:
                                bot_mod._watchdog_consecutive_failures = failures
                                bot_mod._watchdog_outage_start = outage_start

                        logger.warning(
                            f"[Watchdog] WhatsApp session {acc_label} ({acc_id}) NOT ready "
                            f"(consecutive failures: {failures}/{threshold}, status: {status})."
                        )

                        if failures >= threshold:
                            outage_secs = int(time.time() - outage_start)
                            outage_str = (
                                f"{outage_secs // 60}m {outage_secs % 60}s"
                                if outage_secs >= 60
                                else f"{outage_secs}s"
                            )
                            logger.error(
                                f"[Watchdog] 🚨 {failures} consecutive failures for {acc_label} "
                                f"({outage_str} outage). Alerting admins and attempting reconnect."
                            )

                            if acc_id == "user":
                                await notify_fn()

                            if bot_instance:
                                for admin_id in config.admin_ids:
                                    try:
                                        await bot_instance.send_message(
                                            admin_id,
                                            f"🚨 <b>WhatsApp Session Watchdog Alert</b>\n\n"
                                            f"Account: <b>{acc_label}</b> (<code>{acc_id}</code>)\n"
                                            f"The Baileys socket has been <b>unreachable for {outage_str}</b> "
                                            f"({failures} consecutive health check failures).\n\n"
                                            f"🔄 Attempting automatic reconnection...\n"
                                            f"If reconnection fails, please use <code>/login {acc_index}</code> to re-authenticate.",
                                            parse_mode=ParseMode.HTML,
                                        )
                                    except Exception as e:
                                        logger.error(f"[Watchdog] Failed to send alert to admin {admin_id}: {e}")

                            await reconnect_fn(acc_id)

            else:
                # Fallback path if /sessions is unavailable
                health_fn = getattr(bot_mod, "_get_whatsapp_health", _get_whatsapp_health) if bot_mod else _get_whatsapp_health
                is_ready = await health_fn("user")
                failures = getattr(bot_mod, "_watchdog_consecutive_failures", _watchdog_consecutive_failures) if bot_mod else _watchdog_consecutive_failures
                outage_start = getattr(bot_mod, "_watchdog_outage_start", _watchdog_outage_start) if bot_mod else _watchdog_outage_start

                if is_ready:
                    if failures > 0:
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
                            f"({failures} failed checks)."
                        )
                        if bot_instance:
                            for admin_id in config.admin_ids:
                                try:
                                    await bot_instance.send_message(
                                        admin_id,
                                        f"✅ <b>WhatsApp Session Recovered</b>\n\n"
                                        f"The Baileys socket reconnected successfully.\n"
                                        f"⏱ Outage duration: <b>{outage_str}</b>\n"
                                        f"🔄 Failed health checks: {failures}",
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
                    failures += 1
                    _watchdog_consecutive_failures = failures
                    if outage_start is None:
                        outage_start = time.time()
                        _watchdog_outage_start = outage_start

                    if bot_mod:
                        bot_mod._watchdog_consecutive_failures = failures
                        bot_mod._watchdog_outage_start = outage_start

                    logger.warning(
                        f"[Watchdog] WhatsApp session NOT ready "
                        f"(consecutive failures: {failures}/{threshold})."
                    )

                    if failures >= threshold:
                        outage_secs = int(time.time() - outage_start)
                        outage_str = (
                            f"{outage_secs // 60}m {outage_secs % 60}s"
                            if outage_secs >= 60
                            else f"{outage_secs}s"
                        )
                        logger.error(
                            f"[Watchdog] 🚨 {failures} consecutive failures "
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
                                        f"({failures} consecutive health check failures).\n\n"
                                        f"🔄 Attempting automatic reconnection...\n"
                                        f"If reconnection fails, please use /login to re-authenticate.",
                                        parse_mode=ParseMode.HTML,
                                    )
                                except Exception as e:
                                    logger.error(f"[Watchdog] Failed to send alert to admin {admin_id}: {e}")

                        await reconnect_fn("user")

        except asyncio.CancelledError:
            logger.info("[Watchdog] Session watchdog loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[Watchdog] Unexpected error in watchdog loop: {e}", exc_info=True)

        try:
            await check_and_alert_admin_permissions(bot_instance)
        except Exception as e:
            logger.debug(f"[Watchdog] check_and_alert_admin_permissions error: {e}")

        await asyncio.sleep(interval)


_last_admin_permission_alert_time: float = 0.0


async def check_and_alert_admin_permissions(bot_instance=None):
    """
    Periodically checks if all connected WhatsApp accounts are admins/owners in forwarded channels.
    If issues are found, alerts admins (debounced every 6 hours).
    """
    global _last_admin_permission_alert_time
    now = time.time()
    if now - _last_admin_permission_alert_time < 21600:  # 6 hours debounce
        return

    bot_mod = _get_bot_module()
    b_inst = bot_instance or (getattr(bot_mod, "bot", None) if bot_mod else None)
    if not b_inst:
        return

    audit_fn = getattr(bot_mod, "audit_channel_admins", None)
    if not audit_fn:
        from services.whatsapp import audit_channel_admins as audit_fn

    try:
        status_code, data = await audit_fn()
        if status_code == 200 and not data.get("allCompliant", False):
            issues = data.get("issues", [])
            ready_issues = [i for i in issues if i.get("account", {}).get("role") != "NOT_CONNECTED"]
            if not ready_issues:
                return

            _last_admin_permission_alert_time = now

            alert_lines = [
                "⚠️ <b>[Watchdog Alert] Missing Channel Admin Permissions!</b>\n",
                "One or more connected WhatsApp accounts do not have Admin/Owner rights in forwarded channels:\n",
            ]
            for idx, iss in enumerate(ready_issues[:8], 1):
                raw_dest_name = str(iss.get("destinationName") or iss.get("destinationId") or "").strip("<> ")
                d_name = html.escape(raw_dest_name)
                acc = iss.get("account", {})
                phone = html.escape(str(acc.get("phone") or "No Phone"))
                lbl = html.escape(str(acc.get("label") or f"Account {acc.get('index', '?')}"))
                r = html.escape(str(acc.get("role", "NOT ADMIN")))
                alert_lines.append(f"• <b>{d_name}</b>: <code>{phone}</code> ({lbl}) is <b>{r}</b>")

            if len(ready_issues) > 8:
                alert_lines.append(f"<i>...and {len(ready_issues) - 8} more channels.</i>")

            alert_lines.append("\n👉 <i>Promote these mobile numbers to Admin in WhatsApp or use /audit_admins to inspect!</i>")
            msg_text = "\n".join(alert_lines)

            for admin_id in config.admin_ids:
                try:
                    await b_inst.send_message(admin_id, msg_text, parse_mode=ParseMode.HTML)
                except Exception as ex:
                    logger.debug(f"[Watchdog] Failed to send HTML permission alert to {admin_id}: {ex}. Falling back to plain text...")
                    try:
                        clean_plain = re.sub(r"<[^>]+>", "", msg_text)
                        await b_inst.send_message(admin_id, clean_plain)
                    except Exception as plain_ex:
                        logger.debug(f"[Watchdog] Plain text fallback also failed for {admin_id}: {plain_ex}")
    except Exception as e:
        logger.debug(f"[Watchdog] check_and_alert_admin_permissions notice: {e}")


async def send_instant_logout_alert(
    account_id: str,
    phone: str | None = None,
    reason: str = "Session Logged Out or Banned",
) -> bool:
    """
    Immediately dispatches a high-priority Telegram push alert to all admin IDs
    when an account experiences a 401 Unauthorized or session logout.
    Includes an interactive inline button: [ 🔑 Generate New QR Code ] for one-tap recovery.
    """
    bot_mod = _get_bot_module()
    bot_instance = getattr(bot_mod, "bot", None)
    pool = getattr(bot_mod, "account_pool", None)

    canon_id = pool.resolve_account_id(account_id) if pool else ("user" if account_id in ("1", "user") else account_id)
    now = time.time()

    last_alert = _last_logout_alert_time[canon_id]
    cooldown = getattr(bot_mod, "LOGOUT_ALERT_COOLDOWN", LOGOUT_ALERT_COOLDOWN) if bot_mod else LOGOUT_ALERT_COOLDOWN
    if now - last_alert < cooldown:
        logger.debug(f"[Watchdog] Logout alert for {canon_id} debounced ({int(now - last_alert)}s ago).")
        return False

    _last_logout_alert_time[canon_id] = now
    if bot_mod and hasattr(bot_mod, "_last_logout_alert_time"):
        bot_mod._last_logout_alert_time[canon_id] = now

    # 1. Immediately mark account degraded in the pool so traffic shifts away
    if pool:
        pool.mark_degraded(canon_id, cooldown_sec=3600)
        if canon_id in pool._accounts:
            pool._accounts[canon_id]["is_ready"] = False
            pool._accounts[canon_id]["status"] = "logged_out"

    acc_index = ID_TO_INDEX.get(canon_id, 1)
    short_label = f"Account {acc_index}" if canon_id != "user" else "Account 1"

    # 2. Resolve phone number
    phone_val = phone
    if not phone_val and pool:
        phone_val = pool.get_account_info(canon_id).get("phone")
    phone_str = f" ({phone_val})" if phone_val else ""

    # 3. Compute active siblings traffic shift
    if pool:
        siblings_str = pool.format_active_siblings_string(canon_id)
    else:
        siblings_str = "Account 1"

    # 4. Construct inline keyboard for one-tap recovery
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton(
            text="🔑 Generate New QR Code",
            callback_data=f"cb:acc:login:{acc_index}",
        )
    )

    alert_text = (
        f"🚨 <b>URGENT: WhatsApp {short_label}{phone_str} was logged out!</b>\n\n"
        f"Traffic has been safely shifted to <b>{siblings_str}</b>."
    )

    logger.error(
        f"[Watchdog] 🚨 PUSH ALERT: WhatsApp {short_label}{phone_str} was logged out. Traffic shifted to {siblings_str}."
    )

    if bot_instance:
        for admin_id in config.admin_ids:
            try:
                await bot_instance.send_message(
                    admin_id,
                    alert_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"[Watchdog] Failed to send instant logout alert to admin {admin_id}: {e}")

    return True


async def scheduled_instant_disconnect_watchdog_loop():
    """
    Sub-second / real-time poller that checks for 401 disconnect events
    from the WhatsApp microservice every 1.5 seconds.
    """
    await asyncio.sleep(5)  # allow WhatsApp and bot to initialize
    logger.info("[Watchdog] Instant disconnect push alert loop started (polling interval: 1.5s).")

    while True:
        try:
            bot_mod = _get_bot_module()
            get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session
            session = await get_session_fn()
            url = f"{config.whatsapp_service}/disconnect-events?ack=true"

            events = []
            if session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as res:
                    if res.status == 200:
                        data = await res.json()
                        events = data.get("events", [])
            else:
                def _sync_get():
                    return requests.get(url, headers=get_http_headers(), timeout=5)
                r = await asyncio.to_thread(_sync_get)
                if r.status_code == 200:
                    events = r.json().get("events", [])

            if events and isinstance(events, list):
                for ev in events:
                    acc_id = ev.get("clientId", "user")
                    phone = ev.get("phone")
                    reason = ev.get("reason", "Session Logged Out or Banned")
                    alert_fn = getattr(bot_mod, "send_instant_logout_alert", send_instant_logout_alert) if bot_mod else send_instant_logout_alert
                    await alert_fn(acc_id, phone=phone, reason=reason)

            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[Watchdog] Instant disconnect poller error: {e}")
            await asyncio.sleep(2.0)


async def cmd_watchdog(message: Message):
    """
    /watchdog — shows the current session watchdog status across all multi-account slots.
    """
    bot_mod = _get_bot_module()
    check_admin_fn = getattr(bot_mod, "is_admin", _is_admin) if bot_mod else _is_admin
    if not check_admin_fn(message.from_user.id):
        return

    threshold = getattr(bot_mod, "WATCHDOG_FAILURE_THRESHOLD", WATCHDOG_FAILURE_THRESHOLD) if bot_mod else WATCHDOG_FAILURE_THRESHOLD
    check_interval = getattr(bot_mod, "WATCHDOG_CHECK_INTERVAL", WATCHDOG_CHECK_INTERVAL) if bot_mod else WATCHDOG_CHECK_INTERVAL
    cooldown = getattr(bot_mod, "WATCHDOG_RECONNECT_COOLDOWN", WATCHDOG_RECONNECT_COOLDOWN) if bot_mod else WATCHDOG_RECONNECT_COOLDOWN
    get_sessions_fn = getattr(bot_mod, "_get_all_sessions_status", _get_all_sessions_status) if bot_mod else _get_all_sessions_status
    sessions_list = await get_sessions_fn()

    if sessions_list and isinstance(sessions_list, list):
        ready_count = sum(1 for a in sessions_list if a.get("isReady"))
        total_configured = len(sessions_list)
        overall_status = f"✅ <b>Operational</b> ({ready_count}/{total_configured} Active)" if ready_count > 0 else "🔴 <b>All Disconnected</b>"

        account_lines = []
        for acc in sessions_list:
            acc_id = acc.get("id", "user")
            acc_label = acc.get("label") or ACCOUNT_LABELS.get(acc_id, acc_id)
            acc_index = acc.get("index") or ID_TO_INDEX.get(acc_id, 1)
            is_ready = bool(acc.get("isReady"))
            status_raw = str(acc.get("status", "unknown"))
            phone = acc.get("phone")
            phone_str = f" (📞 <code>{phone}</code>)" if phone else ""

            failures = _watchdog_account_failures.get(acc_id, 0)
            outage_start = _watchdog_account_outage_start.get(acc_id)
            last_reconnect_at = _watchdog_account_reconnect_at.get(acc_id, 0.0)

            if is_ready:
                badge = "🟢 <b>Ready</b>"
            elif status_raw == "waiting_qr_scan":
                badge = "🟡 <b>Waiting for QR Scan</b>"
            elif status_raw == "reconnecting":
                badge = "🔄 <b>Reconnecting</b>"
            elif acc.get("hasAuthFolder"):
                badge = f"🔴 <b>Disconnected</b> ({failures} fails)" if failures > 0 else "🔴 <b>Disconnected</b>"
            else:
                badge = "⚪ <b>Not Logged In</b>"

            extra_info = []
            if acc.get("hasProxy"):
                proxy_host = acc.get("proxyHost") or "Active"
                extra_info.append(f"🌐 Proxy: {proxy_host}")

            if outage_start and failures > 0:
                outage_secs = int(time.time() - outage_start)
                outage_str = f"{outage_secs // 60}m {outage_secs % 60}s" if outage_secs >= 60 else f"{outage_secs}s"
                extra_info.append(f"⏱ Outage: {outage_str}")

            if last_reconnect_at > 0:
                ago = int(time.time() - last_reconnect_at)
                ago_str = f"{ago // 60}m ago" if ago >= 60 else f"{ago}s ago"
                extra_info.append(f"🔄 Reconnected: {ago_str}")

            extra_str = f" <i>[{', '.join(extra_info)}]</i>" if extra_info else ""
            account_lines.append(f"• <b>{acc_label}</b>: {badge}{phone_str}{extra_str}")

        accounts_block = "\n".join(account_lines)
    else:
        health_fn = getattr(bot_mod, "_get_whatsapp_health", _get_whatsapp_health) if bot_mod else _get_whatsapp_health
        failures = getattr(bot_mod, "_watchdog_consecutive_failures", _watchdog_consecutive_failures) if bot_mod else _watchdog_consecutive_failures
        outage_start = getattr(bot_mod, "_watchdog_outage_start", _watchdog_outage_start) if bot_mod else _watchdog_outage_start
        last_reconnect_at = getattr(bot_mod, "_watchdog_last_reconnect_at", _watchdog_last_reconnect_at) if bot_mod else _watchdog_last_reconnect_at

        is_ready = await health_fn("user")
        session_status = "✅ <b>Ready</b>" if is_ready else (f"🔴 <b>Not Ready</b> ({failures} consecutive failures)" if failures > 0 else "🟡 <b>Checking...</b>")
        overall_status = session_status

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

        accounts_block = f"📡 <b>Session:</b> {session_status}{outage_info}{last_reconnect}"

    text = (
        f"🩺 <b>Multi-Account Session Watchdog Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Gateway Health:</b> {overall_status}\n\n"
        f"📱 <b>Account Slots:</b>\n"
        f"{accounts_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Check Interval:</b> {check_interval // 60} min\n"
        f"⚠️ <b>Alert Threshold:</b> {threshold} consecutive failures\n"
        f"🔁 <b>Reconnect Cooldown:</b> {cooldown // 60} min"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


def register_watchdog_handlers(dp):
    dp.register_message_handler(cmd_watchdog, commands=["watchdog"])

