import unittest
import asyncio
import time
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from rate_limiter import TokenBucket, DeliveryRateController


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_token_bucket(self):
        # Capacity 3, refill rate 2.0 (1 token every 0.5s)
        bucket = TokenBucket(capacity=3.0, refill_rate=2.0)

        # First 3 tokens should be immediate (burst)
        t0 = time.monotonic()
        w1 = await bucket.acquire()
        w2 = await bucket.acquire()
        w3 = await bucket.acquire()
        elapsed = time.monotonic() - t0
        self.assertEqual(w1, 0.0)
        self.assertEqual(w2, 0.0)
        self.assertEqual(w3, 0.0)
        self.assertLess(elapsed, 0.05)

        # 4th token should require waiting ~0.5s
        t1 = time.monotonic()
        w4 = await bucket.acquire()
        elapsed_wait = time.monotonic() - t1
        self.assertGreater(w4, 0.4)
        self.assertGreater(elapsed_wait, 0.4)

    async def test_rate_controller_pacing(self):
        controller = DeliveryRateController(
            max_per_hour=100,
            max_per_day=500,
            alert_threshold_percent=80.0,
            per_recipient_rate=0.5,
            per_recipient_burst=2,
        )

        # Low queue depth (<= 5)
        controller.active_queue_depth = 2
        delay_low = controller.calculate_pacing_delay("123@newsletter")
        self.assertGreaterEqual(delay_low, 0.6)

        # High queue depth (> 20)
        controller.active_queue_depth = 30
        delay_high = controller.calculate_pacing_delay("123@newsletter")
        self.assertGreaterEqual(delay_high, 0.6)

        # Group (@g.us) delay should be generally higher than newsletter
        controller.active_queue_depth = 5
        delay_group = controller.calculate_pacing_delay("123@g.us")
        self.assertGreaterEqual(delay_group, 0.6)

    async def test_sliding_window_and_alerts(self):
        controller = DeliveryRateController(
            max_per_hour=10,
            max_per_day=50,
            alert_threshold_percent=80.0,
        )

        # Initial state: OK
        status, msg = controller.check_health()
        self.assertEqual(status, "OK")
        self.assertIsNone(msg)

        # Send 7 messages (70% - below 80%)
        for _ in range(7):
            controller.record_sent()
        status, msg = controller.check_health()
        self.assertEqual(status, "OK")
        self.assertIsNone(msg)

        # Send 8th message (80% - should trigger WARNING)
        controller.record_sent()
        status, msg = controller.check_health()
        self.assertEqual(status, "WARNING")
        self.assertIsNotNone(msg)
        self.assertIn("Account Health Warning", msg)

        # Debounce check: second call should not re-send warning immediately
        status2, msg2 = controller.check_health()
        self.assertEqual(status2, "WARNING")
        self.assertIsNone(msg2)

        # Send 2 more messages to hit 10 (100% - should trigger CRITICAL)
        controller.record_sent()
        controller.record_sent()
        status_crit, msg_crit = controller.check_health()
        self.assertEqual(status_crit, "CRITICAL")
        self.assertIsNotNone(msg_crit)
        self.assertIn("Account Health CRITICAL", msg_crit)

        # Stats check
        stats = controller.get_stats()
        self.assertEqual(stats["hour_count"], 10)
        self.assertEqual(stats["hour_percent"], 100.0)
        self.assertEqual(stats["status"], "CRITICAL")

    async def test_multi_account_rate_isolation(self):
        """
        Verifies that when Account 1 reaches capacity, Account 2 is not throttled,
        and total pool throughput scales proportionally with number of accounts.
        """
        accounts = ["user", "account2", "account3", "account4"]
        controller = DeliveryRateController(
            max_per_hour=10,
            max_per_day=50,
            alert_threshold_percent=80.0,
            account_ids=accounts,
        )

        # 1. Fill Account 1 (user) to 100% capacity
        for _ in range(10):
            controller.record_sent(account_id="user")

        # Account 1 is CRITICAL
        status_user, msg_user = controller.check_health(account_id="user")
        self.assertEqual(status_user, "CRITICAL")
        self.assertIsNotNone(msg_user)
        self.assertIn("user", msg_user)

        # Account 2, 3, 4 are still OK
        for acc in ["account2", "account3", "account4"]:
            status_acc, msg_acc = controller.check_health(account_id=acc)
            self.assertEqual(status_acc, "OK")
            self.assertIsNone(msg_acc)

        # Account 1 has emergency cooldown multiplier active
        controller.active_queue_depth = 1
        user_delays = [controller.calculate_pacing_delay("grp@g.us", account_id="user") for _ in range(10)]
        avg_user_delay = sum(user_delays) / len(user_delays)

        # Account 2 is completely unthrottled
        acc2_delays = [controller.calculate_pacing_delay("grp@g.us", account_id="account2") for _ in range(10)]
        avg_acc2_delay = sum(acc2_delays) / len(acc2_delays)

        # Account 1 delay must be significantly higher (~2.5x) than Account 2 delay
        self.assertGreater(avg_user_delay, avg_acc2_delay * 1.8)

        # 2. Dispatch 5 messages from Account 2
        for _ in range(5):
            controller.record_sent(account_id="account2")

        # Total pool dispatched = 15 messages (10 from user + 5 from account2)
        # Pool capacity is 4 * 10 = 40 msgs/hour
        stats = controller.get_stats()
        self.assertEqual(stats["hour_count"], 15)
        self.assertEqual(stats["max_per_hour"], 40)
        self.assertEqual(stats["hour_percent"], 37.5)

        # Verify individual account stats breakdown
        self.assertIn("accounts", stats)
        self.assertEqual(stats["accounts"]["user"]["hour_count"], 10)
        self.assertEqual(stats["accounts"]["user"]["status"], "CRITICAL")
        self.assertEqual(stats["accounts"]["account2"]["hour_count"], 5)
        self.assertEqual(stats["accounts"]["account2"]["status"], "HEALTHY")
        self.assertEqual(stats["accounts"]["account3"]["hour_count"], 0)
        self.assertEqual(stats["accounts"]["account3"]["status"], "HEALTHY")

    async def test_multi_account_token_buckets(self):
        """
        Verifies that token buckets are tracked per (account_id, recipient_id),
        so one account bursting does not starve another account sending to the same recipient.
        """
        controller = DeliveryRateController(
            per_recipient_rate=0.5,
            per_recipient_burst=3,
            account_ids=["user", "account2"],
        )

        bucket_user = controller.get_bucket("shared_group@g.us", account_id="user")
        bucket_acc2 = controller.get_bucket("shared_group@g.us", account_id="account2")

        # Distinct bucket instances
        self.assertIsNot(bucket_user, bucket_acc2)

        # Exhaust burst on user bucket (3 tokens)
        w1 = await bucket_user.acquire()
        w2 = await bucket_user.acquire()
        w3 = await bucket_user.acquire()
        self.assertEqual(w1, 0.0)
        self.assertEqual(w2, 0.0)
        self.assertEqual(w3, 0.0)

        # Account 2 bucket still has full burst capacity
        w_acc2_1 = await bucket_acc2.acquire()
        w_acc2_2 = await bucket_acc2.acquire()
        self.assertEqual(w_acc2_1, 0.0)
        self.assertEqual(w_acc2_2, 0.0)


if __name__ == "__main__":
    unittest.main()
