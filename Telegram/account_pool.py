import asyncio
import time
import logging
from datetime import datetime

logger = logging.getLogger("forwarder")

CONFIGURED_ACCOUNTS = ["user", "account2", "account3", "account4"]
ACCOUNT_INDICES = {1: "user", 2: "account2", 3: "account3", 4: "account4"}
ID_TO_INDEX = {"user": 1, "account2": 2, "account3": 3, "account4": 4}
ACCOUNT_LABELS = {
    "user": "Account 1 (Primary)",
    "account2": "Account 2",
    "account3": "Account 3",
    "account4": "Account 4",
}


class AccountPool:
    """
    Multi-Account WhatsApp Dispatcher & Pool Manager.

    Coordinates up to 4 concurrent WhatsApp sender sessions:
    - Round-robin load balancing across active accounts (1 -> 2 -> 3 -> 4 -> 1).
    - Automatic zero-downtime failover if an account is degraded or disconnected.
    - Synchronizes live metadata (phone numbers, display names, connection states)
      via the WhatsApp microservice GET /sessions endpoint.
    - Tracks per-account message dispatch volumes.
    """

    def __init__(self):
        self._accounts: dict[str, dict] = {
            acc_id: {
                "id": acc_id,
                "index": ID_TO_INDEX[acc_id],
                "label": ACCOUNT_LABELS[acc_id],
                "is_ready": (acc_id == "user"),  # Account 1 defaults to ready
                "status": "ready" if acc_id == "user" else "not_logged_in",
                "phone": None,
                "name": None,
                "today_dispatched": 0,
                "degraded_until": 0.0,
                "last_used_at": 0.0,
                "has_proxy": False,
                "proxy_host": None,
                "proxy_exit_ip": None,
                "proxy_country": None,
                "proxy_country_code": None,
                "proxy_latency_ms": None,
                "proxy_status": "direct",
            }
            for acc_id in CONFIGURED_ACCOUNTS
        }
        self._rr_cursor: int = 0
        self._lock = asyncio.Lock()
        self._last_date_key: str = datetime.now().strftime("%Y-%m-%d")

    def resolve_account_id(self, identifier: str | int | None) -> str:
        """Normalizes any representation (1, '1', 'account1', 'user') to canonical account ID."""
        if identifier is None:
            return "user"
        raw = str(identifier).strip().lower()
        if raw in ("1", "account1", "user"):
            return "user"
        if raw in ("2", "account2"):
            return "account2"
        if raw in ("3", "account3"):
            return "account3"
        if raw in ("4", "account4"):
            return "account4"
        return "user"

    def get_index_for_id(self, account_id: str) -> int:
        """Returns 1-based index (1 to 4) for a given account ID."""
        return ID_TO_INDEX.get(account_id, 1)

    def sync_from_api_response(self, api_data: dict) -> None:
        """Updates internal account states from GET /sessions response."""
        if not api_data or not isinstance(api_data, dict):
            return
        accounts_list = api_data.get("accounts", [])
        for acc in accounts_list:
            acc_id = acc.get("id")
            if acc_id in self._accounts:
                target = self._accounts[acc_id]
                target["is_ready"] = bool(acc.get("isReady", False))
                target["status"] = str(acc.get("status", "unknown"))
                target["phone"] = acc.get("phone")
                target["name"] = acc.get("name")
                target["has_proxy"] = bool(acc.get("hasProxy", False))
                target["proxy_host"] = acc.get("proxyHost")
                target["proxy_exit_ip"] = acc.get("proxyExitIp")
                target["proxy_country"] = acc.get("proxyCountry")
                target["proxy_country_code"] = acc.get("proxyCountryCode")
                target["proxy_latency_ms"] = acc.get("proxyLatencyMs")
                target["proxy_status"] = acc.get("proxyStatus", "direct" if not target["has_proxy"] else "healthy")

    def get_ready_accounts(self) -> list[str]:
        """Returns list of currently ready, non-degraded account IDs."""
        now = time.time()
        ready = []
        for acc_id in CONFIGURED_ACCOUNTS:
            acc = self._accounts[acc_id]
            if acc["is_ready"] and now >= acc["degraded_until"]:
                ready.append(acc_id)
        # Fallback to Account 1 if none report ready, ensuring delivery is never stalled
        if not ready:
            ready = ["user"]
        return ready

    def get_strictly_ready_accounts(self) -> list[str]:
        """Returns accounts that are strictly ready and not degraded, without fallback."""
        now = time.time()
        return [
            acc_id
            for acc_id in CONFIGURED_ACCOUNTS
            if self._accounts[acc_id]["is_ready"] and now >= self._accounts[acc_id]["degraded_until"]
        ]

    def format_active_siblings_string(self, exclude_account_id: str) -> str:
        """
        Returns human-readable text for remaining active siblings,
        e.g. 'Account 1 & 3' or 'Account 1, 3 & 4' or 'Account 1'.
        If no siblings are ready, returns an alert string.
        """
        canon_id = self.resolve_account_id(exclude_account_id)
        short_labels = {
            "user": "Account 1",
            "account2": "Account 2",
            "account3": "Account 3",
            "account4": "Account 4",
        }
        active_siblings = [
            acc_id for acc_id in self.get_strictly_ready_accounts()
            if acc_id != canon_id
        ]
        if not active_siblings:
            return "None (Pool Exhausted - Immediate Re-login Required)"

        indices = [ID_TO_INDEX.get(acc_id, 1) for acc_id in active_siblings]
        if not indices:
            return "None (Pool Exhausted - Immediate Re-login Required)"
        if len(indices) == 1:
            return f"Account {indices[0]}"
        elif len(indices) == 2:
            return f"Account {indices[0]} & {indices[1]}"
        else:
            nums_str = ", ".join(str(x) for x in indices[:-1]) + f" & {indices[-1]}"
            return f"Account {nums_str}"

    async def get_next_sender(self) -> str:
        """
        Thread-safe Round-Robin sender selection.
        Picks the next available active account in sequence.
        """
        self._check_midnight_reset()
        ready_accounts = self.get_ready_accounts()

        async with self._lock:
            self._rr_cursor = self._rr_cursor % len(ready_accounts)
            selected = ready_accounts[self._rr_cursor]
            self._rr_cursor = (self._rr_cursor + 1) % len(ready_accounts)

        self._accounts[selected]["last_used_at"] = time.time()
        return selected

    def mark_degraded(self, account_id: str, cooldown_sec: int = 60) -> None:
        """Temporarily penalizes an account so traffic routes through sibling accounts."""
        canon_id = self.resolve_account_id(account_id)
        if canon_id in self._accounts:
            self._accounts[canon_id]["degraded_until"] = time.time() + cooldown_sec
            logger.warning(
                f"[AccountPool] Account {canon_id} marked degraded for {cooldown_sec}s. Routing around it."
            )

    def mark_recovered(self, account_id: str) -> None:
        """Clears any degradation for an account immediately."""
        canon_id = self.resolve_account_id(account_id)
        if canon_id in self._accounts:
            self._accounts[canon_id]["degraded_until"] = 0.0

    def record_dispatched(self, account_id: str) -> None:
        """Increments today's dispatched post counter for the specified account."""
        self._check_midnight_reset()
        canon_id = self.resolve_account_id(account_id)
        if canon_id in self._accounts:
            self._accounts[canon_id]["today_dispatched"] += 1

    def _check_midnight_reset(self) -> None:
        today_key = datetime.now().strftime("%Y-%m-%d")
        if today_key != self._last_date_key:
            for acc in self._accounts.values():
                acc["today_dispatched"] = 0
            self._last_date_key = today_key

    def get_account_info(self, account_id: str) -> dict:
        canon_id = self.resolve_account_id(account_id)
        return dict(self._accounts.get(canon_id, self._accounts["user"]))

    def get_all_accounts(self) -> list[dict]:
        return [dict(self._accounts[acc_id]) for acc_id in CONFIGURED_ACCOUNTS]

    def format_dashboard_card(self) -> str:
        """Formats an HTML summary card for Telegram display."""
        ready_accounts = self.get_ready_accounts()
        ready_count = len([a for a in CONFIGURED_ACCOUNTS if self._accounts[a]["is_ready"]])
        total_today = sum(a["today_dispatched"] for a in self._accounts.values())

        lines = [
            f"📱 <b>WhatsApp Accounts Pool</b> ({ready_count}/4 Active)",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        status_badges = {
            "ready": "🟢 <b>Ready</b>",
            "waiting_qr_scan": "🟡 <b>Waiting for QR Scan</b>",
            "reconnecting": "🔄 <b>Reconnecting</b>",
            "disconnected": "🔴 <b>Disconnected</b>",
            "not_logged_in": "⚪ <b>Not Logged In</b>",
        }

        for acc_id in CONFIGURED_ACCOUNTS:
            acc = self._accounts[acc_id]
            badge = status_badges.get(acc["status"], "⚪ <b>Offline</b>")
            phone_str = acc["phone"] or "Not configured"
            name_str = f' ("{acc["name"]}")' if acc["name"] else ""

            lines.append(f"• <b>{acc['label']}</b>: {badge}")

            # Residential Proxy metadata
            if acc.get("has_proxy"):
                host_str = acc.get("proxy_host") or "Configured"
                exit_ip = acc.get("proxy_exit_ip")
                country = acc.get("proxy_country") or ""
                latency = f" - {acc['proxy_latency_ms']}ms" if acc.get("proxy_latency_ms") else ""

                flag = ""
                code = (acc.get("proxy_country_code") or "").upper()
                if code == "US": flag = "🇺🇸 "
                elif code in ("GB", "UK"): flag = "🇬🇧 "
                elif code == "DE": flag = "🇩🇪 "
                elif code == "CA": flag = "🇨🇦 "
                elif code == "FR": flag = "🇫🇷 "
                elif code == "IN": flag = "🇮🇳 "

                if exit_ip:
                    lines.append(f"  └ 🌐 Proxy: <code>{exit_ip}</code> ({flag}{country}{latency})")
                else:
                    lines.append(f"  └ 🌐 Proxy: <code>{host_str}</code> (Active)")
            else:
                lines.append("  └ 🌐 Proxy: <i>Direct (Host VPS IP)</i>")

            if acc["is_ready"]:
                lines.append(f"  └ 📞 <code>{phone_str}</code>{name_str}")
                lines.append(f"  └ 📤 Dispatched Today: <b>{acc['today_dispatched']} msgs</b>")
            elif acc["status"] == "waiting_qr_scan":
                lines.append("  └ ⚠️ <i>QR code waiting for scan via /login.</i>")
            elif acc["phone"]:
                lines.append(f"  └ 📞 <code>{phone_str}</code> (Saved)")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        if ready_count > 1:
            lines.append(
                f"🔄 <b>Dispatch Policy</b>: <code>Round-Robin Active Load Balancing ({ready_count} numbers)</code>"
            )
        else:
            lines.append(
                "🔄 <b>Dispatch Policy</b>: <code>Single Dedicated Sender (Account 1)</code>"
            )
        lines.append(f"📊 <b>Total Dispatched Across Pool Today</b>: <b>{total_today} msgs</b>")

        return "\n".join(lines)

    def get_pool_summary(self) -> str:
        """Returns short summary e.g. '1/4 Active' or '4/4 Active'."""
        ready_count = len([a for a in CONFIGURED_ACCOUNTS if self._accounts[a]["is_ready"]])
        return f"{ready_count}/4 Accounts Active"