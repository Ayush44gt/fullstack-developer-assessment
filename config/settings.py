"""Settings for the assessment project.

Everything that differs between a laptop, the test suite and a production box
is read from the environment, so no secret ever needs to be committed.
"""

import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes"}


# No key is committed. Without DJANGO_SECRET_KEY we use an ephemeral key, which
# is fine locally (it only invalidates sessions on restart).
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

# django-silk records every request and its SQL; it is a dev-only profiler.
ENABLE_SILK = env_bool("ENABLE_SILK", DEBUG)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "orders",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if ENABLE_SILK:
    INSTALLED_APPS.append("silk")
    # Must come first: nothing before it may short-circuit process_request.
    MIDDLEWARE.insert(0, "silk.middleware.SilkyMiddleware")
    SILKY_PYTHON_PROFILER = False
    SILKY_META = True  # also record silk's own overhead per request
    SILKY_AUTHENTICATION = True
    SILKY_AUTHORISATION = True  # /silk/ is staff-only
    SILKY_MAX_RECORDED_REQUESTS = 2_000

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_NAME", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            # WAL lets the web process and Celery workers read while one writes;
            # IMMEDIATE + timeout makes concurrent writers queue instead of erroring.
            "init_command": "PRAGMA journal_mode=WAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
        # A file (not :memory:) so worker subprocesses can share the test database.
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"notifications": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}

# --- Section 2: email queue ---------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_IGNORE_RESULT = True  # job state lives in EmailJob rows, not a result backend
CELERY_TASK_DEFAULT_QUEUE = "default"

# Crash safety. ANSWERS.md ("Section 2: SIGKILL") explains each one.
CELERY_TASK_ACKS_LATE = True  # ack when the task returns, not when it starts
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # child process killed mid-task -> requeue, don't ack
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # a dead worker strands at most `concurrency` messages
CELERY_BROKER_TRANSPORT_OPTIONS = {
    # Unacked messages of a worker that vanished are redelivered after this.
    # Must be longer than any countdown we schedule, or Redis would redeliver
    # deferred messages that a live worker is still holding.
    "visibility_timeout": int(os.environ.get("CELERY_VISIBILITY_TIMEOUT", 3600)),
}
CELERY_BEAT_SCHEDULE = {
    "republish-stuck-email-jobs": {"task": "notifications.republish_stuck_jobs", "schedule": 300.0},
}

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "notifications.providers.SimulatedEmailProvider")

# Read by notifications.policy.QueuePolicy; DESIGN.md explains every value.
EMAIL_QUEUE = {
    "RATE_KEY": "ratelimit:email-provider",
    "RATE_LIMIT": 200,  # provider allows 200 emails...
    "RATE_WINDOW_MS": 60_000,  # ...per minute
    "RATE_GUARD_MS": 1_000,  # we measure over 61 s to absorb send->provider latency jitter
    "CRITICAL_RESERVE": 20,  # slots per window that only OTP/alert emails may use
    "MAX_ATTEMPTS": 6,  # provider attempts before a job is dead-lettered
    "BACKOFF_BASE_S": 10.0,  # 10, 20, 40, 80, 160 s (each with jitter)
    "BACKOFF_MAX_S": 300.0,
    "THROTTLE_JITTER_S": 2.0,  # spread jobs that wake up for the same free slot
    # A SENDING claim older than this belongs to a dead worker. Must exceed the
    # send task's hard time_limit (45 s), after which a live attempt is killed.
    "CLAIM_TTL_S": float(os.environ.get("EMAIL_CLAIM_TTL_S", 90)),
    "LIMITER_DOWN_RETRY_S": 5.0,
    "UNPUBLISHED_AFTER_S": 60.0,  # sweeper: QUEUED but the broker never took the message
    "STALE_AFTER_S": 1_800.0,  # sweeper: no progress for 30 min -> assume the message was lost
}
