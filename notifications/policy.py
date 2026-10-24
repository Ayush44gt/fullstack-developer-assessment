import random
from dataclasses import dataclass, fields

from django.conf import settings


@dataclass(frozen=True)
class QueuePolicy:
    """The email queue's tunables, read from settings.EMAIL_QUEUE (documented there)."""

    rate_key: str
    rate_limit: int
    rate_window_ms: int
    rate_guard_ms: int
    critical_reserve: int
    max_attempts: int
    backoff_base_s: float
    backoff_max_s: float
    throttle_jitter_s: float
    claim_ttl_s: float
    limiter_down_retry_s: float
    unpublished_after_s: float
    stale_after_s: float

    @classmethod
    def from_settings(cls):
        return cls(**{f.name: settings.EMAIL_QUEUE[f.name.upper()] for f in fields(cls)})

    @property
    def limiter_window_ms(self):
        return self.rate_window_ms + self.rate_guard_ms

    def limit_for(self, *, critical):
        """Bulk mail may only fill the window up to limit - reserve, so a flash
        sale's order confirmations can never use the slots OTPs need."""
        return self.rate_limit if critical else self.rate_limit - self.critical_reserve

    def backoff(self, attempt, rand=random.random):
        """Delay before retrying after failed attempt number `attempt` (1-based).

        Exponential (base * 2^(attempt-1), capped) with "equal jitter": the
        result is in [d/2, d]. The fixed half guarantees a real pause; the
        random half stops jobs that failed together from retrying together.
        """
        delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** (attempt - 1))
        return delay / 2 + rand() * delay / 2

    def throttle_delay(self, retry_after_ms, rand=random.random):
        """When to re-check after the limiter said no: the moment a slot frees up, plus jitter."""
        return retry_after_ms / 1000 + rand() * self.throttle_jitter_s
