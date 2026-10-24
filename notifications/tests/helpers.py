import os

import redis

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def real_redis():
    """A client for the test Redis server, or None when none is reachable."""
    client = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=0.5)
    try:
        client.ping()
    except redis.ConnectionError:
        return None
    return client


def max_in_window(times, window):
    """Largest number of events inside any half-open window (t - window, t]."""
    times = sorted(times)
    best = start = 0
    for end, t in enumerate(times):
        while times[start] <= t - window:
            start += 1
        best = max(best, end - start + 1)
    return best
