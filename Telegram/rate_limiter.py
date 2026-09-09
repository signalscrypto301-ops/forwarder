import asyncio
import time
import random
from collections import deque, defaultdict
from typing import Tuple, Optional, Dict, List


class TokenBucket:
    """
    Token Bucket rate limiter for an individual recipient (group / newsletter).
    Guarantees no individual target receives messages faster than refill_rate,
    while permitting bounded bursts up to capacity.
    """

    def __init__(self, capacity: float = 3.0, refill_rate: float = 0.2):
        self.capacity: float = float(capacity)
        self.refill_rate: float = float(refill_rate)
        self.tokens: float = float(capacity)
        self.last_update: float = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> float:
        """
        Acquires 1 token. If sufficient tokens are not yet available,
        asynchronously sleeps until a token has regenerated.
        Returns the seconds waited.
        """
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_update = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0

            wait_time = (1.0 - self.tokens) / self.refill_rate
            await asyncio.sleep(wait_time)
            self.tokens = 0.0
            self.last_update = time.monotonic()
            return wait_time


class DeliveryRateController:
    """
    Centralized WhatsApp Anti-Ban & Deliverability Protection Controller:
    - Enforces Token Bucket per (account_id, recipient_id)
    - Implements Queue-Depth Aware humanized pacing with Gaussian jitter
    - Tracks message volume over 1-hour and 24-hour sliding windows PER ACCOUNT
    - Alerts administrators when an account approaches safety thresholds
    - Automatically engages progressive cooldown under heavy load per account
    - Scales aggregate delivery throughput with multi-account pools
    """

    def __init__(
        self,
        max_per_hour: int = 120,
        max_per_day: int = 1000,
        alert_threshold_percent: float = 80.0,
        per_recipient_rate: float = 0.2,
        per_recipient_burst: int = 3,
        account_ids: Optional[List[str]] = None,
    ):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.alert_threshold_percent = alert_threshold_percent
        self.per_recipient_rate = per_recipient_rate
        self.per_recipient_burst = per_recipient_burst

        # Configured accounts list (defaults to ["user"])
        self.account_ids: List[str] = list(account_ids) if account_ids else ["user"]

        # Recipient buckets keyed by f"{account_id}:{recipient_id}" or recipient_id
        self.recipient_buckets: Dict[str, TokenBucket] = {}

        # Per-account sliding windows
        self.account_sliding_hour: Dict[str, deque[float]] = defaultdict(deque)
        self.account_sliding_day: Dict[str, deque[float]] = defaultdict(deque)

        for acc in self.account_ids:
            self.account_sliding_hour[acc] = deque()
            self.account_sliding_day[acc] = deque()

        self.active_queue_depth: int = 0
        self._last_warning_alert: Dict[str, float] = defaultdict(float)
        self._last_critical_alert: Dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    @property
    def sliding_hour(self) -> deque[float]:
        """Backward compatibility for direct access to default account sliding hour."""
        return self.account_sliding_hour["user"]

    @property
    def sliding_day(self) -> deque[float]:
        """Backward compatibility for direct access to default account sliding day."""
        return self.account_sliding_day["user"]

    def enter_queue(self):
        """Increments current active in-flight delivery count."""
        self.active_queue_depth += 1

    def exit_queue(self):
        """Decrements current active in-flight delivery count."""
        self.active_queue_depth = max(0, self.active_queue_depth - 1)

    def _bucket_key(self, recipient_id: str, account_id: Optional[str] = None) -> str:
        return f"{account_id}:{recipient_id}" if account_id else recipient_id

    def get_bucket(self, recipient_id: str, account_id: Optional[str] = None) -> TokenBucket:
        """Retrieves or creates a TokenBucket for the specific recipient and account."""
        key = self._bucket_key(recipient_id, account_id)
        if key not in self.recipient_buckets:
            self.recipient_buckets[key] = TokenBucket(
                capacity=float(self.per_recipient_burst),
                refill_rate=float(self.per_recipient_rate),
            )
        return self.recipient_buckets[key]

    def _prune_expired(self, now: float, account_id: Optional[str] = None):
        """Prunes timestamps older than 1 hour and 24 hours."""
        hour_cutoff = now - 3600.0
        day_cutoff = now - 86400.0

        targets = [account_id] if account_id else list(self.account_sliding_hour.keys())
        for acc in targets:
            h_deque = self.account_sliding_hour.get(acc)
            if h_deque:
                while h_deque and h_deque[0] < hour_cutoff:
                    h_deque.popleft()
            d_deque = self.account_sliding_day.get(acc)
            if d_deque:
                while d_deque and d_deque[0] < day_cutoff:
                    d_deque.popleft()

    def calculate_pacing_delay(self, recipient_id: str, account_id: str = "user") -> float:
        """
        Calculates a humanized pacing delay:
        - Scales based on active queue depth
        - Differentiates between strict groups (@g.us) and newsletters (@newsletter)
        - Injects Gaussian jitter to eliminate uniform machine periodicity
        - Applies progressive cooldown multipliers based on the specific account's volume
        """
        now = time.time()
        self._prune_expired(now, account_id=account_id)

        q = self.active_queue_depth

        # Dynamic baseline based on queue depth
        if q <= 5:
            base_delay = random.uniform(2.0, 3.5)
        elif q <= 20:
            base_delay = random.uniform(1.2, 2.2)
        else:
            base_delay = random.uniform(0.8, 1.4)

        # Groups (@g.us) have stricter heuristics than newsletters (@newsletter)
        if "@g.us" in recipient_id:
            base_delay *= 1.25

        # Check for volume-based progressive cooldown for THIS specific account
        h_deque = self.account_sliding_hour.get(account_id, deque())
        d_deque = self.account_sliding_day.get(account_id, deque())
        hour_ratio = len(h_deque) / max(1, self.max_per_hour)
        day_ratio = len(d_deque) / max(1, self.max_per_day)
        max_ratio = max(hour_ratio, day_ratio)

        if max_ratio >= 1.0:
            # Critical limit reached: strong cooldown to avoid immediate ban
            base_delay *= 2.5
        elif max_ratio >= (self.alert_threshold_percent / 100.0):
            # Approaching warning threshold: moderate pacing stretch
            base_delay *= 1.5

        # Gaussian jitter with standard deviation = 15%
        jitter = random.gauss(1.0, 0.15)
        # Ensure delay never drops below safe absolute floor of 0.6 seconds
        final_delay = max(0.6, base_delay * jitter)
        return round(final_delay, 3)

    def record_sent(self, account_id: str = "user"):
        """Records a successfully dispatched message timestamp for a specific account."""
        now = time.time()
        self.account_sliding_hour[account_id].append(now)
        self.account_sliding_day[account_id].append(now)
        self._prune_expired(now, account_id=account_id)

    def check_health(self, account_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """
        Evaluates sending volume against safety thresholds.
        If account_id is provided, evaluates for that account.
        If account_id is None, evaluates across all accounts and returns the highest severity alert.
        Alerts are debounced (WARNING: 30 mins, CRITICAL: 15 mins) per account.
        """
        now = time.time()
        self._prune_expired(now, account_id=account_id)

        targets = [account_id] if account_id else list(self.account_sliding_hour.keys())
        if not targets:
            targets = ["user"]

        highest_status = "OK"
        highest_msg = None

        for acc in targets:
            h_deque = self.account_sliding_hour.get(acc, deque())
            d_deque = self.account_sliding_day.get(acc, deque())
            hour_count = len(h_deque)
            day_count = len(d_deque)

            hour_pct = (hour_count / max(1, self.max_per_hour)) * 100.0
            day_pct = (day_count / max(1, self.max_per_day)) * 100.0
            max_pct = max(hour_pct, day_pct)

            acc_suffix = f" ({acc})" if len(targets) > 1 or acc != "user" else ""

            if max_pct >= 100.0:
                # Critical threshold
                if now - self._last_critical_alert[acc] > 900.0:  # 15 minutes debounce
                    self._last_critical_alert[acc] = now
                    msg = (
                        f"🚨 <b>WhatsApp Account Health CRITICAL{acc_suffix}</b>\n\n"
                        f"Message volume has reached safety limits for account <code>{acc}</code>!\n"
                        f"• <b>Last 1 Hour:</b> <code>{hour_count} / {self.max_per_hour}</code> ({hour_pct:.1f}%)\n"
                        f"• <b>Last 24 Hours:</b> <code>{day_count} / {self.max_per_day}</code> ({day_pct:.1f}%)\n"
                        f"• <b>Active Queue Depth:</b> <code>{self.active_queue_depth}</code>\n\n"
                        "⚠️ <i>Emergency rate throttling engaged (2.5x delay) to protect account from automated WhatsApp spam bans.</i>"
                    )
                    highest_status = "CRITICAL"
                    highest_msg = msg
                else:
                    if highest_status != "CRITICAL":
                        highest_status = "CRITICAL"

            elif max_pct >= self.alert_threshold_percent:
                # Warning threshold
                if now - self._last_warning_alert[acc] > 1800.0:  # 30 minutes debounce
                    self._last_warning_alert[acc] = now
                    msg = (
                        f"⚠️ <b>WhatsApp Account Health Warning{acc_suffix}</b>\n\n"
                        f"Broadcast volume is approaching safety thresholds for account <code>{acc}</code>:\n"
                        f"• <b>Last 1 Hour:</b> <code>{hour_count} / {self.max_per_hour}</code> ({hour_pct:.1f}%)\n"
                        f"• <b>Last 24 Hours:</b> <code>{day_count} / {self.max_per_day}</code> ({day_pct:.1f}%)\n"
                        f"• <b>Active Queue Depth:</b> <code>{self.active_queue_depth}</code>\n\n"
                        "ℹ️ <i>Adaptive pacing automatically increased by 1.5x.</i>"
                    )
                    if highest_status != "CRITICAL":
                        highest_status = "WARNING"
                        highest_msg = msg
                else:
                    if highest_status == "OK":
                        highest_status = "WARNING"

        return highest_status, highest_msg

    def get_stats(self, account_id: Optional[str] = None) -> dict:
        """Returns comprehensive diagnostic dictionary for admin reporting."""
        now = time.time()
        self._prune_expired(now, account_id=account_id)

        if account_id:
            h_deque = self.account_sliding_hour.get(account_id, deque())
            d_deque = self.account_sliding_day.get(account_id, deque())
            hour_count = len(h_deque)
            day_count = len(d_deque)
            hour_pct = (hour_count / max(1, self.max_per_hour)) * 100.0
            day_pct = (day_count / max(1, self.max_per_day)) * 100.0
            status = "CRITICAL" if max(hour_pct, day_pct) >= 100 else ("WARNING" if max(hour_pct, day_pct) >= self.alert_threshold_percent else "HEALTHY")

            return {
                "account_id": account_id,
                "hour_count": hour_count,
                "max_per_hour": self.max_per_hour,
                "hour_percent": round(hour_pct, 1),
                "day_count": day_count,
                "max_per_day": self.max_per_day,
                "day_percent": round(day_pct, 1),
                "active_queue_depth": self.active_queue_depth,
                "tracked_recipients": len(self.recipient_buckets),
                "status": status,
            }

        # Otherwise aggregate pool stats
        accounts_stats = {}
        total_hour = 0
        total_day = 0
        worst_status = "HEALTHY"

        all_accs = list(self.account_sliding_hour.keys())
        if not all_accs:
            all_accs = ["user"]

        for acc in all_accs:
            h_cnt = len(self.account_sliding_hour.get(acc, deque()))
            d_cnt = len(self.account_sliding_day.get(acc, deque()))
            total_hour += h_cnt
            total_day += d_cnt
            h_pct = (h_cnt / max(1, self.max_per_hour)) * 100.0
            d_pct = (d_cnt / max(1, self.max_per_day)) * 100.0
            acc_status = "CRITICAL" if max(h_pct, d_pct) >= 100 else ("WARNING" if max(h_pct, d_pct) >= self.alert_threshold_percent else "HEALTHY")
            if acc_status == "CRITICAL":
                worst_status = "CRITICAL"
            elif acc_status == "WARNING" and worst_status != "CRITICAL":
                worst_status = "WARNING"

            accounts_stats[acc] = {
                "hour_count": h_cnt,
                "max_per_hour": self.max_per_hour,
                "hour_percent": round(h_pct, 1),
                "day_count": d_cnt,
                "max_per_day": self.max_per_day,
                "day_percent": round(d_pct, 1),
                "status": acc_status,
            }

        num_accounts = max(1, len(all_accs))
        pool_max_hour = num_accounts * self.max_per_hour
        pool_max_day = num_accounts * self.max_per_day
        pool_hour_pct = (total_hour / max(1, pool_max_hour)) * 100.0
        pool_day_pct = (total_day / max(1, pool_max_day)) * 100.0

        return {
            "hour_count": total_hour,
            "max_per_hour": pool_max_hour,
            "hour_percent": round(pool_hour_pct, 1),
            "day_count": total_day,
            "max_per_day": pool_max_day,
            "day_percent": round(pool_day_pct, 1),
            "active_queue_depth": self.active_queue_depth,
            "tracked_recipients": len(self.recipient_buckets),
            "status": worst_status,
            "accounts": accounts_stats,
        }
