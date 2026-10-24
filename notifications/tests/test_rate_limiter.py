"""The limiter in isolation, on a pinned clock (now_ms) so minutes take microseconds."""

import threading

import pytest
import redis

from notifications.rate_limiter import RateLimiterUnavailable, SlidingWindowRateLimiter

from .helpers import max_in_window

WINDOW = 60_000


@pytest.fixture
def limiter(redis_client, rate_key):
    return SlidingWindowRateLimiter(redis_client, key=rate_key, limit=200, window_ms=WINDOW)


def test_flash_sale_burst_admits_exactly_the_limit(limiter):
    """2,000 requests in 10 s against 200/min: the first 200 go through, no more."""
    decisions = [limiter.try_acquire(now_ms=n * 5) for n in range(2_000)]

    assert [d.allowed for d in decisions] == [True] * 200 + [False] * 1_800
    # The 201st (at t=1 s) may retry when the first send (t=0) leaves the window.
    assert decisions[200].retry_after_ms == WINDOW - 1_000


def test_slots_free_up_one_by_one_as_sends_age_out(limiter):
    for t in range(200):
        assert limiter.try_acquire(now_ms=t).allowed

    assert not limiter.try_acquire(now_ms=WINDOW - 1).allowed
    assert limiter.try_acquire(now_ms=WINDOW).allowed  # the t=0 send just left the window
    assert not limiter.try_acquire(now_ms=WINDOW).allowed  # ...and only that one
    assert limiter.try_acquire(now_ms=WINDOW + 1).allowed


def test_sustained_overload_never_exceeds_limit_in_any_rolling_minute(limiter):
    """Ten minutes at 20 requests/s (6x the limit). Never more than 200 in any
    60 s window, and not over-strict either: 200 admitted per minute."""
    admitted = [t for t in range(0, 600_000, 50) if limiter.try_acquire(now_ms=t).allowed]

    assert max_in_window(admitted, WINDOW) == 200
    assert len(admitted) == 2_000


def test_bulk_traffic_cannot_use_the_critical_reserve(limiter):
    bulk = [limiter.try_acquire(limit=180, now_ms=t) for t in range(300)]
    assert sum(d.allowed for d in bulk) == 180

    critical = [limiter.try_acquire(limit=200, now_ms=300 + t) for t in range(50)]
    assert sum(d.allowed for d in critical) == 20

    # Window is full (200). Bulk needs it below 180, i.e. the 21 oldest gone:
    # the 21st oldest was admitted at t=20.
    assert limiter.try_acquire(limit=180, now_ms=400).retry_after_ms == 20 + WINDOW - 400


def test_concurrent_callers_never_over_admit(redis_client, rate_key):
    """Atomicity: 16 threads x 25 calls race for 100 slots on the real clock."""
    limiter = SlidingWindowRateLimiter(redis_client, key=rate_key, limit=100, window_ms=WINDOW)
    barrier = threading.Barrier(16)
    results, lock = [], threading.Lock()

    def hammer():
        barrier.wait()
        mine = [limiter.try_acquire().allowed for _ in range(25)]
        with lock:
            results.extend(mine)

    threads = [threading.Thread(target=hammer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 400
    assert sum(results) == 100
    assert redis_client.zcard(rate_key) == 100


def test_unreachable_redis_raises_so_callers_can_fail_closed():
    client = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    limiter = SlidingWindowRateLimiter(client, key="unused", limit=1, window_ms=1_000)

    with pytest.raises(RateLimiterUnavailable):
        limiter.try_acquire()
