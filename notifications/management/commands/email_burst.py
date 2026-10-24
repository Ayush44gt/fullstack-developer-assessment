import time

from django.core.management.base import BaseCommand

from notifications.models import EmailJob
from notifications.services import enqueue_email


class Command(BaseCommand):
    help = "Simulate a flash-sale burst: enqueue emails as fast as possible."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=500)
        parser.add_argument("--otp-every", type=int, default=20, help="every Nth email is an OTP (critical queue)")
        parser.add_argument("--flaky", type=int, default=3, help="emails that fail twice with a 503, then succeed")
        parser.add_argument("--bounce", type=int, default=1, help="emails that hard-bounce (dead-lettered at once)")

    def handle(self, *args, count, otp_every, flaky, bounce, **opts):
        recipients = [f"flaky{n}+flaky@example.com" for n in range(flaky)]
        recipients += [f"gone{n}+bounce@example.com" for n in range(bounce)]
        recipients += [f"customer{n}@example.com" for n in range(count - len(recipients))]

        started = time.monotonic()
        for n, to in enumerate(recipients):
            otp = otp_every and n % otp_every == 0
            enqueue_email(
                kind=EmailJob.Kind.OTP if otp else EmailJob.Kind.ORDER_CONFIRMATION,
                to=to,
                subject="Your login code" if otp else f"Order #{n} confirmed",
                body="...",
            )
        elapsed = time.monotonic() - started
        self.stdout.write(self.style.SUCCESS(f"Enqueued {len(recipients)} emails in {elapsed:.1f}s"))
