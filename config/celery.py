import os
import sys

# billiard >= 4.3 starts prefork pool children with "spawn" on macOS (fork is
# unsafe there). Spawned children must redo Celery's per-process setup, which
# Celery only does when this flag is set; without it every task fails with
# "not enough values to unpack". Linux keeps fork and is unaffected.
if sys.platform == "darwin":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

from celery import Celery  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
