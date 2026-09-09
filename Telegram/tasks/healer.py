import sys
import asyncio
from aiogram.types import ParseMode

import config
from logger import logger
from auto_healer import AutoHealer
from services.whatsapp import post_whatsapp_json

auto_healer = AutoHealer()


from bot_context import get_bot_module as _get_bot_module


async def scheduled_auto_healer_loop():
    """
    Monitors VPS RAM, disk space, and temporary file accumulation every 60 seconds.
    If critical thresholds are crossed (RAM >= 90%, disk >= 90%, or >= 20 temp files),
    automatically executes cache purging, log trimming, and memory reclamation.
    Dispatches an alert card to all configured administrators whenever a heal occurs.
    """
    await asyncio.sleep(30)  # Initial warm-up delay
    logger.info("[AutoHealer] Automated System Health & RAM monitoring active (interval: 60s).")

    while True:
        bot_mod = _get_bot_module()
        healer = getattr(bot_mod, "auto_healer", auto_healer) if bot_mod else auto_healer
        bot_instance = getattr(bot_mod, "bot", None)
        post_fn = getattr(bot_mod, "post_whatsapp_json", post_whatsapp_json) if bot_mod else post_whatsapp_json

        try:
            heal_res = await asyncio.to_thread(healer.check_and_heal_if_needed)
            if heal_res and heal_res.get("status") == "success":
                wa_res = {}
                try:
                    status, wa_data = await post_fn("cleanup", {}, timeout_sec=10)
                    if status == 200:
                        wa_res = wa_data
                except Exception as wa_err:
                    logger.debug(f"[AutoHealer] WhatsApp cleanup notice: {wa_err}")

                alert_text = (
                    "🩺 <b>Automated System Health & RAM Auto-Healer Triggered</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Trigger Reason:</b> <code>{heal_res['reason']}</code>\n"
                    f"<b>Temporary Files Purged:</b> <code>{heal_res['files_purged']} files</code>\n"
                    f"<b>Disk Space Reclaimed:</b> <code>{heal_res['mb_reclaimed']} MB</code>\n"
                    f"<b>Memory Reclaimed:</b> <code>{heal_res['gc_mb_freed']} MB</code>\n"
                    f"<b>Host RAM:</b> <code>{heal_res['ram_before']}% ➔ {heal_res['ram_after']}%</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>Caches purged and resources stabilized before any lag or crash could occur.</i>"
                )
                if bot_instance:
                    for admin_id in config.admin_ids:
                        try:
                            await bot_instance.send_message(admin_id, alert_text, parse_mode=ParseMode.HTML)
                        except Exception as alert_err:
                            logger.error(
                                f"[AutoHealer] Failed to dispatch heal alert to admin {admin_id}: {alert_err}"
                            )
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AutoHealer] Error in scheduled_auto_healer_loop: {e}")
            await asyncio.sleep(60)
