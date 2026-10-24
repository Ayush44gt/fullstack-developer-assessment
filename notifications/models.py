import uuid

from django.db import models
from django.utils import timezone


class EmailJob(models.Model):
    """One transactional email.

    The row is the source of truth: it is written before anything is queued,
    and the Celery message only carries its id ("look at job <id> now").
    """

    class Kind(models.TextChoices):
        ORDER_CONFIRMATION = "order_confirmation"
        OTP = "otp"
        ALERT = "alert"

    class Status(models.TextChoices):
        QUEUED = "queued"  # waiting for a worker or a rate-limit slot
        SENDING = "sending"  # claimed by a worker; provider call may be in flight
        RETRYING = "retrying"  # last attempt failed transiently; backoff scheduled
        SENT = "sent"
        DEAD = "dead"  # gave up; see the DeadLetter row

    TERMINAL = (Status.SENT, Status.DEAD)
    CRITICAL_KINDS = (Kind.OTP, Kind.ALERT)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    to = models.EmailField()
    subject = models.CharField(max_length=200)
    body = models.TextField()

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)  # provider calls made (or begun)
    last_error = models.TextField(blank=True)
    provider_message_id = models.CharField(max_length=100, blank=True)

    claimed_at = models.DateTimeField(null=True, blank=True)  # also the fencing token, see processing.py
    enqueued_at = models.DateTimeField(null=True, blank=True)  # broker accepted the first message
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)  # set explicitly: QuerySet.update() skips auto_now

    class Meta:
        indexes = [models.Index(fields=["status", "updated_at"], name="emailjob_status_updated")]

    def __str__(self):
        return f"{self.kind} to {self.to} ({self.status})"

    @property
    def is_critical(self):
        return self.kind in self.CRITICAL_KINDS


class DeadLetter(models.Model):
    """A job that will not be retried automatically. Replay it from the admin."""

    class Reason(models.TextChoices):
        PERMANENT_FAILURE = "permanent_failure"  # provider said retrying can't help
        RETRIES_EXHAUSTED = "retries_exhausted"

    job = models.OneToOneField(EmailJob, on_delete=models.CASCADE, related_name="dead_letter")
    reason = models.CharField(max_length=32, choices=Reason.choices)
    error = models.TextField()
    attempts = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    replayed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.job_id}: {self.reason}"
