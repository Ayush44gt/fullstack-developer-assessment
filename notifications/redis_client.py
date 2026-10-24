import functools

import redis
from django.conf import settings


@functools.cache
def _client(url):
    # Short timeouts: a hung Redis must fail fast so the limiter can fail closed
    # instead of pinning worker processes.
    return redis.Redis.from_url(url, socket_timeout=1.0, socket_connect_timeout=1.0)


def get_redis():
    return _client(settings.REDIS_URL)
