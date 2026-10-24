"""Getting jobs onto the queue: the outbox, the sweeper safety net, dead-letter replay, the API."""

from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from notifications.models import DeadLetter, EmailJob
from notifications.policy import QueuePolicy
from notifications.services import enqueue_email, replay_dead_letter, republish_stuck_jobs

pytestmark = pytest.mark.django_db


@pytest.fixture
def apply_async():
    with mock.patch("notifications.tasks.send_email.apply_async") as patched:
        yield patched


def enqueue(kind=EmailJob.Kind.ORDER_CONFIRMATION):
    return enqueue_email(kind=kind, to="a@example.com", subject="s", body="b")


def test_job_is_published_only_after_commit_to_the_right_queue(apply_async, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        bulk = enqueue()
        otp = enqueue(EmailJob.Kind.OTP)
        assert apply_async.call_count == 0  # nothing leaves before the row is committed

    assert [c.kwargs["queue"] for c in apply_async.call_args_list] == ["emails.bulk", "emails.critical"]
    assert apply_async.call_args_list[0].kwargs["args"] == [str(bulk.pk)]
    assert all(j.enqueued_at for j in EmailJob.objects.filter(pk__in=[bulk.pk, otp.pk]))


def test_rolled_back_request_queues_nothing(apply_async, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            enqueue()
            raise RuntimeError("request failed after enqueueing")

    assert not EmailJob.objects.exists()
    apply_async.assert_not_called()


def test_broker_outage_at_enqueue_is_recovered_by_the_sweeper(apply_async, django_capture_on_commit_callbacks):
    apply_async.side_effect = ConnectionError("redis down")
    with django_capture_on_commit_callbacks(execute=True):
        job = enqueue()
    job.refresh_from_db()
    assert (job.status, job.enqueued_at) == (EmailJob.Status.QUEUED, None)

    apply_async.side_effect = None
    later = timezone.now() + timedelta(minutes=2)
    assert republish_stuck_jobs(QueuePolicy.from_settings(), now=later) == 1

    job.refresh_from_db()
    assert job.enqueued_at is not None


def test_sweeper_republishes_lost_messages_and_leaves_finished_jobs(apply_async):
    an_hour_ago = timezone.now() - timedelta(hours=1)
    fields = dict(kind=EmailJob.Kind.ALERT, to="a@example.com", subject="s", body="b", enqueued_at=an_hour_ago)
    lost = EmailJob.objects.create(**fields, updated_at=an_hour_ago)
    EmailJob.objects.create(**fields, status=EmailJob.Status.SENT, updated_at=an_hour_ago)
    EmailJob.objects.create(**fields)  # recently active

    assert republish_stuck_jobs(QueuePolicy.from_settings()) == 1
    assert apply_async.call_args.kwargs == {"args": [str(lost.pk)], "queue": "emails.critical"}
    assert republish_stuck_jobs(QueuePolicy.from_settings()) == 0  # not re-picked straight away


def test_sweeper_rescues_an_attempt_that_died_without_redelivery(apply_async):
    """A hard time-limit kill acks the message, leaving the job SENDING with no
    message anywhere. Once its claim has expired, the sweeper re-publishes it."""
    policy = QueuePolicy.from_settings()
    abandoned_at = timezone.now() - timedelta(seconds=policy.claim_ttl_s + 60)
    fields = dict(kind=EmailJob.Kind.OTP, to="a@example.com", subject="s", body="b", status=EmailJob.Status.SENDING)
    abandoned = EmailJob.objects.create(**fields, claimed_at=abandoned_at, updated_at=abandoned_at, enqueued_at=abandoned_at)
    EmailJob.objects.create(**fields, claimed_at=timezone.now(), enqueued_at=abandoned_at)  # in flight right now

    assert republish_stuck_jobs(policy) == 1
    assert apply_async.call_args.kwargs["args"] == [str(abandoned.pk)]


def test_replaying_a_dead_letter_gives_fresh_attempts(apply_async, django_capture_on_commit_callbacks):
    job = EmailJob.objects.create(
        kind=EmailJob.Kind.OTP, to="a@example.com", subject="s", body="b", status=EmailJob.Status.DEAD, attempts=6
    )
    dead = DeadLetter.objects.create(job=job, reason=DeadLetter.Reason.RETRIES_EXHAUSTED, error="503", attempts=6)

    with django_capture_on_commit_callbacks(execute=True):
        assert replay_dead_letter(dead)

    job.refresh_from_db()
    dead.refresh_from_db()
    assert (job.status, job.attempts) == (EmailJob.Status.QUEUED, 0)
    assert dead.replayed_at is not None
    apply_async.assert_called_once()


def test_api_accepts_email_with_202(client, apply_async, django_capture_on_commit_callbacks):
    staff = get_user_model().objects.create_user("svc", is_staff=True)
    client.force_login(staff)
    payload = {"kind": "otp", "to": "user@example.com", "subject": "Your code", "body": "123456"}

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/api/notifications/emails/", payload, content_type="application/json")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    apply_async.assert_called_once()


def test_api_is_staff_only(client):
    client.force_login(get_user_model().objects.create_user("customer"))
    response = client.post("/api/notifications/emails/", {}, content_type="application/json")
    assert response.status_code == 403
