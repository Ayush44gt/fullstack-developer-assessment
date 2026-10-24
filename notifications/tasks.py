from celery import shared_task

from . import redis_client
from .policy import QueuePolicy
from .processing import Outcome, process_email_job
from .providers import get_provider
from .rate_limiter import SlidingWindowRateLimiter
from .services import republish_stuck_jobs


def build_limiter(policy):
    return SlidingWindowRateLimiter(
        redis_client.get_redis(),
        key=policy.rate_key,
        limit=policy.rate_limit,
        window_ms=policy.limiter_window_ms,
    )


@shared_task(
    bind=True,
    name="notifications.send_email",
    # Attempt limits and backoff live in QueuePolicy and are counted in the
    # EmailJob row, because rate-limit deferrals reuse self.retry() too and
    # must not use up attempts.
    max_retries=None,
    soft_time_limit=30,
    time_limit=45,  # keep below EMAIL_QUEUE["CLAIM_TTL_S"]
)
def send_email(self, job_id):
    policy = QueuePolicy.from_settings()
    result = process_email_job(
        job_id, limiter=build_limiter(policy), provider=get_provider(), policy=policy
    )
    if result.outcome in (Outcome.DEFERRED, Outcome.RETRY):
        # Publishes a new message with an ETA, and only then is this one acked
        # (acks_late). A crash in between duplicates the message; it never loses it.
        raise self.retry(countdown=result.countdown)
    return result.outcome.value


@shared_task(name="notifications.republish_stuck_jobs")
def republish_stuck_jobs_task():
    return republish_stuck_jobs(QueuePolicy.from_settings())
