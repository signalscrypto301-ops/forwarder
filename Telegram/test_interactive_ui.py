import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

# Mock third-party libraries if not installed in host Python environment
for mod in ["yaml", "aiogram", "aiogram.utils", "aiogram.utils.executor", "aiogram.types", "telethon", "telethon.sessions", "qrcode"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = MagicMock()

# Define real minimal classes for InlineKeyboardMarkup/InlineKeyboardButton for assertion testing
class MockInlineKeyboardMarkup:
    def __init__(self, row_width=1):
        self.inline_keyboard = []
        self.row_width = row_width
    def add(self, *buttons):
        for b in buttons:
            self.inline_keyboard.append([b])
    def row(self, *buttons):
        self.inline_keyboard.append(list(buttons))

class MockInlineKeyboardButton:
    def __init__(self, text="", callback_data=""):
        self.text = text
        self.callback_data = callback_data

aiogram_mod = sys.modules.get("aiogram")
if aiogram_mod:
    aiogram_mod.types.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_mod.types.InlineKeyboardButton = MockInlineKeyboardButton

aiogram_types_mod = sys.modules.get("aiogram.types")
if aiogram_types_mod:
    aiogram_types_mod.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    aiogram_types_mod.InlineKeyboardButton = MockInlineKeyboardButton

from database import (
    add_channel,
    delete_channel,
    add_group_for_channel,
    get_channels_overview,
    is_channel_paused,
    set_channel_paused,
    get_channel_details,
    clean_id,
)
from bot import (
    build_channels_keyboard,
    build_channel_detail_keyboard,
    build_delete_confirm_keyboard,
)


def test_database_pause_resume():
    test_ch = clean_id("9999999999")
    print(f"Testing database pause/resume for {test_ch}...")

    # Ensure clean state
    delete_channel(test_ch)
    add_channel(test_ch)
    add_group_for_channel(test_ch, "test-group@g.us")

    # Initial state should not be paused
    assert is_channel_paused(test_ch) is False, "Channel should initially not be paused"

    # Pause channel
    set_channel_paused(test_ch, True)
    assert is_channel_paused(test_ch) is True, "Channel should be paused after set_channel_paused(True)"

    # Check details
    details = get_channel_details(test_ch)
    assert details["channel_id"] == test_ch
    assert details["is_paused"] is True
    assert "test-group@g.us" in details["groups"]

    # Resume channel
    set_channel_paused(test_ch, False)
    assert is_channel_paused(test_ch) is False, "Channel should be resumed after set_channel_paused(False)"

    # Clean up
    delete_channel(test_ch)
    print("[OK] Database pause/resume state operations verified!")


def test_inline_keyboards():
    print("Testing inline keyboard builders...")
    test_ch = clean_id("8888888888")
    add_channel(test_ch)
    add_group_for_channel(test_ch, "news@newsletter")

    # 1. Channels list keyboard
    text, kb = build_channels_keyboard(page=1, per_page=6)
    assert "Monitored Channels Management" in text
    assert len(kb.inline_keyboard) > 0, "Keyboard should contain buttons"

    # 2. Channel detail keyboard (active)
    text_active, kb_active = build_channel_detail_keyboard(test_ch, page=1)
    assert "ACTIVE" in text_active
    assert "news@newsletter" in text_active
    assert any("Pause" in btn.text for row in kb_active.inline_keyboard for btn in row)

    # 3. Channel detail keyboard (paused)
    set_channel_paused(test_ch, True)
    text_paused, kb_paused = build_channel_detail_keyboard(test_ch, page=1)
    assert "PAUSED" in text_paused
    assert any("Resume" in btn.text for row in kb_paused.inline_keyboard for btn in row)

    # 4. Delete confirm keyboard
    text_del, kb_del = build_delete_confirm_keyboard(test_ch, page=1)
    assert "Confirm Channel Deletion" in text_del
    assert any("Yes, Delete" in btn.text for row in kb_del.inline_keyboard for btn in row)

    # Clean up
    delete_channel(test_ch)
    print("[OK] Interactive inline keyboard builders verified!")


def test_channels_overview_consolidation():
    print("Testing get_channels_overview consolidation & single query menu...")
    ch1 = clean_id("7777000001")
    ch2 = clean_id("7777000002")
    ch3 = clean_id("7777000003")

    for ch in [ch1, ch2, ch3]:
        delete_channel(ch)
        add_channel(ch)

    add_group_for_channel(ch1, "grp1@g.us")
    add_group_for_channel(ch1, "grp2@g.us")
    add_group_for_channel(ch2, "grp3@g.us")
    set_channel_paused(ch2, True)

    overview = get_channels_overview()
    overview_map = {item["channel_id"]: item for item in overview}

    assert ch1 in overview_map, f"{ch1} missing from overview"
    assert overview_map[ch1]["is_paused"] is False
    assert overview_map[ch1]["group_count"] == 2

    assert ch2 in overview_map, f"{ch2} missing from overview"
    assert overview_map[ch2]["is_paused"] is True
    assert overview_map[ch2]["group_count"] == 1

    assert ch3 in overview_map, f"{ch3} missing from overview"
    assert overview_map[ch3]["is_paused"] is False
    assert overview_map[ch3]["group_count"] == 0

    text, kb = build_channels_keyboard(page=1, per_page=1000)
    btn_texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any(f"🟢 {ch1} (2 grps)" in t for t in btn_texts), f"Expected button for {ch1} with 2 grps"
    assert any(f"⏸️ {ch2} (Paused)" in t for t in btn_texts), f"Expected button for {ch2} Paused"
    assert any(f"🟢 {ch3} (0 grps)" in t for t in btn_texts), f"Expected button for {ch3} with 0 grps"

    for ch in [ch1, ch2, ch3]:
        delete_channel(ch)

    print("[OK] get_channels_overview consolidation verified!")


class TestInteractiveUI(unittest.TestCase):
    def setUp(self):
        import database
        database.create_table()

    def test_database_pause_resume(self):
        test_database_pause_resume()

    def test_inline_keyboards(self):
        test_inline_keyboards()

    def test_channels_overview_consolidation(self):
        test_channels_overview_consolidation()


if __name__ == "__main__":
    unittest.main()
