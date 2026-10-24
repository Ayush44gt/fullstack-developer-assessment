"""End to end: 500 jobs through a real Celery worker and the Redis rate limiter.

The worker runs in-process (celery.contrib.testing) on the in-memory broker,
so the test needs no running services. The limiter uses a real Redis when one
is reachable, else fakeredis.

The limit is scaled from 200/min to 50/s so the run takes ~10 s rather than
3 minutes; the limiter code and Lua script are exactly the production ones.
"""

import time
from collections import Counter, defaultdict

import pytest
from celery.contrib.testing.worker import start_worker
from django.db import transaction

from config.celery import app
from notifications.models import DeadLetter, EmailJob
from notifications.policy import QueuePolicy
from notifications.providers import SimulatedEmailProvider
from notifications.services import BULK_QUEUE, CRITICAL_QUEUE, enqueue_email

from .helpers import max_in_window

pytestmark = pytest.mark.django_db(transaction=True)  # the worker thread needs committed rows

TOTAL_JOBS = 500
LIMIT, WINDOW_S = 50, 1.0
FLAKY = 3  # fail twice with a 503, then succeed
STATUS = EmailJob.Status


@pytest.fixture
def fast_policy(settings, rate_key):
    settings.EMAIL_PROVIDER = "notifications.providers.SimulatedEmailProvider"
    settings.EMAIL_QUEUE = {
        **settings.EMAIL_QUEUE,
        "RATE_KEY": rate_key,
        "RATE_LIMIT": LIMIT,
        "RATE_WINDOW_MS": int(WINDOW_S * 1000),
        "RATE_GUARD_MS": 100,
        "CRITICAL_RESERVE": 5,
        "MAX_ATTEMPTS": 4,
        "BACKOFF_BASE_S": 0.2,
        "BACKOFF_MAX_S": 1.0,
        "THROTTLE_JITTER_S": 0.05,
        "CLAIM_TTL_S": 5.0,
    }
    return QueuePolicy.from_settings()


@pytest.fixture
def worker(redis_client, fast_policy):
    with app.connection_for_write() as conn:  # drop messages left by earlier tests
        for queue in (BULK_QUEUE, CRITICAL_QUEUE):
            conn.default_channel.queue_purge(queue)
    SimulatedEmailProvider.calls.clear()
    with start_worker(app, pool="solo", perform_ping_check=False, queues=[CRITICAL_QUEUE, BULK_QUEUE]):
        yield


def recipients():
    """500 recipients: 3 flaky, 1 hard bounce, 1 provider that never recovers, the rest fine."""
    special = [f"flaky{n}+flaky@example.com" for n in range(FLAKY)]
    special += ["gone+bounce@example.com", "outage+down@example.com"]
    return special + [f"customer{n}@example.com" for n in range(TOTAL_JOBS - len(special))]


def wait_for_all_jobs(timeout=120):
    deadline = time.monotonic() + timeout
    while EmailJob.objects.exclude(status__in=EmailJob.TERMINAL).exists():
        assert time.monotonic() < deadline, Counter(EmailJob.objects.values_list("status", flat=True))
        time.sleep(0.25)  # polling the test's own DB, not part of the system under test


def test_500_jobs_none_lost_limit_respected_failures_retried(worker, fast_policy):
    started = time.monotonic()
    with transaction.atomic():  # one burst: all 500 messages are published on commit
        for n, to in enumerate(recipients()):
            kind = EmailJob.Kind.OTP if n % 10 == 0 else EmailJob.Kind.ORDER_CONFIRMATION
            enqueue_email(kind=kind, to=to, subject=f"Email {n}", body="...")

    wait_for_all_jobs()
    elapsed = time.monotonic() - started
    jobs = {str(job.pk): job for job in EmailJob.objects.all()}
    by_to = {job.to: job for job in jobs.values()}
    calls = list(SimulatedEmailProvider.calls)
    calls_by_job = defaultdict(list)
    for call in calls:
        calls_by_job[call.key].append(call)
    busiest = max_in_window([call.at for call in calls], WINDOW_S)
    print(f"\n{len(calls)} provider calls in {elapsed:.1f}s; busiest {WINDOW_S:g}s window: {busiest} (limit {LIMIT})")

    # 1. No job lost: everything finished, and only the two scripted failures are dead.
    assert len(jobs) == TOTAL_JOBS
    assert Counter(job.status for job in jobs.values()) == {STATUS.SENT: TOTAL_JOBS - 2, STATUS.DEAD: 2}
    #    ...and every sent job was accepted by the provider exactly once (no duplicates).
    accepted = Counter(call.key for call in calls if call.outcome == "accepted")
    assert set(accepted) == {pk for pk, job in jobs.items() if job.status == STATUS.SENT}
    assert set(accepted.values()) == {1}

    # 2. Rate limit never exceeded, as the provider sees it. Failed calls count
    #    too: a provider meters requests, not successful deliveries.
    assert busiest <= LIMIT
    #    The limit really was binding: 500+ calls at <= 50/s cannot finish in under ~9 s.
    assert elapsed > (len(calls) / LIMIT - 1) * WINDOW_S

    # 3. Intentional failures. Flaky jobs: two 503s, retried with backoff, then delivered once.
    for n in range(FLAKY):
        job = by_to[f"flaky{n}+flaky@example.com"]
        attempts = calls_by_job[str(job.pk)]
        assert (job.status, job.attempts) == (STATUS.SENT, 3)
        assert [c.outcome for c in attempts] == ["TransientEmailError", "TransientEmailError", "accepted"]
        gaps = [later.at - earlier.at for earlier, later in zip(attempts, attempts[1:])]
        min_backoff = [fast_policy.backoff(1, rand=lambda: 0), fast_policy.backoff(2, rand=lambda: 0)]
        assert all(gap >= minimum for gap, minimum in zip(gaps, min_backoff)), gaps

    #    A provider that never recovers: retried up to MAX_ATTEMPTS, then dead-lettered.
    outage = by_to["outage+down@example.com"]
    assert (outage.status, outage.attempts) == (STATUS.DEAD, fast_policy.max_attempts)
    assert outage.dead_letter.reason == DeadLetter.Reason.RETRIES_EXHAUSTED
    assert len(calls_by_job[str(outage.pk)]) == fast_policy.max_attempts

    #    A hard bounce: not retried at all.
    bounce = by_to["gone+bounce@example.com"]
    assert (bounce.status, bounce.attempts) == (STATUS.DEAD, 1)
    assert bounce.dead_letter.reason == DeadLetter.Reason.PERMANENT_FAILURE
