import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")


def clean_id(channel_id):
    if not channel_id:
        return ""
    cid = str(channel_id).strip()
    if cid.lower().startswith("id:"):
        cid = cid[3:].strip()
    # Normalize numeric Telegram channel IDs:
    # If user provides e.g. 2568318126 without prefix, normalize to -1002568318126
    if cid.isdigit() and len(cid) >= 9:
        cid = f"-100{cid}"
    return cid


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def create_table():
    connection = get_connection()
    cursor = connection.cursor()

    # Table for Telegram channels
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY
        )
        """
    )

    # Table for WhatsApp groups mapped to channels
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            group_id TEXT,
            UNIQUE(channel_id, group_id)
        )
        """
    )

    # Index for fast channel lookup
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_groups_channel_id
        ON channel_groups(channel_id)
        """
    )

    # Migration / Synchronization (Fix 2.2):
    # Auto-migrate any channel_id existing in channel_groups that is missing in channels
    cursor.execute(
        """
        INSERT OR IGNORE INTO channels (channel_id)
        SELECT DISTINCT channel_id FROM channel_groups
        WHERE channel_id IS NOT NULL AND channel_id != ''
        """
    )

    # Add is_paused column to channels table if not already present (Optimization 4.A)
    try:
        cursor.execute("ALTER TABLE channels ADD COLUMN is_paused INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add last_post_at and created_at columns to channels table (Optimization 3.B)
    try:
        cursor.execute("ALTER TABLE channels ADD COLUMN last_post_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE channels ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass

    # Baseline existing channels: set last_post_at to CURRENT_TIMESTAMP if NULL
    cursor.execute("UPDATE channels SET last_post_at = CURRENT_TIMESTAMP WHERE last_post_at IS NULL")

    # Table for daily delivery metrics (Optimization 4.B)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_metrics (
            date_key TEXT PRIMARY KEY,
            text_count INTEGER DEFAULT 0,
            photo_count INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            doc_count INTEGER DEFAULT 0,
            audio_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            retried_success_count INTEGER DEFAULT 0,
            total_latency_ms REAL DEFAULT 0.0,
            latency_samples INTEGER DEFAULT 0
        )
        """
    )

    # Table for Dead-Letter Queue / Failed messages (Optimization 3.A)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS failed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            group_id TEXT,
            group_name TEXT,
            content_type TEXT,
            caption TEXT,
            original_filename TEXT,
            media_path TEXT,
            reason TEXT,
            file_size_bytes INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.commit()
    connection.close()


def add_channel(channel_id):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO channels (channel_id, last_post_at) VALUES (?, CURRENT_TIMESTAMP)", (channel_id,)
    )
    connection.commit()
    connection.close()


def add_group_for_channel(channel_id, group_id):
    channel_id = clean_id(channel_id)
    group_id = str(group_id).strip()
    if not channel_id or not group_id:
        return

    # Ensure channel exists in channels table (Fix 2.2)
    add_channel(channel_id)

    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO channel_groups (channel_id, group_id) VALUES (?, ?)",
        (channel_id, group_id),
    )
    connection.commit()
    connection.close()


def get_all_channels():
    connection = get_connection()
    cursor = connection.cursor()
    # Union guarantees every monitored channel is returned even if table desync occurred
    cursor.execute(
        """
        SELECT channel_id FROM channels
        UNION
        SELECT DISTINCT channel_id FROM channel_groups
        """
    )
    channels = cursor.fetchall()
    connection.close()
    return [clean_id(ch[0]) for ch in channels if clean_id(ch[0])]


def get_groups_for_channel(channel_id):
    channel_id = clean_id(channel_id)
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT group_id FROM channel_groups WHERE channel_id = ?", (channel_id,)
    )
    groups = cursor.fetchall()
    connection.close()
    return [group[0] for group in groups]


def delete_group_for_channel(channel_id, group_id):
    channel_id = clean_id(channel_id)
    group_id = str(group_id).strip()
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "DELETE FROM channel_groups WHERE channel_id = ? AND group_id = ?",
        (channel_id, group_id),
    )
    connection.commit()
    connection.close()


def delete_channel(channel_id):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    cursor.execute("DELETE FROM channel_groups WHERE channel_id = ?", (channel_id,))
    connection.commit()
    connection.close()


def is_channel_paused(channel_id: str) -> bool:
    channel_id = clean_id(channel_id)
    if not channel_id:
        return False
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT is_paused FROM channels WHERE channel_id = ?", (channel_id,))
    row = cursor.fetchone()
    connection.close()
    return bool(row and row[0] == 1)


def set_channel_paused(channel_id: str, paused: bool):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    add_channel(channel_id)
    val = 1 if paused else 0
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE channels SET is_paused = ? WHERE channel_id = ?",
        (val, channel_id),
    )
    connection.commit()
    connection.close()


def get_channel_details(channel_id: str) -> dict:
    channel_id = clean_id(channel_id)
    paused = is_channel_paused(channel_id)
    groups = get_groups_for_channel(channel_id)
    last_post = get_channel_last_post(channel_id)
    return {
        "channel_id": channel_id,
        "is_paused": paused,
        "groups": groups,
        "last_post_at": last_post,
    }


def get_active_channels_count() -> int:
    """
    Returns the count of active (non-paused) channels.
    """
    channels = get_all_channels()
    if not channels:
        return 0
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT channel_id FROM channels WHERE is_paused = 1")
    paused_ids = {clean_id(row[0]) for row in cursor.fetchall()}
    connection.close()
    return sum(1 for ch in channels if ch not in paused_ids)


def set_all_channels_paused(paused: bool) -> int:
    """
    Emergency control: Pauses or resumes all monitored channels in one atomic transaction.
    Returns the total number of channels affected.
    """
    channels = get_all_channels()
    if not channels:
        return 0
    val = 1 if paused else 0
    connection = get_connection()
    cursor = connection.cursor()
    for ch in channels:
        cursor.execute("INSERT OR IGNORE INTO channels (channel_id) VALUES (?)", (ch,))
    cursor.execute("UPDATE channels SET is_paused = ?", (val,))
    connection.commit()
    connection.close()
    return len(channels)



def record_delivery_metric(
    content_type: str,
    latency_ms: float = 0.0,
    retried: bool = False,
    failed: bool = False,
    date_str: str | None = None,
):
    """
    Records a delivery outcome into daily_metrics for today's date (or date_str).
    Uses atomic SQLite upsert.
    """
    from datetime import datetime

    today = date_str or datetime.now().strftime("%Y-%m-%d")
    ct = str(content_type).lower().strip()
    col = "text_count"
    if "photo" in ct:
        col = "photo_count"
    elif "video" in ct:
        col = "video_count"
    elif "doc" in ct:
        col = "doc_count"
    elif "audio" in ct or "voice" in ct:
        col = "audio_count"

    retried_val = 1 if (retried and not failed) else 0
    failed_val = 1 if failed else 0
    success_val = 0 if failed else 1
    latency_val = max(0.0, float(latency_ms))
    sample_val = 1 if (latency_val > 0 and not failed) else 0

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO daily_metrics (date_key) VALUES (?)",
        (today,),
    )

    query = f"""
        UPDATE daily_metrics
        SET {col} = {col} + ?,
            failed_count = failed_count + ?,
            retried_success_count = retried_success_count + ?,
            total_latency_ms = total_latency_ms + ?,
            latency_samples = latency_samples + ?
        WHERE date_key = ?
    """
    cursor.execute(
        query,
        (success_val, failed_val, retried_val, latency_val, sample_val, today),
    )
    connection.commit()
    connection.close()


def get_daily_metrics(date_str: str | None = None) -> dict:
    """
    Retrieves daily metrics summary for the given date (default today).
    """
    from datetime import datetime

    today = date_str or datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT text_count, photo_count, video_count, doc_count, audio_count,
               failed_count, retried_success_count, total_latency_ms, latency_samples
        FROM daily_metrics
        WHERE date_key = ?
        """,
        (today,),
    )
    row = cursor.fetchone()
    connection.close()

    if not row:
        return {
            "date": today,
            "text": 0,
            "photos": 0,
            "videos": 0,
            "docs": 0,
            "audio": 0,
            "total_forwarded": 0,
            "failed": 0,
            "retried_success": 0,
            "avg_latency_s": 0.0,
            "latency_samples": 0,
        }

    (
        text_c,
        photo_c,
        video_c,
        doc_c,
        audio_c,
        failed_c,
        retried_c,
        tot_lat,
        samples,
    ) = row

    total_fwd = text_c + photo_c + video_c + doc_c + audio_c
    avg_latency = (tot_lat / max(1, samples)) / 1000.0 if samples > 0 else 0.0

    return {
        "date": today,
        "text": text_c,
        "photos": photo_c,
        "videos": video_c,
        "docs": doc_c,
        "audio": audio_c,
        "total_forwarded": total_fwd,
        "failed": failed_c,
        "retried_success": retried_c,
        "avg_latency_s": round(avg_latency, 2),
        "latency_samples": samples,
    }


# ---------- Dead-Letter Queue (DLQ) Management (Optimization 3.A) ----------


def add_failed_message(
    channel_id: str,
    group_id: str,
    content_type: str,
    group_name: str | None = None,
    caption: str = "",
    original_filename: str | None = None,
    media_path: str | None = None,
    reason: str = "Unknown error",
    file_size_bytes: int = 0,
) -> int:
    """
    Inserts a failed message into the Dead-Letter Queue (DLQ).
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO failed_messages (
            channel_id, group_id, group_name, content_type,
            caption, original_filename, media_path, reason,
            file_size_bytes, retry_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            clean_id(channel_id),
            str(group_id),
            str(group_name) if group_name else "",
            str(content_type),
            str(caption or ""),
            str(original_filename) if original_filename else None,
            str(media_path) if media_path else None,
            str(reason or "Unknown error"),
            int(file_size_bytes or 0),
        ),
    )
    new_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return new_id


def get_failed_messages(limit: int = 50) -> list[dict]:
    """
    Retrieves queued failed messages from the DLQ ordered chronologically.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id, channel_id, group_id, group_name, content_type,
               caption, original_filename, media_path, reason,
               file_size_bytes, retry_count, created_at
        FROM failed_messages
        ORDER BY id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    connection.close()
    return [
        {
            "id": r[0],
            "channel_id": r[1],
            "group_id": r[2],
            "group_name": r[3],
            "content_type": r[4],
            "caption": r[5],
            "original_filename": r[6],
            "media_path": r[7],
            "reason": r[8],
            "file_size_bytes": r[9],
            "retry_count": r[10],
            "created_at": r[11],
        }
        for r in rows
    ]


def delete_failed_message(msg_id: int):
    """
    Removes a single recovered message from the DLQ by ID.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM failed_messages WHERE id = ?", (msg_id,))
    connection.commit()
    connection.close()


def clear_failed_messages():
    """
    Purges all records from the Dead-Letter Queue.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM failed_messages")
    connection.commit()
    connection.close()


def get_failed_messages_count() -> int:
    """
    Returns the total number of items currently in the Dead-Letter Queue.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM failed_messages")
    count = cursor.fetchone()[0]
    connection.close()
    return count


# ---------- Stale Channel Detection (Optimization 3.B) ----------


def update_channel_last_post(channel_id: str, post_time: str | None = None):
    """
    Updates the last post timestamp for a monitored Telegram channel.
    """
    from datetime import datetime

    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    now_str = post_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE channels SET last_post_at = ? WHERE channel_id = ?",
        (now_str, channel_id),
    )
    connection.commit()
    connection.close()


def get_channel_last_post(channel_id: str) -> str | None:
    """
    Retrieves the last post timestamp for a specific channel.
    """
    channel_id = clean_id(channel_id)
    if not channel_id:
        return None
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT last_post_at FROM channels WHERE channel_id = ?",
        (channel_id,),
    )
    row = cursor.fetchone()
    connection.close()
    return row[0] if row and row[0] else None


def get_stale_channels(threshold_hours: int = 72) -> list[dict]:
    """
    Returns all monitored channels that have had no posts for more than threshold_hours.
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    cutoff = (now - timedelta(hours=threshold_hours)).strftime("%Y-%m-%d %H:%M:%S")

    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT channel_id, last_post_at, is_paused
        FROM channels
        WHERE last_post_at IS NOT NULL AND last_post_at <= ?
        ORDER BY last_post_at ASC
        """,
        (cutoff,),
    )
    rows = cursor.fetchall()
    connection.close()

    stale_list = []
    for r in rows:
        cid, last_post, is_paused = r
        try:
            lp_dt = datetime.strptime(str(last_post).split(".")[0], "%Y-%m-%d %H:%M:%S")
            diff_hours = int((now - lp_dt).total_seconds() // 3600)
        except Exception:
            diff_hours = threshold_hours

        groups = get_groups_for_channel(cid)
        stale_list.append(
            {
                "channel_id": cid,
                "last_post_at": last_post,
                "hours_inactive": diff_hours,
                "is_paused": bool(is_paused),
                "groups_count": len(groups),
            }
        )

    return stale_list


create_table()


