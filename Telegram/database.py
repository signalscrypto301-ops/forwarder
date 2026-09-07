import sqlite3


def clean_id(channel_id):
    if not channel_id:
        return ""
    cid = str(channel_id).strip()
    if cid.lower().startswith("id:"):
        cid = cid[3:].strip()
    return cid


def create_table():
    connection = sqlite3.connect("database.db")
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

    connection.commit()
    connection.close()


def add_channel(channel_id):
    channel_id = clean_id(channel_id)
    if not channel_id:
        return
    connection = sqlite3.connect("database.db")
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM channels WHERE channel_id = ?", (channel_id,))
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO channels (channel_id) VALUES (?)", (channel_id,))
        connection.commit()
    connection.close()


def add_group_for_channel(channel_id, group_id):
    channel_id = clean_id(channel_id)
    group_id = str(group_id).strip()
    if not channel_id or not group_id:
        return
    connection = sqlite3.connect("database.db")
    cursor = connection.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM channel_groups WHERE channel_id = ? AND group_id = ?",
        (channel_id, group_id),
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO channel_groups (channel_id, group_id) VALUES (?, ?)",
            (channel_id, group_id),
        )
        connection.commit()
    connection.close()


def get_all_channels():
    connection = sqlite3.connect("database.db")
    cursor = connection.cursor()
    cursor.execute("SELECT channel_id FROM channels")
    channels = cursor.fetchall()
    connection.close()
    return [clean_id(ch[0]) for ch in channels if clean_id(ch[0])]


def get_groups_for_channel(channel_id):
    channel_id = clean_id(channel_id)
    connection = sqlite3.connect("database.db")
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
    connection = sqlite3.connect("database.db")
    cursor = connection.cursor()
    cursor.execute(
        "DELETE FROM channel_groups WHERE channel_id = ? AND group_id = ?",
        (channel_id, group_id),
    )
    connection.commit()
    connection.close()


create_table()
