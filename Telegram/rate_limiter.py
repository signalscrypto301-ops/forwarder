import asyncio
import time
import random
from collections import deque
from typing import Tuple, Optional, Dict


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
    - Enforces Token Bucket per recipient JID
    - Implements Queue-Depth Aware humanized pacing with Gaussian jitter
    - Tracks message volume over 1-hour and 24-hour sliding windows
    - Alerts administrators when approaching safety thresholds
    - Automatically engages progressive cooldown under heavy load
    """

    def __init__(
        self,
        max_per_hour: int = 120,
        max_per_day: int = 1000,
        alert_threshold_percent: float = 80.0,
        per_recipient_rate: float = 0.2,
        per_recipient_burst: int = 3,
    ):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.alert_threshold_percent = alert_threshold_percent
        self.per_recipient_rate = per_recipient_rate
        self.per_recipient_burst = per_recipient_burst

        self.recipient_buckets: Dict[str, TokenBucket] = {}
        self.sliding_hour: deque[float] = deque()
        self.sliding_day: deque[float] = deque()

        self.active_queue_depth: int = 0
        self._last_warning_alert: float = 0.0
        self._last_critical_alert: float = 0.0
        self._lock = asyncio.Lock()

    def enter_queue(self):
        """Increments current active in-flight delivery count."""
        self.active_queue_depth += 1

    def exit_queue(self):
        """Decrements current active in-flight delivery count."""
        self.active_queue_depth = max(0, self.active_queue_depth - 1)

    def get_bucket(self, recipient_id: str) -> TokenBucket:
        """Retrieves or creates a TokenBucket for the specific recipient."""
        if recipient_id not in self.recipient_buckets:
            self.recipient_buckets[recipient_id] = TokenBucket(
                capacity=float(self.per_recipient_burst),
                refill_rate=float(self.per_recipient_rate),
            )
        return self.recipient_buckets[recipient_id]

    def _prune_expired(self, now: float):
        """Prunes timestamps older than 1 hour and 24 hours."""
        hour_cutoff = now - 3600.0
        while self.sliding_hour and self.sliding_hour[0] < hour_cutoff:
            self.sliding_hour.popleft()

        day_cutoff = now - 86400.0
        while self.sliding_day and self.sliding_day[0] < day_cutoff:
            self.sliding_day.popleft()

    def calculate_pacing_delay(self, recipient_id: str) -> float:
        """
        Calculates a humanized pacing delay:
        - Scales based on active queue depth
        - Differentiates between strict groups (@g.us) and newsletters (@newsletter)
        - Injects Gaussian jitter to eliminate uniform machine periodicity
        - Applies progressive cooldown multipliers when limits are approached
        """
        now = time.time()
        self._prune_expired(now)

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

        # Check for volume-based progressive cooldown
        hour_ratio = len(self.sliding_hour) / max(1, self.max_per_hour)
        day_ratio = len(self.sliding_day) / max(1, self.max_per_day)
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

    def record_sent(self):
        """Records a successfully dispatched message timestamp."""
        now = time.time()
        self.sliding_hour.append(now)
        self.sliding_day.append(now)
        self._prune_expired(now)

    def check_health(self) -> Tuple[str, Optional[str]]:
        """
        Evaluates current sending volume against safety thresholds.
        Returns (status, alert_message) where status is 'OK', 'WARNING', or 'CRITICAL'.
        Alerts are debounced (WARNING: 30 mins, CRITICAL: 15 mins).
        """
        now = time.time()
        self._prune_expired(now)

        hour_count = len(self.sliding_hour)
        day_count = len(self.sliding_day)

        hour_pct = (hour_count / max(1, self.max_per_hour)) * 100.0
        day_pct = (day_count / max(1, self.max_per_day)) * 100.0
        max_pct = max(hour_pct, day_pct)

        if max_pct >= 100.0:
            # Critical threshold
            if now - self._last_critical_alert > 900.0:  # 15 minutes debounce
                self._last_critical_alert = now
                msg = (
                    "🚨 <b>WhatsApp Account Health CRITICAL</b>\n\n"
                    f"Message volume has reached safety limits!\n"
                    f"• <b>Last 1 Hour:</b> <code>{hour_count} / {self.max_per_hour}</code> ({hour_pct:.1f}%)\n"
                    f"• <b>Last 24 Hours:</b> <code>{day_count} / {self.max_per_day}</code> ({day_pct:.1f}%)\n"
                    f"• <b>Active Queue Depth:</b> <code>{self.active_queue_depth}</code>\n\n"
                    "⚠️ <i>Emergency rate throttling engaged (2.5x delay) to protect account from automated WhatsApp spam bans.</i>"
                )
                return "CRITICAL", msg
            return "CRITICAL", None

        if max_pct >= self.alert_threshold_percent:
            # Warning threshold
            if now - self._last_warning_alert > 1800.0:  # 30 minutes debounce
                self._last_warning_alert = now
                msg = (
                    "⚠️ <b>WhatsApp Account Health Warning</b>\n\n"
                    f"Broadcast volume is approaching safety thresholds:\n"
                    f"• <b>Last 1 Hour:</b> <code>{hour_count} / {self.max_per_hour}</code> ({hour_pct:.1f}%)\n"
                    f"• <b>Last 24 Hours:</b> <code>{day_count} / {self.max_per_day}</code> ({day_pct:.1f}%)\n"
                    f"• <b>Active Queue Depth:</b> <code>{self.active_queue_depth}</code>\n\n"
                    "ℹ️ <i>Adaptive pacing automatically increased by 1.5x.</i>"
                )
                return "WARNING", msg
            return "WARNING", None

        return "OK", None

    def get_stats(self) -> dict:
        """Returns comprehensive diagnostic dictionary for admin reporting."""
        now = time.time()
        self._prune_expired(now)

        hour_count = len(self.sliding_hour)
        day_count = len(self.sliding_day)
        hour_pct = (hour_count / max(1, self.max_per_hour)) * 100.0
        day_pct = (day_count / max(1, self.max_per_day)) * 100.0

        return {
            "hour_count": hour_count,
            "max_per_hour": self.max_per_hour,
            "hour_percent": round(hour_pct, 1),
            "day_count": day_count,
            "max_per_day": self.max_per_day,
            "day_percent": round(day_pct, 1),
            "active_queue_depth": self.active_queue_depth,
            "tracked_recipients": len(self.recipient_buckets),
            "status": "CRITICAL" if max(hour_pct, day_pct) >= 100 else ("WARNING" if max(hour_pct, day_pct) >= self.alert_threshold_percent else "HEALTHY"),
        }
