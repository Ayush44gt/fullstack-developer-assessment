import os
import threading
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string

from . import redis_client


class TransientEmailError(Exception):
    """Worth retrying: timeouts, 5xx, a 429 from the provider."""


class PermanentEmailError(Exception):
    """Retrying cannot help: invalid address, rejected content (other 4xx)."""


def get_provider():
    return import_string(settings.EMAIL_PROVIDER)()


@dataclass(frozen=True)
class ProviderCall:
    at: float  # time.monotonic() when the provider received the call
    key: str
    to: str
    outcome: str  # "accepted" or the exception class name


class SimulatedEmailProvider:
    """Stands in for the third-party email API (no network).

    Failures are scripted by a tag in the recipient's local part:

        name+flaky@...   TransientEmailError on the first 2 calls, then accepted
        name+down@...    TransientEmailError on every call
        name+bounce@...  PermanentEmailError
        name+slow@...    first call blocks for SLOW_SECONDS (the SIGKILL test kills it mid-call)

    Per-job call counts live in Redis, so they are shared by every worker
    process. In-process callers (tests) can also read `calls`.
    """

    FLAKY_FAILURES = 2
    SLOW_SECONDS = 5.0
    calls = []
    _lock = threading.Lock()

    def send(self, *, idempotency_key, to, subject, body):
        tag = to.partition("@")[0].partition("+")[2]
        r = redis_client.get_redis()
        call_no = r.incr(f"sim-provider:calls:{idempotency_key}")
        r.expire(f"sim-provider:calls:{idempotency_key}", 86_400)

        outcome = "accepted"
        try:
            if tag == "slow" and call_no == 1:
                r.set(f"sim-provider:pid:{idempotency_key}", os.getpid(), ex=600)
                time.sleep(self.SLOW_SECONDS)  # a provider that hangs, not a rate limiter
            if tag == "bounce":
                raise PermanentEmailError(f"550 mailbox unavailable: {to}")
            if tag == "down" or (tag == "flaky" and call_no <= self.FLAKY_FAILURES):
                raise TransientEmailError("503 service unavailable")
            return f"sim-{uuid.uuid4().hex[:12]}"
        except Exception as exc:
            outcome = type(exc).__name__
            raise
        finally:
            with self._lock:
                self.calls.append(ProviderCall(time.monotonic(), idempotency_key, to, outcome))
