from notifications.tests.helpers import TEST_REDIS_URL, real_redis


def pytest_report_header(config):
    if real_redis():
        return f"redis: real server at {TEST_REDIS_URL}"
    return "redis: no server reachable, using fakeredis; the worker-crash tests will be skipped"
