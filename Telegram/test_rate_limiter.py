import asyncio
import time
from rate_limiter import TokenBucket, DeliveryRateController


async def test_token_bucket():
    print("Testing TokenBucket...")
    # Capacity 3, refill rate 2.0 (1 token every 0.5s)
    bucket = TokenBucket(capacity=3.0, refill_rate=2.0)

    # First 3 tokens should be immediate (burst)
    t0 = time.monotonic()
    w1 = await bucket.acquire()
    w2 = await bucket.acquire()
    w3 = await bucket.acquire()
    elapsed = time.monotonic() - t0
    assert w1 == 0.0 and w2 == 0.0 and w3 == 0.0, "Burst tokens should not wait"
    assert elapsed < 0.05, f"Burst took {elapsed}s, expected < 0.05s"

    # 4th token should require waiting ~0.5s
    t1 = time.monotonic()
    w4 = await bucket.acquire()
    elapsed_wait = time.monotonic() - t1
    assert w4 > 0.4 and elapsed_wait > 0.4, f"Throttled token should wait ~0.5s, got {elapsed_wait}s"
    print("[OK] TokenBucket burst and throttle verified!")


async def test_rate_controller_pacing():
    print("Testing DeliveryRateController dynamic pacing...")
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
    assert delay_low >= 0.6, f"Delay {delay_low} below floor"

    # High queue depth (> 20)
    controller.active_queue_depth = 30
    delay_high = controller.calculate_pacing_delay("123@newsletter")
    assert delay_high >= 0.6, f"Delay {delay_high} below floor"

    # Group (@g.us) delay should be generally higher than newsletter
    controller.active_queue_depth = 5
    delay_group = controller.calculate_pacing_delay("123@g.us")
    assert delay_group >= 0.6

    print("[OK] Dynamic pacing delay calculation verified!")


async def test_sliding_window_and_alerts():
    print("Testing sliding window tracking and safety alerts...")
    controller = DeliveryRateController(
        max_per_hour=10,
        max_per_day=50,
        alert_threshold_percent=80.0,
    )

    # Initial state: OK
    status, msg = controller.check_health()
    assert status == "OK" and msg is None, "Initial state should be OK"

    # Send 7 messages (70% - below 80%)
    for _ in range(7):
        controller.record_sent()
    status, msg = controller.check_health()
    assert status == "OK" and msg is None, "70% volume should be OK"

    # Send 8th message (80% - should trigger WARNING)
    controller.record_sent()
    status, msg = controller.check_health()
    assert status == "WARNING", f"Expected WARNING, got {status}"
    assert msg is not None and "Account Health Warning" in msg, "Expected warning alert message"

    # Debounce check: second call should not re-send warning immediately
    status2, msg2 = controller.check_health()
    assert status2 == "WARNING" and msg2 is None, "Warning should be debounced"

    # Send 2 more messages to hit 10 (100% - should trigger CRITICAL)
    controller.record_sent()
    controller.record_sent()
    status_crit, msg_crit = controller.check_health()
    assert status_crit == "CRITICAL", f"Expected CRITICAL, got {status_crit}"
    assert msg_crit is not None and "Account Health CRITICAL" in msg_crit, "Expected critical alert message"

    # Stats check
    stats = controller.get_stats()
    assert stats["hour_count"] == 10
    assert stats["hour_percent"] == 100.0
    assert stats["status"] == "CRITICAL"

    print("[OK] Sliding window metrics and debounced alerts verified!")


async def main():
    await test_token_bucket()
    await test_rate_controller_pacing()
    await test_sliding_window_and_alerts()
    print("\n[SUCCESS] ALL ANTI-BAN RATE LIMITER TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(main())
