import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")


def clean_id(channel_id):
    if not channel_id:
        return ""
    cid = str(channel_id).strip()
    # Strip any leading/trailing angle brackets, quotes, or whitespace
    cid = cid.strip("<>\"' \t\r\n")
    if cid.lower().startswith("id:"):
        cid = cid[3:].strip()
    # Normalize numeric Telegram channel IDs:
    # If user provides e.g. 2568318126 without prefix, normalize to -1002568318126
    if cid.isdigit() and len(cid) >= 9:
        cid = f"-100{cid}"
    return cid


def clean_destination_id(dest_id):
    if not dest_id:
        return ""
    did = str(dest_id).strip()
    # Strip any leading/trailing angle brackets, quotes, or whitespace
    did = did.strip("<>\"' \t\r\n")
    return did


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

    # Auto-clean legacy malformed angle bracket rows from channel_groups and channels
    cursor.execute("DELETE FROM channel_groups WHERE channel_id LIKE '%<%' OR group_id LIKE '%<%'")
    cursor.execute("DELETE FROM channels WHERE channel_id LIKE '%<%'")

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

    try:
        cursor.execute("ALTER TABLE channels ADD COLUMN title TEXT")
    except sqlite3.OperationalError:
        pass

    # Baseline existing channels: set last_post_at to CURRENT_TIMESTAMP if NULL
    cursor.execute("UPDATE channels SET last_post_at = CURRENT_TIMESTAMP WHERE last_post_at IS NULL")

    # Clean any accidental mock titles or malformed values from channels table
    cursor.execute("UPDATE channels SET title = NULL WHERE title LIKE '<AsyncMock%' OR title LIKE '<MagicMock%'")

    # Table for cached human-readable WhatsApp destination titles
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS destination_titles (
            destination_id TEXT PRIMARY KEY,
            title TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

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

    # Table for permanently failed / archived DLQ records
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS archived_failures (
            id INTEGER PRIMARY KEY,
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
            created_at TIMESTAMP,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_failed_messages_retry_count
        ON failed_messages(retry_count)
        """
    )

    # Table for per-channel hourly traffic and audience analytics (Optimization 6.A)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_hourly_stats (
            channel_id TEXT NOT NULL,
            date_key TEXT NOT NULL,
            hour_key INTEGER NOT NULL,
            post_count INTEGER DEFAULT 1,
            PRIMARY KEY (channel_id, date_key, hour_key)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hourly_date ON channel_hourly_stats(date_key)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hourly_channel ON channel_hourly_stats(channel_id)
        """
    )

    # Table for Auto-Healer audit logs
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_heal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trigger_reason TEXT NOT NULL,
            ram_percent REAL,
            disk_percent REAL,
            files_purged INTEGER DEFAULT 0,
            bytes_reclaimed INTEGER DEFAULT 0,
            action_taken TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_heal_logs_timestamp
        ON auto_heal_logs(timestamp)
        """
    )

    # Table for Real-Time Content Sync: Message ID mappings (Telegram -> WhatsApp)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS forwarded_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            tg_message_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            wa_message_id TEXT,
            wa_key_json TEXT,
            sender_id TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fwd_lookup
        ON forwarded_messages(channel_id, tg_message_id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fwd_created_at
        ON forwarded_messages(created_at)
        """
    )

    connection.commit()
    connection.close()


def add_channel(channel_id, title=None):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    connection = get_connection()
    cursor = connection.cursor()
    if title and str(title).strip():
        cursor.execute(
            "INSERT INTO channels (channel_id, last_post_at, title) VALUES (?, CURRENT_TIMESTAMP, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET title = COALESCE(excluded.title, channels.title)",
            (channel_id, str(title).strip()),
        )
    else:
        cursor.execute(
            "INSERT OR IGNORE INTO channels (channel_id, last_post_at) VALUES (?, CURRENT_TIMESTAMP)", (channel_id,)
        )
    connection.commit()
    connection.close()


def update_channel_title(channel_id, title):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    clean_title = str(title).strip() if title else None
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("INSERT OR IGNORE INTO channels (channel_id) VALUES (?)", (channel_id,))
    cursor.execute("UPDATE channels SET title = ? WHERE channel_id = ?", (clean_title, channel_id))
    connection.commit()
    connection.close()


def get_channel_title(channel_id) -> str | None:
    channel_id = clean_id(channel_id)
    if not channel_id:
        return None
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT title FROM channels WHERE channel_id = ?", (channel_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return None
        val = str(row[0]).strip()
        if val.startswith("<AsyncMock") or val.startswith("<MagicMock"):
            return None
        return val if val else None
    except Exception:
        return None
    finally:
        connection.close()


def get_channels_without_title() -> list[str]:
    """Returns list of monitored Telegram channel IDs that do not currently have a resolved title in DB."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT channel_id FROM channels
            WHERE title IS NULL OR TRIM(title) = '' OR title LIKE '<AsyncMock%' OR title LIKE '<MagicMock%'
            UNION
            SELECT DISTINCT cg.channel_id FROM channel_groups cg
            LEFT JOIN channels c ON cg.channel_id = c.channel_id
            WHERE c.title IS NULL OR TRIM(c.title) = '' OR c.title LIKE '<AsyncMock%' OR c.title LIKE '<MagicMock%'
            """
        )
        rows = cursor.fetchall()
        return [clean_id(r[0]) for r in rows if clean_id(r[0])]
    except Exception:
        return []
    finally:
        connection.close()


def get_channels_for_group(group_id: str) -> list[str]:
    group_id = clean_destination_id(group_id)
    if not group_id:
        return []
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT DISTINCT channel_id FROM channel_groups 
        WHERE group_id = ? 
           OR group_id = ? 
           OR REPLACE(REPLACE(group_id, '<', ''), '>', '') = ?
        ORDER BY channel_id ASC
        """,
        (group_id, f"<{group_id}>", group_id),
    )
    rows = cursor.fetchall()
    connection.close()
    return [clean_id(r[0]) for r in rows if clean_id(r[0])]


def update_destination_title(destination_id: str, title: str) -> None:
    if not destination_id or not title:
        return
    did = clean_destination_id(destination_id)
    t = str(title).strip()
    if not did or not t or t == did:
        return
    if t.endswith("@newsletter") or t.endswith("@g.us") or t.isdigit():
        return
    if t.startswith("<AsyncMock") or t.startswith("<MagicMock"):
        return

    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO destination_titles (destination_id, title, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(destination_id) DO UPDATE SET
            title = excluded.title,
            updated_at = CURRENT_TIMESTAMP
        """,
        (did, t),
    )
    connection.commit()
    connection.close()


def get_destination_title(destination_id: str) -> str | None:
    if not destination_id:
        return None
    did = clean_destination_id(destination_id)
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT title FROM destination_titles WHERE destination_id = ?",
            (did,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            val = str(row[0]).strip()
            if not val.startswith("<AsyncMock") and not val.startswith("<MagicMock"):
                return val
        return None
    except Exception:
        return None
    finally:
        connection.close()


def get_all_destination_titles() -> dict[str, str]:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT destination_id, title FROM destination_titles WHERE title IS NOT NULL")
        rows = cursor.fetchall()
        return {r[0]: r[1] for r in rows if r[0] and r[1]}
    except Exception:
        return {}
    finally:
        connection.close()


def get_destination_names_map() -> dict[str, str]:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT cg.group_id, c.title, c.channel_id
            FROM channel_groups cg
            LEFT JOIN channels c ON cg.channel_id = c.channel_id
            WHERE cg.group_id IS NOT NULL AND TRIM(cg.group_id) != ''
            """
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []
    finally:
        connection.close()

    result = {}
    for gid, title, cid in rows:
        clean_gid = clean_destination_id(gid)
        if clean_gid:
            t = str(title).strip() if title and str(title).strip() else None
            val = t or (clean_id(cid) if cid else None)
            if val:
                result[clean_gid] = val
                result[f"<{clean_gid}>"] = val

    # Override with genuine WhatsApp destination titles if available
    try:
        dest_titles = get_all_destination_titles()
        for gid, dt in dest_titles.items():
            if dt and not dt.endswith("@newsletter") and not dt.endswith("@g.us"):
                result[gid] = dt
                result[f"<{gid}>"] = dt
    except Exception:
        pass

    return result


def add_group_for_channel(channel_id, group_id):
    channel_id = clean_id(channel_id)
    group_id = clean_destination_id(group_id)
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


def get_channels_overview() -> list[dict]:
    """
    Consolidates channel metadata (channel_id, is_paused, group_count)
    into a single relational query to eliminate N+1 query storms in menus.
    Returns:
    [
        {"channel_id": str, "is_paused": bool, "group_count": int},
        ...
    ]
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT 
            ch.channel_id,
            COALESCE(c.is_paused, 0) AS is_paused,
            COUNT(DISTINCT g.group_id) AS group_count,
            c.title
        FROM (
            SELECT channel_id FROM channels
            UNION
            SELECT channel_id FROM channel_groups
        ) ch
        LEFT JOIN channels c ON ch.channel_id = c.channel_id
        LEFT JOIN channel_groups g ON ch.channel_id = g.channel_id
        GROUP BY ch.channel_id
        ORDER BY ch.channel_id ASC
        """
    )
    rows = cursor.fetchall()
    connection.close()

    result = []
    for r in rows:
        cid = clean_id(r[0])
        if cid:
            result.append({
                "channel_id": cid,
                "is_paused": bool(r[1] == 1),
                "group_count": int(r[2]),
                "title": str(r[3]).strip() if r[3] and str(r[3]).strip() else None,
            })
    return result


def get_groups_for_channel(channel_id):
    channel_id = clean_id(channel_id)
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT group_id FROM channel_groups WHERE channel_id = ?", (channel_id,)
    )
    groups = cursor.fetchall()
    connection.close()
    return [clean_destination_id(group[0]) for group in groups if clean_destination_id(group[0])]


def get_all_unique_destinations() -> list[str]:
    """
    Returns all unique WhatsApp group and newsletter JIDs currently mapped for forwarding.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT DISTINCT group_id FROM channel_groups WHERE group_id IS NOT NULL AND TRIM(group_id) != '' ORDER BY group_id ASC"
    )
    rows = cursor.fetchall()
    connection.close()
    return [clean_destination_id(r[0]) for r in rows if r[0] and clean_destination_id(r[0])]


def delete_group_for_channel(channel_id, group_id):
    channel_id = clean_id(channel_id)
    group_id = clean_destination_id(group_id)
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


def unlink_channel(channel_id: str, group_id: str | None = None) -> int:
    """
    Unlinks destination WhatsApp group(s) from a Telegram channel.
    - If group_id is specified: removes only that specific mapping.
    - If group_id is None/omitted: removes all mapped groups for this channel.
    The channel itself remains registered in the channels table.
    Returns the number of unlinked group mappings.
    """
    channel_id = str(channel_id).strip()
    if not channel_id:
        return 0
    cid_norm = clean_id(channel_id)
    cid_raw = channel_id[4:] if channel_id.startswith("-100") else channel_id
    connection = get_connection()
    cursor = connection.cursor()
    if group_id:
        gid = str(group_id).strip()
        cursor.execute(
            "DELETE FROM channel_groups WHERE (channel_id = ? OR channel_id = ?) AND group_id = ?",
            (cid_norm, cid_raw, gid),
        )
    else:
        cursor.execute(
            "DELETE FROM channel_groups WHERE channel_id = ? OR channel_id = ?",
            (cid_norm, cid_raw),
        )
    count = cursor.rowcount
    connection.commit()
    connection.close()
    return count


def deactivate_channel(channel_id: str) -> bool:
    """Deactivates/pauses message forwarding for a specific channel."""
    set_channel_paused(channel_id, True)
    return True


def activate_channel(channel_id: str) -> bool:
    """Activates/resumes message forwarding for a specific channel."""
    set_channel_paused(channel_id, False)
    return True


def is_channel_paused(channel_id: str) -> bool:
    channel_id = str(channel_id).strip()
    if not channel_id:
        return False
    cid_norm = clean_id(channel_id)
    cid_raw = channel_id[4:] if channel_id.startswith("-100") else channel_id
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT is_paused FROM channels WHERE channel_id = ? OR channel_id = ?",
        (cid_norm, cid_raw),
    )
    row = cursor.fetchone()
    connection.close()
    return bool(row and row[0] == 1)


def set_channel_paused(channel_id: str, paused: bool):
    channel_id = str(channel_id).strip()
    if not channel_id:
        return
    cid_norm = clean_id(channel_id)
    cid_raw = channel_id[4:] if channel_id.startswith("-100") else channel_id
    add_channel(cid_norm)
    val = 1 if paused else 0
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE channels SET is_paused = ? WHERE channel_id = ? OR channel_id = ?",
        (val, cid_norm, cid_raw),
    )
    connection.commit()
    connection.close()


def get_channel_details(channel_id: str) -> dict:
    channel_id = clean_id(channel_id)
    paused = is_channel_paused(channel_id)
    groups = get_groups_for_channel(channel_id)
    last_post = get_channel_last_post(channel_id)
    title = get_channel_title(channel_id)
    return {
        "channel_id": channel_id,
        "title": title,
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


def get_failed_messages(limit: int = 50, max_retries: int = 3) -> list[dict]:
    """
    Retrieves queued failed messages from the DLQ ordered by retry_count ASC, id ASC.
    Only retrieves active items that have not exceeded max_retries.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id, channel_id, group_id, group_name, content_type,
               caption, original_filename, media_path, reason,
               file_size_bytes, retry_count, created_at
        FROM failed_messages
        WHERE retry_count < ?
        ORDER BY retry_count ASC, id ASC
        LIMIT ?
        """,
        (max_retries, limit),
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


def increment_failed_message_retry(
    msg_id: int, reason: str | None = None, max_retries: int = 3
) -> int:
    """
    Increments retry_count for a failed message.
    If retry_count reaches or exceeds max_retries, moves the record to archived_failures
    and deletes it from failed_messages. Unlinks associated media_path if unrecoverable.
    Returns the updated retry_count.
    """
    connection = get_connection()
    cursor = connection.cursor()

    if reason:
        cursor.execute(
            "UPDATE failed_messages SET retry_count = retry_count + 1, reason = ? WHERE id = ?",
            (str(reason), msg_id),
        )
    else:
        cursor.execute(
            "UPDATE failed_messages SET retry_count = retry_count + 1 WHERE id = ?",
            (msg_id,),
        )

    cursor.execute(
        """
        SELECT id, channel_id, group_id, group_name, content_type,
               caption, original_filename, media_path, reason,
               file_size_bytes, retry_count, created_at
        FROM failed_messages WHERE id = ?
        """,
        (msg_id,),
    )
    row = cursor.fetchone()

    new_count = 0
    if row:
        new_count = row[10]
        if new_count >= max_retries:
            # Archive permanently failed message
            cursor.execute(
                """
                INSERT OR REPLACE INTO archived_failures (
                    id, channel_id, group_id, group_name, content_type,
                    caption, original_filename, media_path, reason,
                    file_size_bytes, retry_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            cursor.execute("DELETE FROM failed_messages WHERE id = ?", (msg_id,))

            # Clean up unrecoverable DLQ media file if on disk
            media_path = row[7]
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except Exception:
                    pass

    connection.commit()
    connection.close()
    return new_count


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


def get_failed_messages_count(max_retries: int = 3) -> int:
    """
    Returns the total number of active pending items currently in the Dead-Letter Queue (retry_count < max_retries).
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM failed_messages WHERE retry_count < ?", (max_retries,))
    count = cursor.fetchone()[0]
    connection.close()
    return count


def get_archived_failures_count() -> int:
    """
    Returns the total number of permanently failed / archived items.
    """
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM archived_failures")
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
        SELECT channel_id, last_post_at, is_paused, title
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
        cid = r[0]
        last_post = r[1]
        is_paused = r[2]
        title = r[3] if len(r) > 3 and r[3] else None
        try:
            lp_dt = datetime.strptime(str(last_post).split(".")[0], "%Y-%m-%d %H:%M:%S")
            diff_hours = int((now - lp_dt).total_seconds() // 3600)
        except Exception:
            diff_hours = threshold_hours

        groups = get_groups_for_channel(cid)
        stale_list.append(
            {
                "channel_id": cid,
                "title": title,
                "last_post_at": last_post,
                "hours_inactive": diff_hours,
                "is_paused": bool(is_paused),
                "groups": groups,
                "groups_count": len(groups),
            }
        )

    return stale_list


def record_channel_post_activity(channel_id: str, timestamp=None):
    """
    Records and increments post volume for a specific channel within its hourly bucket.
    """
    from datetime import datetime

    channel_id = clean_id(channel_id)
    if not channel_id:
        return

    dt = timestamp or datetime.now()
    date_key = dt.strftime("%Y-%m-%d")
    hour_key = dt.hour

    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO channel_hourly_stats (channel_id, date_key, hour_key, post_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(channel_id, date_key, hour_key)
        DO UPDATE SET post_count = post_count + 1
        """,
        (channel_id, date_key, hour_key),
    )
    connection.commit()
    connection.close()


def get_top_active_channels(date_key: str | None = None, limit: int = 5) -> list[dict]:
    """
    Returns top active channels ranked by post count for a given date (defaults to today).
    Includes channel_id, post_count, percentage of total volume, and mapped groups count.
    """
    from datetime import datetime

    target_date = date_key or datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT COALESCE(SUM(post_count), 0) FROM channel_hourly_stats WHERE date_key = ?",
        (target_date,),
    )
    total_row = cursor.fetchone()
    total_posts = total_row[0] if total_row else 0

    cursor.execute(
        """
        SELECT channel_id, SUM(post_count) as total
        FROM channel_hourly_stats
        WHERE date_key = ?
        GROUP BY channel_id
        ORDER BY total DESC
        LIMIT ?
        """,
        (target_date, limit),
    )
    rows = cursor.fetchall()
    connection.close()

    result = []
    for rank, (cid, cnt) in enumerate(rows, 1):
        pct = round((cnt / total_posts * 100.0), 1) if total_posts > 0 else 0.0
        groups = get_groups_for_channel(cid)
        result.append(
            {
                "rank": rank,
                "channel_id": cid,
                "post_count": cnt,
                "percent": pct,
                "groups_count": len(groups),
            }
        )
    return result


def get_hourly_traffic_distribution(date_key: str | None = None) -> list[dict]:
    """
    Returns hourly post counts for all 24 hours (00:00 to 23:00) for a given date.
    Returns a list of 24 items: [{"hour": 0, "label": "00:00", "post_count": N, "is_peak": bool}, ...]
    """
    from datetime import datetime

    target_date = date_key or datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT hour_key, SUM(post_count)
        FROM channel_hourly_stats
        WHERE date_key = ?
        GROUP BY hour_key
        """,
        (target_date,),
    )
    rows = dict(cursor.fetchall())
    connection.close()

    max_count = max(rows.values()) if rows else 0
    distribution = []
    for h in range(24):
        cnt = rows.get(h, 0)
        distribution.append(
            {
                "hour": h,
                "label": f"{h:02d}:00",
                "post_count": cnt,
                "is_peak": (cnt == max_count and cnt > 0),
            }
        )
    return distribution


def get_channel_volume_summary(date_key: str | None = None) -> dict:
    """
    Returns aggregated traffic summary for a given date:
    total posts, active channels count, peak hour, peak hour volume.
    """
    from datetime import datetime

    target_date = date_key or datetime.now().strftime("%Y-%m-%d")
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT 
            COALESCE(SUM(post_count), 0),
            COUNT(DISTINCT channel_id)
        FROM channel_hourly_stats
        WHERE date_key = ?
        """,
        (target_date,),
    )
    row = cursor.fetchone()
    total_posts = row[0] if row else 0
    active_channels = row[1] if row else 0

    cursor.execute(
        """
        SELECT hour_key, SUM(post_count) as total
        FROM channel_hourly_stats
        WHERE date_key = ?
        GROUP BY hour_key
        ORDER BY total DESC
        LIMIT 1
        """,
        (target_date,),
    )
    peak_row = cursor.fetchone()
    connection.close()

    peak_hour = peak_row[0] if peak_row else None
    peak_count = peak_row[1] if peak_row else 0

    return {
        "date_key": target_date,
        "total_posts": total_posts,
        "active_channels": active_channels,
        "peak_hour": peak_hour,
        "peak_hour_label": f"{peak_hour:02d}:00" if peak_hour is not None else "N/A",
        "peak_hour_volume": peak_count,
    }


def record_auto_heal_event(
    trigger_reason: str,
    ram_percent: float,
    disk_percent: float,
    files_purged: int = 0,
    bytes_reclaimed: int = 0,
    action_taken: str = "",
) -> int:
    """Records an automated or manual heal/purge event to SQLite."""
    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO auto_heal_logs (
            timestamp, trigger_reason, ram_percent, disk_percent,
            files_purged, bytes_reclaimed, action_taken
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_str,
            str(trigger_reason),
            float(ram_percent),
            float(disk_percent),
            int(files_purged),
            int(bytes_reclaimed),
            str(action_taken),
        ),
    )
    new_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return new_id


def get_recent_auto_heals(limit: int = 10) -> list:
    """Returns the most recent auto-heal audit records."""
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id, timestamp, trigger_reason, ram_percent, disk_percent,
               files_purged, bytes_reclaimed, action_taken
        FROM auto_heal_logs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    connection.close()
    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "timestamp": r[1],
            "trigger_reason": r[2],
            "ram_percent": r[3],
            "disk_percent": r[4],
            "files_purged": r[5],
            "bytes_reclaimed": r[6],
            "action_taken": r[7],
        })
    return results


def get_auto_heal_stats() -> dict:
    """Computes lifetime auto-heal summary statistics."""
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT 
            COUNT(*),
            COALESCE(SUM(files_purged), 0),
            COALESCE(SUM(bytes_reclaimed), 0),
            MAX(timestamp)
        FROM auto_heal_logs
        """
    )
    row = cursor.fetchone()
    connection.close()
    total_events = row[0] if row else 0
    total_files = row[1] if row else 0
    total_bytes = row[2] if row else 0
    last_heal_time = row[3] if row and row[3] else None
    return {
        "total_events": total_events,
        "total_files_purged": total_files,
        "total_bytes_reclaimed": total_bytes,
        "total_mb_reclaimed": round(total_bytes / (1024 * 1024), 1),
        "last_heal_time": last_heal_time,
    }


def record_forwarded_message(
    channel_id: str,
    tg_message_id: int,
    group_id: str,
    wa_message_id: str | None = None,
    wa_key_json: str | dict | None = None,
    sender_id: str = "user",
    wa_key: str | dict | None = None,
):
    """Records a mapping between a Telegram channel message and a dispatched WhatsApp message."""
    channel_id = clean_id(channel_id)
    if not channel_id or not tg_message_id or not group_id:
        return
    import json
    if wa_key is not None and wa_key_json is None:
        wa_key_json = wa_key
    if isinstance(wa_key_json, dict):
        wa_key_json = json.dumps(wa_key_json)

    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO forwarded_messages (channel_id, tg_message_id, group_id, wa_message_id, wa_key_json, sender_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (channel_id, int(tg_message_id), str(group_id), wa_message_id, wa_key_json, sender_id),
    )
    connection.commit()
    connection.close()


def get_forwarded_messages(channel_id: str, tg_message_id: int) -> list[dict]:
    """Retrieves all WhatsApp message records mapped to a Telegram channel post."""
    channel_id = clean_id(channel_id)
    if not channel_id or not tg_message_id:
        return []
    import json
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT group_id, wa_message_id, wa_key_json, sender_id, created_at
        FROM forwarded_messages
        WHERE channel_id = ? AND tg_message_id = ?
        ORDER BY id ASC
        """,
        (channel_id, int(tg_message_id)),
    )
    rows = cursor.fetchall()
    connection.close()

    results = []
    for r in rows:
        key_obj = None
        if r[2]:
            try:
                key_obj = json.loads(r[2])
            except Exception:
                key_obj = r[2]
        results.append({
            "group_id": r[0],
            "wa_message_id": r[1],
            "wa_key": key_obj,
            "wa_key_json": r[2],
            "sender_id": r[3] or "user",
            "created_at": r[4],
        })
    return results


def delete_forwarded_message_records(channel_id: str, tg_message_id: int) -> int:
    """Removes mappings for a deleted Telegram post."""
    channel_id = clean_id(channel_id)
    if not channel_id or not tg_message_id:
        return 0
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        "DELETE FROM forwarded_messages WHERE channel_id = ? AND tg_message_id = ?",
        (channel_id, int(tg_message_id)),
    )
    deleted_cnt = cursor.rowcount
    connection.commit()
    connection.close()
    return deleted_cnt


def prune_old_forwarded_messages(max_age_days: int = 14) -> int:
    """Prunes forwarded message mappings older than max_age_days to conserve disk space."""
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        f"DELETE FROM forwarded_messages WHERE created_at < datetime('now', '-{int(max_age_days)} days')"
    )
    deleted_count = cursor.rowcount
    connection.commit()
    connection.close()
    return deleted_count


create_table()


