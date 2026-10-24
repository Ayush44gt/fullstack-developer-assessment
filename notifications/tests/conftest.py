import uuid

import fakeredis
import pytest

from .helpers import real_redis


@pytest.fixture(scope="session")
def redis_server():
    return real_redis() or fakeredis.FakeRedis()


@pytest.fixture
def redis_client(redis_server, monkeypatch):
    """The Redis used by the limiter and the simulated provider during this test."""
    monkeypatch.setattr("notifications.redis_client.get_redis", lambda: redis_server)
    return redis_server


@pytest.fixture
def rate_key():
    return f"test:ratelimit:{uuid.uuid4().hex}"
