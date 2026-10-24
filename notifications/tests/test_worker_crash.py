"""What happens to an in-flight email when a worker is SIGKILL'd (ANSWERS.md, Section 2).

Runs real `celery worker` subprocesses (prefork pool) against a real Redis
broker, so these tests are skipped when no Redis server is reachable.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection

from config.celery import app
from notifications.models import EmailJob
from notifications.tasks import send_email

from .helpers import TEST_REDIS_URL, real_redis

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(real_redis() is None, reason="needs a real Redis server (docker compose up -d redis)"),
]

CLAIM_TTL_S = 3
VISIBILITY_TIMEOUT_S = 5


class Worker:
    """A `celery worker` subprocess in its own process group, consuming one queue."""

    def __init__(self, queue, log_dir):
        self.log_path = Path(log_dir) / f"worker-{uuid.uuid4().hex[:6]}.log"
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DJANGO_DEBUG": "0",
            "ENABLE_SILK": "0",
            "DJANGO_DB_NAME": str(connection.settings_dict["NAME"]),  # the test database
            "REDIS_URL": TEST_REDIS_URL,
            "CELERY_BROKER_URL": TEST_REDIS_URL,
            "EMAIL_CLAIM_TTL_S": str(CLAIM_TTL_S),
            "CELERY_VISIBILITY_TIMEOUT": str(VISIBILITY_TIMEOUT_S),
        }
        command = [
            sys.executable, "-m", "celery", "-A", "config", "worker",
            "--pool", "prefork", "--concurrency", "1", "-Q", queue,
            "-n", f"crash-test-{uuid.uuid4().hex[:6]}@%h",
            "--without-mingle", "--without-gossip", "--without-heartbeat", "--loglevel", "INFO",
        ]  # fmt: skip
        self.log = open(self.log_path, "w")
        self.process = subprocess.Popen(
            command, cwd=settings.BASE_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True
        )
        wait_until(lambda: " ready." in self.log_path.read_text(), timeout=30, what="worker ready")

    def kill_everything(self):
        """SIGKILL the main process and its pool children: an OOM-killed container."""
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=10)

    def stop(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
        self.log.close()


def wait_until(condition, timeout, what):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.1)


@pytest.fixture
def queue():
    name = f"crash-test-{uuid.uuid4().hex[:8]}"
    yield name
    real_redis().delete(name)


@pytest.fixture
def workers(queue, tmp_path):
    started = []

    def start():
        started.append(Worker(queue, tmp_path))
        return started[-1]

    yield start
    for worker in started:
        worker.stop()


def submit_slow_job(queue):
    """A job whose first provider call hangs for 5 s, so we can kill the worker mid-send."""
    job = EmailJob.objects.create(kind=EmailJob.Kind.OTP, to="victim+slow@example.com", subject="s", body="b")
    with app.connection_for_write(TEST_REDIS_URL) as conn:
        send_email.apply_async(args=[str(job.pk)], queue=queue, connection=conn)
    return job


def provider_calls(job):
    return int(real_redis().get(f"sim-provider:calls:{job.pk}") or 0)


def wait_for_pid_mid_send(job):
    """PID of the pool process currently blocked inside the provider call."""
    key = f"sim-provider:pid:{job.pk}"
    wait_until(lambda: real_redis().get(key), timeout=20, what="provider call to start")
    return int(real_redis().get(key))


def wait_for_status(job, status, timeout):
    wait_until(lambda: EmailJob.objects.get(pk=job.pk).status == status, timeout=timeout, what=status)
    job.refresh_from_db()


def test_pool_child_killed_mid_send_is_requeued_and_delivered(queue, workers):
    """Only the child running the task dies (the kernel OOM killer's usual pick).

    The main process sees the child vanish; task_reject_on_worker_lost makes it
    reject the message with requeue instead of acking it. The redelivered task
    finds the job SENDING under a fresh claim, waits out CLAIM_TTL_S, re-claims
    it and delivers it.
    """
    workers()
    job = submit_slow_job(queue)
    child_pid = wait_for_pid_mid_send(job)
    assert EmailJob.objects.get(pk=job.pk).status == EmailJob.Status.SENDING

    os.kill(child_pid, signal.SIGKILL)

    wait_for_status(job, EmailJob.Status.SENT, timeout=30)
    assert job.attempts == 2  # the killed attempt still counts
    assert provider_calls(job) == 2


def test_whole_worker_killed_mid_send_is_redelivered_after_visibility_timeout(queue, workers):
    """Main process and children all die, so nobody is left to reject the message.

    It stays in Redis's `unacked` hash. A surviving or newly started worker
    restores it once it is older than visibility_timeout, and it is delivered.
    """
    first = workers()
    job = submit_slow_job(queue)
    wait_for_pid_mid_send(job)

    first.kill_everything()
    killed_at = time.monotonic()
    assert real_redis().hlen("unacked") >= 1  # the message survived, unacknowledged
    time.sleep(0.5)
    job.refresh_from_db()
    assert job.status == EmailJob.Status.SENDING and provider_calls(job) == 1  # nobody is working on it

    # kombu restores unacked messages at worker startup (then about every 100 s),
    # and only those older than the visibility timeout.
    wait_until(lambda: time.monotonic() - killed_at > VISIBILITY_TIMEOUT_S + 1, timeout=10, what="timeout")
    workers()

    wait_for_status(job, EmailJob.Status.SENT, timeout=30)
    assert job.attempts == 2
    assert provider_calls(job) == 2
