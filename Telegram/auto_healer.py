import os
import sys
import gc
import time
import shutil
import logging
from datetime import datetime

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

import database
from logger import logger

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media")
DLQ_MEDIA_DIR = os.path.join(MEDIA_DIR, "dlq")
WHATSAPP_UPLOADS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Whatsapp", "uploads")
)
BASE_DIR = os.path.dirname(__file__)


class AutoHealer:
    """
    Automated System Health & RAM Auto-Healer

    Monitors host RAM, disk space, process RSS memory, and temporary file accumulation.
    Automatically purges orphaned temporary files, truncates bloated logs, runs garbage
    collection, and alerts administrators if resources exceed configured safety thresholds.
    """

    def __init__(
        self,
        ram_critical_percent: float = 90.0,
        ram_warning_percent: float = 85.0,
        disk_warning_percent: float = 90.0,
        temp_files_threshold: int = 20,
        temp_file_max_age_sec: int = 180,
        heal_cooldown_sec: int = 300,
    ):
        self.ram_critical_percent = ram_critical_percent
        self.ram_warning_percent = ram_warning_percent
        self.disk_warning_percent = disk_warning_percent
        self.temp_files_threshold = temp_files_threshold
        self.temp_file_max_age_sec = temp_file_max_age_sec
        self.heal_cooldown_sec = heal_cooldown_sec

        self.last_heal_time: float = 0.0
        self.last_heal_reason: str = "None"
        self.is_enabled: bool = True

    def get_telemetry(self) -> dict:
        """Collects current system health and resource telemetry."""
        # 1. Host Virtual Memory
        host_ram_pct = 0.0
        host_ram_used_gb = 0.0
        host_ram_total_gb = 0.0
        if psutil:
            try:
                vm = psutil.virtual_memory()
                if hasattr(vm, "percent") and isinstance(vm.percent, (int, float)):
                    host_ram_pct = round(float(vm.percent), 1)
                    host_ram_used_gb = round(float(vm.used) / (1024 ** 3), 2)
                    host_ram_total_gb = round(float(vm.total) / (1024 ** 3), 2)
            except Exception as e:
                logger.debug(f"[AutoHealer] psutil.virtual_memory error: {e}")

        # 2. CPU Utilization
        cpu_pct = 0
        if psutil:
            try:
                cp = psutil.cpu_percent(interval=0.05)
                if isinstance(cp, (int, float)):
                    cpu_pct = round(float(cp))
            except Exception:
                pass

        # 3. Disk Space
        disk_pct = 0.0
        disk_free_gb = 0.0
        disk_total_gb = 0.0
        if psutil:
            try:
                target_path = os.path.dirname(__file__) or "."
                disk = psutil.disk_usage(target_path)
                if hasattr(disk, "percent") and isinstance(disk.percent, (int, float)):
                    disk_pct = round(float(disk.percent), 1)
                    disk_free_gb = round(float(disk.free) / (1024 ** 3), 1)
                    disk_total_gb = round(float(disk.total) / (1024 ** 3), 1)
            except Exception:
                pass

        # 4. Telegram Bot Process RSS
        bot_ram_mb = 0
        if psutil:
            try:
                proc = psutil.Process(os.getpid())
                mem_info = proc.memory_info()
                if hasattr(mem_info, "rss") and isinstance(mem_info.rss, (int, float)):
                    bot_ram_mb = round(float(mem_info.rss) / (1024 * 1024))
            except Exception:
                pass

        # 5. Media Temp Files Count & Size
        media_count = 0
        media_size_bytes = 0
        for dir_path in [MEDIA_DIR, WHATSAPP_UPLOADS_DIR]:
            if os.path.exists(dir_path):
                try:
                    for root, dirs, files in os.walk(dir_path):
                        # Skip DLQ preservation directory
                        if "dlq" in root.lower():
                            continue
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                if os.path.isfile(fp):
                                    media_count += 1
                                    media_size_bytes += os.path.getsize(fp)
                            except Exception:
                                pass
                except Exception:
                    pass

        media_size_mb = round(media_size_bytes / (1024 * 1024), 2)

        return {
            "host_ram_pct": host_ram_pct,
            "host_ram_used_gb": host_ram_used_gb,
            "host_ram_total_gb": host_ram_total_gb,
            "cpu_pct": cpu_pct,
            "disk_pct": disk_pct,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "bot_ram_mb": bot_ram_mb,
            "media_count": media_count,
            "media_size_mb": media_size_mb,
            "media_size_bytes": media_size_bytes,
        }

    def purge_temp_media(self, max_age_seconds: int | None = None) -> tuple[int, int]:
        """
        Purges temporary media files older than max_age_seconds.
        Strictly preserves the Dead-Letter Queue (DLQ) folder.
        Returns: (files_purged_count, bytes_reclaimed)
        """
        age_limit = max_age_seconds if max_age_seconds is not None else self.temp_file_max_age_sec
        now = time.time()
        purged_count = 0
        reclaimed_bytes = 0

        target_dirs = [MEDIA_DIR, WHATSAPP_UPLOADS_DIR]

        for d in target_dirs:
            if not os.path.exists(d):
                continue
            try:
                for root, dirs, files in os.walk(d):
                    # Never purge inside the DLQ folder
                    if "dlq" in root.lower():
                        continue
                    for f in files:
                        filepath = os.path.join(root, f)
                        try:
                            if not os.path.isfile(filepath):
                                continue
                            file_stat = os.stat(filepath)
                            file_age = now - file_stat.st_mtime
                            if file_age >= age_limit:
                                size = file_stat.st_size
                                os.remove(filepath)
                                purged_count += 1
                                reclaimed_bytes += size
                        except Exception as e:
                            logger.debug(f"[AutoHealer] Failed to remove {filepath}: {e}")
            except Exception as e:
                logger.warning(f"[AutoHealer] Error scanning directory {d}: {e}")

        return purged_count, reclaimed_bytes

    def trim_oversized_logs(self, max_bytes: int = 30 * 1024 * 1024) -> int:
        """
        Checks application log files and trims any exceeding max_bytes.
        Preserves the most recent 10,000 lines.
        Returns: bytes reclaimed.
        """
        reclaimed = 0
        log_files = [
            os.path.join(BASE_DIR, "admin.log"),
            os.path.join(BASE_DIR, "bot.log"),
        ]

        for log_path in log_files:
            if not os.path.exists(log_path):
                continue
            try:
                size = os.path.getsize(log_path)
                if size > max_bytes:
                    logger.info(
                        f"[AutoHealer] Log {log_path} ({size / (1024*1024):.1f}MB) exceeds {max_bytes / (1024*1024):.0f}MB limit. Trimming..."
                    )
                    # Read trailing lines
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    keep_lines = lines[-10000:]
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.writelines(keep_lines)
                    new_size = os.path.getsize(log_path)
                    diff = size - new_size
                    if diff > 0:
                        reclaimed += diff
            except Exception as e:
                logger.debug(f"[AutoHealer] Log trim notice for {log_path}: {e}")

        return reclaimed

    def reclaim_memory(self) -> float:
        """Forces Python Garbage Collection to reclaim unreferenced memory."""
        before = 0
        if psutil:
            try:
                before = psutil.Process(os.getpid()).memory_info().rss
            except Exception:
                pass

        gc.collect()

        after = 0
        if psutil:
            try:
                after = psutil.Process(os.getpid()).memory_info().rss
            except Exception:
                pass

        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before and after:
            freed_mb = max(0.0, (before - after) / (1024 * 1024))
            return round(freed_mb, 2)
        return 0.0

    def perform_heal(self, reason: str, force: bool = False, wa_cleanup_result: dict | None = None) -> dict:
        """
        Executes a multi-stage auto-heal and cache purge.
        Respects cooldown unless force is True.
        """
        now = time.time()
        if not force and (now - self.last_heal_time < self.heal_cooldown_sec):
            logger.debug(
                f"[AutoHealer] Cooldown active ({int(self.heal_cooldown_sec - (now - self.last_heal_time))}s left). Skipping heal."
            )
            return {"status": "cooldown", "reason": reason}

        logger.info(f"🩺 [AutoHealer] Engaging system auto-heal (Reason: {reason})...")

        # Telemetry before heal
        telem_before = self.get_telemetry()

        # Stage 1: Purge temporary media files
        files_purged, media_bytes_freed = self.purge_temp_media(
            max_age_seconds=0 if force else self.temp_file_max_age_sec
        )

        # Stage 2: Trim oversized logs
        log_bytes_freed = self.trim_oversized_logs()

        # Stage 3: Python GC memory reclaim
        gc_mb_freed = self.reclaim_memory()

        # Incorporate WhatsApp container cleanup if provided
        wa_purged = 0
        wa_bytes = 0
        if wa_cleanup_result and isinstance(wa_cleanup_result, dict):
            wa_purged = int(wa_cleanup_result.get("filesPurged", 0))
            wa_bytes = int(wa_cleanup_result.get("bytesReclaimed", 0))

        total_files_purged = files_purged + wa_purged
        total_bytes_freed = media_bytes_freed + log_bytes_freed + wa_bytes

        # Telemetry after heal
        telem_after = self.get_telemetry()
        self.last_heal_time = now
        self.last_heal_reason = reason

        action_summary = (
            f"Purged {total_files_purged} temp files (TG: {files_purged}, WA: {wa_purged}), "
            f"trimmed {round(log_bytes_freed / (1024*1024), 1)}MB logs, "
            f"ran GC ({gc_mb_freed}MB freed)"
        )

        # Record to SQLite database
        try:
            database.record_auto_heal_event(
                trigger_reason=reason,
                ram_percent=telem_before["host_ram_pct"],
                disk_percent=telem_before["disk_pct"],
                files_purged=total_files_purged,
                bytes_reclaimed=total_bytes_freed,
                action_taken=action_summary,
            )
        except Exception as db_err:
            logger.error(f"[AutoHealer] Failed to record heal event in DB: {db_err}")

        logger.info(
            f"✅ [AutoHealer] Completed auto-heal: {total_files_purged} files purged, "
            f"{round(total_bytes_freed / (1024*1024), 1)}MB disk reclaimed, "
            f"RAM: {telem_before['host_ram_pct']}% -> {telem_after['host_ram_pct']}%."
        )

        return {
            "status": "success",
            "reason": reason,
            "files_purged": total_files_purged,
            "bytes_reclaimed": total_bytes_freed,
            "mb_reclaimed": round(total_bytes_freed / (1024 * 1024), 1),
            "gc_mb_freed": gc_mb_freed,
            "ram_before": telem_before["host_ram_pct"],
            "ram_after": telem_after["host_ram_pct"],
            "action_taken": action_summary,
        }

    def check_and_heal_if_needed(self) -> dict | None:
        """
        Evaluates current system telemetry against configured safety thresholds.
        Triggers auto-heal if thresholds are breached.
        """
        if not self.is_enabled:
            return None

        telem = self.get_telemetry()
        heal_reason = None

        # 1. Critical Host RAM Check
        if telem["host_ram_pct"] >= self.ram_critical_percent:
            heal_reason = f"Host RAM Critical ({telem['host_ram_pct']}% >= {self.ram_critical_percent}%)"

        # 2. Critical Disk Space Check
        elif telem["disk_pct"] >= self.disk_warning_percent:
            heal_reason = f"Disk Space Warning ({telem['disk_pct']}% >= {self.disk_warning_percent}%)"

        # 3. Temp File Accumulation Check
        elif telem["media_count"] >= self.temp_files_threshold:
            heal_reason = (
                f"Temp Files Accumulation ({telem['media_count']} files >= {self.temp_files_threshold})"
            )

        if heal_reason:
            return self.perform_heal(reason=heal_reason, force=False)

        return None

    def format_dashboard_card(self, wa_memory_mb: int | None = None) -> str:
        """Formats an HTML status dashboard for Telegram admin display."""
        telem = self.get_telemetry()
        stats = database.get_auto_heal_stats()
        recent = database.get_recent_auto_heals(limit=3)

        status_badge = "🟢 <b>Active & Monitoring</b>" if self.is_enabled else "⏸️ <b>Paused</b>"
        ram_badge = "🔴" if telem["host_ram_pct"] >= self.ram_critical_percent else ("🟡" if telem["host_ram_pct"] >= self.ram_warning_percent else "🟢")
        disk_badge = "🔴" if telem["disk_pct"] >= self.disk_warning_percent else "🟢"
        files_badge = "🟡" if telem["media_count"] >= self.temp_files_threshold else "🟢"

        wa_str = f"{wa_memory_mb} MB" if wa_memory_mb is not None else "N/A"

        lines = [
            "🩺 <b>Automated System Health & RAM Auto-Healer</b>",
            f"• Engine Status: {status_badge}",
            f"• Auto-Check Interval: <code>Every 60s</code>",
            "",
            "📊 <b>Live System Telemetry</b>",
            f"• {ram_badge} Host RAM: <b>{telem['host_ram_pct']}%</b> ({telem['host_ram_used_gb']}GB / {telem['host_ram_total_gb']}GB)",
            f"• ⚡ CPU Utilization: <b>{telem['cpu_pct']}%</b>",
            f"• {disk_badge} Disk Usage: <b>{telem['disk_pct']}%</b> ({telem['disk_free_gb']}GB free of {telem['disk_total_gb']}GB)",
            f"• 🤖 Telegram Bot RAM: <b>{telem['bot_ram_mb']} MB</b>",
            f"• 🌐 Baileys Socket RAM: <b>{wa_str}</b>",
            f"• {files_badge} Temp Media Files: <b>{telem['media_count']} files</b> ({telem['media_size_mb']} MB)",
            "",
            "🛡️ <b>Safety Thresholds</b>",
            f"• RAM Critical: <code>{self.ram_critical_percent}%</code> (Auto-Purge & Reclaim)",
            f"• Disk Critical: <code>{self.disk_warning_percent}%</code>",
            f"• Temp Accumulation: <code>{self.temp_files_threshold} files</code>",
            "",
            "📈 <b>Lifetime Auto-Healer Statistics</b>",
            f"• Total Heals Performed: <b>{stats.get('total_events', 0)}</b>",
            f"• Total Files Purged: <b>{stats.get('total_files_purged', 0)}</b>",
            f"• Total Disk Space Reclaimed: <b>{stats.get('total_mb_reclaimed', 0.0)} MB</b>",
            f"• Last Auto-Heal Time: <code>{stats.get('last_heal_time') or 'Never'}</code>",
        ]

        if recent:
            lines.append("")
            lines.append("📋 <b>Recent Auto-Heal Actions</b>")
            for r in recent:
                lines.append(
                    f"• <code>{r['timestamp']}</code>: {r['trigger_reason']}\n"
                    f"  └ <i>{r['action_taken']}</i>"
                )

        return "\n".join(lines)
