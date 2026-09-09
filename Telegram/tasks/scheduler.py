import sys
import asyncio
from datetime import datetime, timedelta
from aiogram.types import ParseMode

import config
from logger import logger
from database import (
    get_daily_metrics,
    get_active_channels_count,
    get_stale_channels,
)
from services.whatsapp import _sync_account_pool_now
from services.forwarder import cleanup_stale_media


from bot_context import get_bot_module as _get_bot_module


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


async def scheduled_daily_report_loop():
    """
    Background task that sleeps until midnight every day, compiles
    yesterday's delivery metrics report, and broadcasts it to all admins.
    """
    while True:
        try:
            now = datetime.now()
            tomorrow_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            sleep_duration = (tomorrow_midnight - now).total_seconds()
            logger.info(
                f"Daily report scheduler sleeping for {sleep_duration:.1f}s until midnight ({tomorrow_midnight.strftime('%Y-%m-%d %H:%M:%S')})."
            )
            await asyncio.sleep(sleep_duration)

            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            bot_mod = _get_bot_module()
            gen_report_fn = getattr(bot_mod, "generate_daily_report_text", generate_daily_report_text) if bot_mod else generate_daily_report_text
            report_text = gen_report_fn(yesterday_str)

            bot_instance = getattr(bot_mod, "bot", None) if bot_mod else None
            if bot_instance:
                for admin_id in config.admin_ids:
                    try:
                        await bot_instance.send_message(admin_id, report_text)
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
                bot_mod = _get_bot_module()
                gen_digest_fn = getattr(bot_mod, "generate_stale_channels_digest", generate_stale_channels_digest) if bot_mod else generate_stale_channels_digest
                digest = gen_digest_fn(stale_channels, threshold_hours=72)
                bot_instance = getattr(bot_mod, "bot", None) if bot_mod else None
                if bot_instance:
                    for admin_id in config.admin_ids:
                        try:
                            await bot_instance.send_message(admin_id, digest, parse_mode=ParseMode.HTML)
                        except Exception as e:
                            logger.error(
                                f"Failed to dispatch stale channel alert to admin {admin_id}: {e}"
                            )
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
            bot_mod = _get_bot_module()
            cleanup_fn = getattr(bot_mod, "cleanup_stale_media", cleanup_stale_media) if bot_mod else cleanup_stale_media
            await asyncio.to_thread(cleanup_fn)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup task: {e}")


async def scheduled_account_pool_sync_loop():
    """
    Background loop that continuously syncs multi-account status from
    WhatsApp microservice GET /sessions every 30 seconds.
    """
    await asyncio.sleep(5)
    logger.info("[AccountPool] Background account pool sync loop started.")
    while True:
        try:
            bot_mod = _get_bot_module()
            sync_fn = getattr(bot_mod, "_sync_account_pool_now", _sync_account_pool_now) if bot_mod else _sync_account_pool_now
            await sync_fn()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[AccountPool] Sync loop error: {e}")
            await asyncio.sleep(30)
