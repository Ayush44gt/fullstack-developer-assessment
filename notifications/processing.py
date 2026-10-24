"""One delivery attempt for one email job.

Kept free of Celery so every branch is unit-testable; tasks.py turns the
returned Result into a Celery retry. The flow:

    load job -> skip if finished -> wait if another worker holds it
    -> take a rate-limit slot -> claim the row -> call the provider
    -> record SENT / schedule a retry / dead-letter
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import DeadLetter, EmailJob
from .providers import PermanentEmailError
from .rate_limiter import RateLimiterUnavailable

log = logging.getLogger(__name__)
Status = EmailJob.Status


class AttemptAbandoned(Exception):
    """The worker running an attempt died before recording any result."""


class Outcome(str, Enum):
    SENT = "sent"
    DEFERRED = "deferred"  # not attempted (no slot / Redis down / held elsewhere); not counted as an attempt
    RETRY = "retry"  # attempted, failed transiently; try again after backoff
    DEAD = "dead"  # dead-lettered
    SKIPPED = "skipped"  # nothing to do: already finished, or unknown id


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    countdown: float = 0.0  # seconds until the next run, for DEFERRED / RETRY
    detail: str = ""


def process_email_job(job_id, *, limiter, provider, policy):
    job = EmailJob.objects.filter(pk=job_id).first()
    if job is None:
        return Result(Outcome.SKIPPED, detail="unknown job")
    if job.status in EmailJob.TERMINAL:
        # A duplicate message: redelivery after a crash, or a sweeper re-publish.
        return Result(Outcome.SKIPPED, detail=f"already {job.status}")

    now = timezone.now()
    if job.status == Status.SENDING:
        held_for = (now - job.claimed_at).total_seconds()
        if held_for < policy.claim_ttl_s:
            # Maybe still in flight on another worker. Look again once its claim
            # has expired; by then that attempt has finished or was killed.
            return Result(Outcome.DEFERRED, policy.claim_ttl_s - held_for, "held by another worker")
        if job.attempts >= policy.max_attempts:
            # The last allowed attempt's worker died mid-call (SIGKILL, OOM, hard
            # time limit), so no except-branch ever ran. Stop here: a job that
            # kills its worker every time must not crash-loop forever.
            exc = AttemptAbandoned(f"worker died during attempt {job.attempts}; no result recorded")
            _dead_letter(job, DeadLetter.Reason.RETRIES_EXHAUSTED, exc)
            return Result(Outcome.DEAD, detail=_describe(exc))

    try:
        decision = limiter.try_acquire(limit=policy.limit_for(critical=job.is_critical))
    except RateLimiterUnavailable as exc:
        # Fail closed: a late email is better than breaching the provider limit.
        log.warning("job %s: rate limiter unavailable, deferring: %s", job.pk, exc)
        return Result(Outcome.DEFERRED, policy.limiter_down_retry_s, "rate limiter unavailable")
    if not decision.allowed:
        return Result(Outcome.DEFERRED, policy.throttle_delay(decision.retry_after_ms), "rate limited")

    if not _claim(job, now, policy):
        # Another worker claimed it between our read and our UPDATE. The slot we
        # took is wasted, which errs on the safe side of the limit.
        return Result(Outcome.DEFERRED, policy.claim_ttl_s, "claimed concurrently")

    try:
        message_id = provider.send(idempotency_key=str(job.pk), to=job.to, subject=job.subject, body=job.body)
    except PermanentEmailError as exc:
        _dead_letter(job, DeadLetter.Reason.PERMANENT_FAILURE, exc)
        return Result(Outcome.DEAD, detail=_describe(exc))
    except Exception as exc:  # transient provider errors, timeouts and our own bugs alike
        if job.attempts >= policy.max_attempts:
            _dead_letter(job, DeadLetter.Reason.RETRIES_EXHAUSTED, exc)
            return Result(Outcome.DEAD, detail=_describe(exc))
        _finish(job, status=Status.RETRYING, last_error=_describe(exc))
        delay = policy.backoff(job.attempts)
        log.info("job %s: attempt %s failed (%s), retrying in %.1fs", job.pk, job.attempts, exc, delay)
        return Result(Outcome.RETRY, delay, _describe(exc))

    _finish(job, status=Status.SENT, provider_message_id=message_id, sent_at=timezone.now(), last_error="")
    log.info("job %s: sent on attempt %s", job.pk, job.attempts)
    return Result(Outcome.SENT)


def _claim(job, now, policy):
    """Move the job to SENDING with one conditional UPDATE.

    If two workers hold messages for the same job, the database lets exactly
    one UPDATE match. The attempt is counted here, before the provider call, so
    an attempt whose worker dies still uses up one of the job's attempts.
    """
    claimable = Q(status__in=[Status.QUEUED, Status.RETRYING]) | Q(
        status=Status.SENDING, claimed_at__lt=now - timedelta(seconds=policy.claim_ttl_s)
    )
    claimed = EmailJob.objects.filter(claimable, pk=job.pk, attempts__lt=policy.max_attempts).update(
        status=Status.SENDING, claimed_at=now, attempts=F("attempts") + 1, updated_at=now
    )
    if claimed:
        job.refresh_from_db()
    return bool(claimed)


def _finish(job, **fields):
    """Write the attempt's result, but only if our claim is still the current one.

    `claimed_at` works as a fencing token: if this attempt overran its claim and
    another worker re-claimed the job, our late write must not overwrite theirs.
    """
    updated = EmailJob.objects.filter(pk=job.pk, claimed_at=job.claimed_at).update(
        **fields, updated_at=timezone.now()
    )
    if not updated:
        log.warning("job %s: claim was taken over, discarding result %s", job.pk, fields.get("status"))
    return bool(updated)


def _dead_letter(job, reason, exc):
    with transaction.atomic():
        if _finish(job, status=Status.DEAD, last_error=_describe(exc)):
            DeadLetter.objects.update_or_create(
                job=job,
                defaults={
                    "reason": reason,
                    "error": _describe(exc),
                    "attempts": job.attempts,
                    "created_at": timezone.now(),
                    "replayed_at": None,
                },
            )
    log.error("job %s: dead-lettered after %s attempt(s) (%s): %s", job.pk, job.attempts, reason, exc)


def _describe(exc):
    return f"{type(exc).__name__}: {exc}"[:2000]
