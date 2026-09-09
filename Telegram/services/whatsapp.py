import os
import sys
import time
import asyncio
from datetime import datetime

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

import requests
import qrcode
from aiogram.types import ParseMode

import config
from logger import logger
from database import get_all_channels, get_groups_for_channel, get_all_unique_destinations
from account_pool import AccountPool

http_session: aiohttp.ClientSession | None = None
last_auth_alert_time: float = 0.0

AUDIENCE_CACHE: dict = {"timestamp": 0.0, "data": None}
AUDIENCE_CACHE_LOCK = asyncio.Lock()

WHATSAPP_CHATS_CACHE: dict[str, any] = {"timestamp": 0.0, "chats": []}


from bot_context import get_bot_module as _get_bot_module


def get_http_headers() -> dict[str, str]:
    headers = {}
    if config.api_secret:
        headers["x-api-key"] = config.api_secret
    return headers


async def get_http_session():
    global http_session
    bot_mod = _get_bot_module()
    if bot_mod and hasattr(bot_mod, "http_session"):
        session = getattr(bot_mod, "http_session")
        if session is not None and not session.closed:
            return session

    if not HAS_AIOHTTP:
        return None
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=90)
        http_session = aiohttp.ClientSession(
            headers=get_http_headers(), timeout=timeout
        )
        if bot_mod:
            bot_mod.http_session = http_session
    return http_session


async def close_http_session():
    global http_session
    bot_mod = _get_bot_module()
    sess = getattr(bot_mod, "http_session", http_session) if bot_mod else http_session
    if sess and not sess.closed:
        await sess.close()


async def post_whatsapp_json(endpoint: str, data: dict, timeout_sec: int = 30) -> tuple[int, dict]:
    """Unified POST helper supporting both aiohttp and requests fallback with authentication."""
    bot_mod = _get_bot_module()
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session
    session = await get_session_fn()
    clean_ep = endpoint.lstrip("/")
    url = f"{config.whatsapp_service.rstrip('/')}/{clean_ep}"
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


async def _fetch_baileys_ram_mb() -> int | None:
    """Queries the WhatsApp service /health endpoint to retrieve current RSS memory in MB."""
    try:
        bot_mod = _get_bot_module()
        get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session
        session = await get_session_fn()
        if session:
            async with session.get(f"{config.whatsapp_service}/health", timeout=aiohttp.ClientTimeout(total=3)) as res:
                if res.status == 200:
                    data = await res.json()
                    return data.get("memory", {}).get("rssMb")
        else:
            def _sync_get():
                return requests.get(
                    url=f"{config.whatsapp_service}/health",
                    headers=get_http_headers(),
                    timeout=3,
                )
            res_sync = await asyncio.to_thread(_sync_get)
            if res_sync.status_code == 200:
                data = res_sync.json()
                return data.get("memory", {}).get("rssMb")
    except Exception:
        pass
    return None


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
    dir_path = os.path.dirname(os.path.dirname(__file__))
    image_path = os.path.join(
        dir_path, f"qr_{int(datetime.now().timestamp())}.png"
    )
    img.save(image_path)
    return image_path


async def notify_admins_auth_required():
    """Alert admins when WhatsApp session drops or needs QR login (throttled to once every 5 mins)."""
    global last_auth_alert_time
    bot_mod = _get_bot_module()
    alert_time = getattr(bot_mod, "last_auth_alert_time", last_auth_alert_time) if bot_mod else last_auth_alert_time

    now = datetime.now().timestamp()
    if now - alert_time > 300:
        last_auth_alert_time = now
        if bot_mod:
            bot_mod.last_auth_alert_time = now
        bot_instance = getattr(bot_mod, "bot", None)
        if not bot_instance:
            return
        for admin_id in config.admin_ids:
            try:
                await bot_instance.send_message(
                    admin_id,
                    "⚠️ <b>WhatsApp Session Error</b>: The WhatsApp Web session is unauthorized or disconnected. Please send /login to re-authenticate.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Failed to alert admin {admin_id} about auth: {e}")


async def fetch_audience_metadata(force_refresh: bool = False) -> dict:
    """
    Fetches aggregate audience statistics (groups, newsletters, subscribers)
    from the Baileys WhatsApp microservice. Cached for 15 minutes to reduce socket queries.
    """
    global AUDIENCE_CACHE
    bot_mod = _get_bot_module()
    cache = getattr(bot_mod, "AUDIENCE_CACHE", AUDIENCE_CACHE) if bot_mod else AUDIENCE_CACHE
    cache_lock = getattr(bot_mod, "AUDIENCE_CACHE_LOCK", AUDIENCE_CACHE_LOCK) if bot_mod else AUDIENCE_CACHE_LOCK

    now = time.time()
    if (
        not force_refresh
        and cache.get("data") is not None
        and (now - cache.get("timestamp", 0)) < 900
    ):
        return cache["data"]

    async with cache_lock:
        if (
            not force_refresh
            and cache.get("data") is not None
            and (now - cache.get("timestamp", 0)) < 900
        ):
            return cache["data"]

        get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session
        session = await get_session_fn()
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

        cache["timestamp"] = now
        cache["data"] = data
        AUDIENCE_CACHE["timestamp"] = now
        AUDIENCE_CACHE["data"] = data
        return data


async def fetch_whatsapp_chats(search_query: str = "") -> list[dict]:
    """
    Fetches participating WhatsApp groups and subscribed newsletters from the WhatsApp service.
    Caches results in memory for 15s to make inline button pagination instantaneous.
    """
    global WHATSAPP_CHATS_CACHE
    bot_mod = _get_bot_module()
    cache = getattr(bot_mod, "WHATSAPP_CHATS_CACHE", WHATSAPP_CHATS_CACHE) if bot_mod else WHATSAPP_CHATS_CACHE
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

    now = time.time()
    if cache.get("chats") and (now - cache.get("timestamp", 0.0) < 15.0):
        chats = cache["chats"]
    else:
        try:
            status_code, res_data = await post_fn(
                "getGroups", {"clientId": "user"}, timeout_sec=10
            )
            if status_code == 200 and isinstance(res_data.get("chats"), list):
                chats = res_data["chats"]
                cache["timestamp"] = now
                cache["chats"] = chats
                WHATSAPP_CHATS_CACHE["timestamp"] = now
                WHATSAPP_CHATS_CACHE["chats"] = chats
            else:
                logger.warning(f"Failed to fetch WhatsApp groups: {res_data}")
                chats = cache.get("chats") or []
        except Exception as e:
            logger.error(f"Error calling getGroups: {e}")
            chats = cache.get("chats") or []

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
    bot_mod = _get_bot_module()
    cache = getattr(bot_mod, "WHATSAPP_CHATS_CACHE", WHATSAPP_CHATS_CACHE) if bot_mod else WHATSAPP_CHATS_CACHE
    chats = cache.get("chats") or []
    for c in chats:
        if c.get("id") == group_id and c.get("name"):
            return c.get("name")
    return group_id


async def _sync_account_pool_now() -> None:
    """Queries WhatsApp GET /sessions endpoint and updates live AccountPool metadata."""
    bot_mod = _get_bot_module()
    pool = getattr(bot_mod, "account_pool", None)
    get_session_fn = getattr(bot_mod, "get_http_session", get_http_session) if bot_mod else get_http_session

    try:
        session = await get_session_fn()
        if session:
            async with session.get(
                f"{config.whatsapp_service}/sessions",
                headers=get_http_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as res:
                if res.status == 200:
                    data = await res.json()
                    if pool:
                        pool.sync_from_api_response(data)
        else:
            def _sync():
                return requests.get(
                    f"{config.whatsapp_service}/sessions",
                    headers=get_http_headers(),
                    timeout=5,
                )
            res_sync = await asyncio.to_thread(_sync)
            if res_sync.status_code == 200:
                data = res_sync.json()
                if pool:
                    pool.sync_from_api_response(data)
    except Exception as e:
        logger.debug(f"[AccountPool] _sync_account_pool_now exception: {e}")


async def sync_database_newsletters_to_whatsapp():
    """
    Scans all mapped groups in SQLite database and registers any newsletters/groups
    with the WhatsApp microservice so they are instantly discoverable via /get_chat_id and /map.
    """
    bot_mod = _get_bot_module()
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

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
            await post_fn("registerNewsletters", {"newsletters": list(all_jids)}, timeout_sec=15)
    except Exception as e:
        logger.debug(f"sync_database_newsletters_to_whatsapp notice: {e}")


async def audit_channel_admins(destinations: list[str] | None = None) -> tuple[int, dict]:
    """
    Queries the WhatsApp service to audit Admin & Owner permissions for all connected WhatsApp accounts.
    If destinations is None, automatically pulls all active forwarding destinations from SQLite database.
    Returns (status_code, response_data).
    """
    bot_mod = _get_bot_module()
    post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json
    get_dests_fn = getattr(bot_mod, "get_all_unique_destinations", get_all_unique_destinations) if bot_mod else get_all_unique_destinations

    if destinations is None:
        try:
            destinations = await asyncio.to_thread(get_dests_fn)
        except Exception as e:
            logger.warning(f"Error fetching destinations from database: {e}")
            destinations = []

    payload = {"destinations": destinations or []}
    return await post_fn("audit-admins", payload, timeout_sec=30)

