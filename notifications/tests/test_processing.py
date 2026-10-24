"""Every branch of one delivery attempt, without Celery or Redis."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from notifications.models import DeadLetter, EmailJob
from notifications.policy import QueuePolicy
from notifications.processing import Outcome, process_email_job
from notifications.providers import PermanentEmailError, TransientEmailError
from notifications.rate_limiter import Decision, RateLimiterUnavailable

pytestmark = pytest.mark.django_db

POLICY = QueuePolicy(
    rate_key="unused",
    rate_limit=10,
    rate_window_ms=1_000,
    rate_guard_ms=0,
    critical_reserve=2,
    max_attempts=3,
    backoff_base_s=10.0,
    backoff_max_s=300.0,
    throttle_jitter_s=0.0,
    claim_ttl_s=90.0,
    limiter_down_retry_s=5.0,
    unpublished_after_s=60.0,
    stale_after_s=1_800.0,
)
Status = EmailJob.Status


class StubLimiter:
    def __init__(self, allowed=True, retry_after_ms=0, error=None, during_call=None):
        self.allowed, self.retry_after_ms, self.error = allowed, retry_after_ms, error
        self.during_call = during_call
        self.limits_requested = []

    def try_acquire(self, *, limit=None, now_ms=None):
        self.limits_requested.append(limit)
        if self.during_call:
            self.during_call()
        if self.error:
            raise self.error
        return Decision(self.allowed, self.retry_after_ms, 0)


class StubProvider:
    """Raises the given errors on successive calls, then accepts."""

    def __init__(self, *errors, during_call=None):
        self.errors, self.during_call, self.calls = list(errors), during_call, 0

    def send(self, **message):
        self.calls += 1
        if self.during_call:
            self.during_call()
        if self.errors:
            raise self.errors.pop(0)
        return "provider-msg-1"


class WorkerKilled(BaseException):
    """Stands in for SIGKILL: no except-branch in the task ever sees it."""


def make_job(kind=EmailJob.Kind.ORDER_CONFIRMATION, **fields):
    return EmailJob.objects.create(kind=kind, to="a@example.com", subject="s", body="b", **fields)


def run(job, limiter=None, provider=None, policy=POLICY):
    result = process_email_job(job.pk, limiter=limiter or StubLimiter(), provider=provider or StubProvider(), policy=policy)
    job.refresh_from_db()
    return result


def test_successful_send_is_recorded():
    job = make_job()
    result = run(job)

    assert result.outcome == Outcome.SENT
    assert (job.status, job.attempts, job.provider_message_id) == (Status.SENT, 1, "provider-msg-1")
    assert job.sent_at is not None


def test_transient_failure_retries_with_exponential_backoff_then_succeeds():
    job = make_job()
    provider = StubProvider(TransientEmailError("503"), TransientEmailError("503"))

    first, second, third = run(job, provider=provider), run(job, provider=provider), run(job, provider=provider)

    assert first.outcome == second.outcome == Outcome.RETRY
    assert 5 <= first.countdown <= 10  # attempt 1: base 10 s, equal jitter -> [5, 10]
    assert 10 <= second.countdown <= 20  # attempt 2: doubled
    assert third.outcome == Outcome.SENT
    assert (job.status, job.attempts, provider.calls) == (Status.SENT, 3, 3)


def test_failed_attempt_leaves_job_retrying_with_error():
    job = make_job()
    run(job, provider=StubProvider(TransientEmailError("503 service unavailable")))

    assert job.status == Status.RETRYING
    assert job.last_error == "TransientEmailError: 503 service unavailable"


def test_exhausted_retries_are_dead_lettered():
    job = make_job()
    provider = StubProvider(*[TransientEmailError("503")] * 5)

    outcomes = [run(job, provider=provider).outcome for _ in range(POLICY.max_attempts)]

    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.DEAD]
    assert (job.status, job.attempts) == (Status.DEAD, 3)
    assert job.dead_letter.reason == DeadLetter.Reason.RETRIES_EXHAUSTED
    assert job.dead_letter.attempts == 3


def test_permanent_failure_is_dead_lettered_without_retrying():
    job = make_job()
    result = run(job, provider=StubProvider(PermanentEmailError("550 no such mailbox")))

    assert result.outcome == Outcome.DEAD
    assert (job.status, job.attempts) == (Status.DEAD, 1)
    assert job.dead_letter.reason == DeadLetter.Reason.PERMANENT_FAILURE


def test_rate_limited_job_is_deferred_without_using_an_attempt():
    job = make_job()
    provider = StubProvider()

    result = run(job, limiter=StubLimiter(allowed=False, retry_after_ms=1_500), provider=provider)

    assert (result.outcome, result.countdown) == (Outcome.DEFERRED, 1.5)
    assert (job.status, job.attempts, provider.calls) == (Status.QUEUED, 0, 0)


def test_fails_closed_when_rate_limiter_is_unavailable():
    job = make_job()
    provider = StubProvider()

    result = run(job, limiter=StubLimiter(error=RateLimiterUnavailable("connection refused")), provider=provider)

    assert (result.outcome, result.countdown) == (Outcome.DEFERRED, POLICY.limiter_down_retry_s)
    assert (job.status, provider.calls) == (Status.QUEUED, 0)


def test_bulk_mail_asks_for_the_reduced_limit_and_otp_for_the_full_one():
    limiter = StubLimiter()
    run(make_job(kind=EmailJob.Kind.ORDER_CONFIRMATION), limiter=limiter)
    run(make_job(kind=EmailJob.Kind.OTP), limiter=limiter)

    assert limiter.limits_requested == [8, 10]


def test_duplicate_message_for_a_finished_job_is_a_no_op():
    job = make_job(status=Status.SENT)
    limiter, provider = StubLimiter(), StubProvider()

    assert run(job, limiter=limiter, provider=provider).outcome == Outcome.SKIPPED
    assert (limiter.limits_requested, provider.calls) == ([], 0)


def test_job_held_by_a_live_worker_waits_for_the_claim_to_expire():
    job = make_job(status=Status.SENDING, claimed_at=timezone.now() - timedelta(seconds=30), attempts=1)
    provider = StubProvider()

    result = run(job, provider=provider)

    assert result.outcome == Outcome.DEFERRED
    assert 59 <= result.countdown <= 60
    assert (job.status, job.attempts, provider.calls) == (Status.SENDING, 1, 0)


def test_job_abandoned_by_a_dead_worker_is_reclaimed_and_sent():
    job = make_job(status=Status.SENDING, claimed_at=timezone.now() - timedelta(seconds=120), attempts=1)

    assert run(job).outcome == Outcome.SENT
    assert (job.status, job.attempts) == (Status.SENT, 2)


def test_job_that_kills_its_worker_every_time_is_dead_lettered():
    """A poison message: every attempt dies mid-call, so no failure is ever
    recorded. Attempts are counted at claim time, so it still runs out."""
    job = make_job()
    provider = StubProvider(*[WorkerKilled()] * 10)

    for _ in range(POLICY.max_attempts):
        with pytest.raises(WorkerKilled):
            run(job, provider=provider)
        # Redelivery arrives once the dead attempt's claim has expired.
        expired = timezone.now() - timedelta(seconds=POLICY.claim_ttl_s + 1)
        EmailJob.objects.filter(pk=job.pk).update(claimed_at=expired)

    result = run(job, provider=provider)

    assert result.outcome == Outcome.DEAD
    assert (job.status, job.attempts, provider.calls) == (Status.DEAD, POLICY.max_attempts, POLICY.max_attempts)
    assert job.dead_letter.reason == DeadLetter.Reason.RETRIES_EXHAUSTED
    assert "worker died" in job.dead_letter.error


def test_job_claimed_by_another_worker_between_read_and_claim_is_deferred():
    job = make_job()
    provider = StubProvider()

    def another_worker_claims_it():
        EmailJob.objects.filter(pk=job.pk).update(status=Status.SENDING, claimed_at=timezone.now(), attempts=1)

    result = run(job, limiter=StubLimiter(during_call=another_worker_claims_it), provider=provider)

    assert (result.outcome, result.detail) == (Outcome.DEFERRED, "claimed concurrently")
    assert (job.attempts, provider.calls) == (1, 0)  # their attempt, not ours


def test_message_for_an_unknown_job_is_skipped():
    result = process_email_job(uuid.uuid4(), limiter=StubLimiter(), provider=StubProvider(), policy=POLICY)
    assert (result.outcome, result.detail) == (Outcome.SKIPPED, "unknown job")


def test_late_result_from_a_superseded_attempt_is_discarded():
    """Our attempt overran its claim and another worker re-claimed and sent the
    job meanwhile. Our late failure must not flip SENT back to RETRYING."""
    job = make_job()

    def another_worker_takes_over():
        EmailJob.objects.filter(pk=job.pk).update(status=Status.SENT, claimed_at=timezone.now())

    run(job, provider=StubProvider(TransientEmailError("timeout"), during_call=another_worker_takes_over))

    assert job.status == Status.SENT


def test_backoff_is_exponential_jittered_and_capped():
    assert [POLICY.backoff(n, rand=lambda: 0.0) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]
    assert [POLICY.backoff(n, rand=lambda: 1.0) for n in (1, 2, 3)] == [10.0, 20.0, 40.0]
    assert POLICY.backoff(10, rand=lambda: 1.0) == POLICY.backoff_max_s
