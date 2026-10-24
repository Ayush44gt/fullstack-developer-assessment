import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import EmailJob

log = logging.getLogger(__name__)

CRITICAL_QUEUE = "emails.critical"
BULK_QUEUE = "emails.bulk"


def enqueue_email(*, kind, to, subject, body):
    """Record the email durably, then hand it to Celery once the row is committed.

    Transactional outbox: if the caller's transaction rolls back, nothing is
    queued; if Redis is down at commit time the row stays QUEUED with
    enqueued_at=NULL and `republish_stuck_jobs` publishes it later.
    """
    job = EmailJob.objects.create(kind=kind, to=to, subject=subject, body=body)
    transaction.on_commit(lambda: publish(job))
    return job


def publish(job):
    from .tasks import send_email  # tasks imports this module

    queue = CRITICAL_QUEUE if job.is_critical else BULK_QUEUE
    try:
        send_email.apply_async(args=[str(job.pk)], queue=queue)
    except Exception:  # broker unreachable; the row is safe and the sweeper will retry
        log.exception("job %s: could not publish, the sweeper will retry", job.pk)
        return False
    EmailJob.objects.filter(pk=job.pk, enqueued_at__isnull=True).update(enqueued_at=timezone.now())
    return True


def republish_stuck_jobs(policy, now=None):
    """Re-publish jobs whose message the broker never got or has lost.

    - QUEUED and never published: Redis was down when the job was created.
    - SENDING under a claim older than `claim_ttl_s` and untouched since: the
      attempt died and nothing will redeliver it. A hard time-limit kill does
      this, because Celery acks the message (task_acks_on_failure_or_timeout).
    - Unfinished and untouched for `stale_after_s`: the message was lost, e.g.
      Redis restarted without its AOF.

    Re-publishing a job that still has a live message is harmless: the
    duplicate either finds the job finished or loses the claim.
    """
    now = now or timezone.now()
    stuck = EmailJob.objects.exclude(status__in=EmailJob.TERMINAL).filter(
        Q(enqueued_at__isnull=True, created_at__lt=now - timedelta(seconds=policy.unpublished_after_s))
        | Q(status=EmailJob.Status.SENDING, updated_at__lt=now - timedelta(seconds=policy.claim_ttl_s))
        | Q(updated_at__lt=now - timedelta(seconds=policy.stale_after_s))
    )
    republished = 0
    for job in stuck.iterator():
        EmailJob.objects.filter(pk=job.pk).update(updated_at=now)  # not picked again next sweep
        republished += publish(job)
    if republished:
        log.warning("republished %s stuck email job(s)", republished)
    return republished


def replay_dead_letter(dead_letter):
    """Give a dead job a fresh set of attempts, once the cause is fixed."""
    now = timezone.now()
    with transaction.atomic():
        reset = EmailJob.objects.filter(pk=dead_letter.job_id, status=EmailJob.Status.DEAD).update(
            status=EmailJob.Status.QUEUED, attempts=0, claimed_at=None, last_error="", updated_at=now
        )
        if not reset:
            return False
        dead_letter.replayed_at = now
        dead_letter.save(update_fields=["replayed_at"])
        job = dead_letter.job
        transaction.on_commit(lambda: publish(job))
    return True
