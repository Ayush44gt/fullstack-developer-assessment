import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from notifications.models import EmailJob
from notifications.policy import QueuePolicy
from notifications.redis_client import get_redis
from notifications.services import BULK_QUEUE, CRITICAL_QUEUE

STATUSES = [s.value for s in EmailJob.Status]


class Command(BaseCommand):
    help = "Live view of the email queue: Redis queue depth, rate-limit window and job states."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=2.0)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, interval, once, **opts):
        r, policy = get_redis(), QueuePolicy.from_settings()
        self.stdout.write(
            "Columns: messages waiting in each Redis list | 'unacked' = reserved by a worker "
            "(running, or holding a countdown) | sends admitted in the limiter's "
            f"{policy.limiter_window_ms / 1000:g}s window (limit {policy.rate_limit}) | "
            "emails sent in the last 60s | job states\n"
        )
        peak = 0
        while True:
            seconds, micros = r.time()  # the limiter's clock, not ours
            now_ms = seconds * 1000 + micros // 1000
            in_window = r.zcount(policy.rate_key, now_ms - policy.limiter_window_ms + 1, "+inf")
            peak = max(peak, in_window)
            sent_60s = EmailJob.objects.filter(sent_at__gte=timezone.now() - timedelta(seconds=60)).count()
            states = dict(EmailJob.objects.values_list("status").annotate(n=Count("pk")))
            self.stdout.write(
                f"{timezone.localtime():%H:%M:%S}  bulk={r.llen(BULK_QUEUE):<5} critical={r.llen(CRITICAL_QUEUE):<4} "
                f"unacked={r.hlen('unacked'):<5} window={in_window:>3}/{policy.rate_limit} (peak {peak:>3})  "
                f"sent/60s={sent_60s:<4} | " + "  ".join(f"{s}={states.get(s, 0)}" for s in STATUSES)
            )
            if once:
                return
            time.sleep(interval)
