# Full-Stack Developer Assessment

Ayush Garg · Python · Django · Celery · Redis

| Section | Code | Written reasoning |
|---|---|---|
| 1. Diagnose a broken system | `orders/` (fix), `orders/regression.py` (deployed bug), `orders/management/commands/profile_summary.py` | [ANSWERS.md § 1](ANSWERS.md#section-1-diagnose-a-broken-system) · [profiler evidence](docs/section1/profiling-evidence.md) |
| 2. Rate-limited async job queue | `notifications/` | [DESIGN.md](DESIGN.md) · [ANSWERS.md § 2 (SIGKILL)](ANSWERS.md#section-2-what-happens-to-in-flight-tasks-when-a-worker-is-sigkilld) |
| 3. Architecture review (A and B) | — | [ANSWERS.md § 3](ANSWERS.md#section-3-written-architecture-review) |
| 4. Recording (optional) | `email_burst`, `queue_monitor` commands | Loom: _to be added_ |

## Setup (about 3 minutes)

Requires **Python 3.10+** (developed on 3.14) and **Docker** (for Redis).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d --wait redis     # Redis 7.4 on localhost:6379, AOF enabled
python manage.py migrate
pytest
```

Expected: **35 passed** in about 40 s. The header line `redis: real server at redis://localhost:6379/15` confirms which Redis the tests used; they use Redis DB 15 and a throwaway `test_db.sqlite3`, never your data. Without Docker, the suite falls back to `fakeredis` and the two real-worker SIGKILL tests are skipped (**33 passed, 2 skipped**). No `.env` is needed; every setting has a local default (see `.env.example`).

## Section 1: reproduce the regression and the fix

```bash
git log --oneline                     # baseline -> FIN-231 deploy (serializer only) -> fix
python manage.py seed_orders          # a 5-order and a 250-order customer (prints their passwords)
python manage.py profile_summary      # django-silk before/after -> docs/section1/profiling-evidence.md
pytest orders                         # query-count guards: fixed = constant 4, regressed = exact N+1 formula
```

To browse the recorded requests in silk's UI: `python manage.py createsuperuser`, then `python manage.py runserver`, log in at <http://localhost:8000/admin/>, and open <http://localhost:8000/silk/requests/>. As a customer (seeded password) you can compare `/api/orders/summary/` with `/api/orders/summary/regressed/` in DRF's browsable API.

## Section 2: run the queue

Three terminals, each with the virtualenv active:

```bash
# 1. worker: critical queue listed first = polled first
celery -A config worker -Q emails.critical,emails.bulk --concurrency 4 -l INFO

# 2. live view: Redis queue depth, unacked/deferred messages, limiter window (x/200), job states
python manage.py queue_monitor

# 3. a flash-sale burst: 500 emails (3 fail twice then succeed, 1 hard-bounces, every 20th is an OTP)
python manage.py email_burst --count 500
```

You should see the limiter window climb to about 195/200 within a second (180 bulk plus OTPs using the 20-slot reserve) and never pass 200. The backlog drains in one-minute waves, the flaky emails retry with backoff (`attempt 1 failed (503 …), retrying in 7.0s` in the worker log), and the bounce lands in **Admin → Dead letters**, where the "Replay" action re-queues it. `celery -A config beat` runs the 5-minute sweeper that re-publishes stuck jobs.

Over HTTP, as a staff user: `POST /api/notifications/emails/` with `{"kind": "otp", "to": "...", "subject": "...", "body": "..."}` returns `202`; `GET /api/notifications/emails/<id>/` shows its status.

Key tests:

| Test | Proves |
|---|---|
| `notifications/tests/test_queue_500.py` | 500 jobs through a real Celery worker: none lost, each delivered exactly once, rate never exceeded *at the provider*, flaky jobs retried with backoff, failures dead-lettered |
| `notifications/tests/test_rate_limiter.py` | The 2,000-in-10 s burst admits exactly 200; no rolling minute ever exceeds 200 over 10 min of overload; atomic under 16 racing threads; critical reserve; fails closed |
| `notifications/tests/test_worker_crash.py` | SIGKILL of a pool child → requeued and delivered; SIGKILL of the whole worker → redelivered after `visibility_timeout` |
| `notifications/tests/test_processing.py` | Every branch of a delivery attempt: backoff, dead-lettering, throttling, claims, fencing |

## Section 4: recording

_Loom link: to be added._ The recording follows the Section 2 steps above: a fresh worker, `queue_monitor` showing Redis state live, and a 500-email burst throttled at ≤ 200/min with the scripted failures retrying.

## Layout

```
config/          settings (all env-driven), Celery app, test settings
orders/          Section 1: models + queryset (with_billable_items), serializer, view, regression.py, commands
notifications/   Section 2: rate_limiter.py (Lua), processing.py (one attempt), tasks.py (Celery),
                 services.py (outbox, sweeper, replay), providers.py (simulated provider), policy.py
docs/section1/   generated profiler evidence
```

## Notes

- **No secrets are committed.** `DJANGO_SECRET_KEY` falls back to an ephemeral random key for local use; seed passwords are generated at runtime.
- **macOS:** `config/celery.py` sets `FORKED_BY_MULTIPROCESSING=1` on macOS only, because billiard ≥ 4.3 spawns (rather than forks) pool processes there. See ANSWERS.md, "Interpretations".
- **SQLite** (WAL mode) keeps setup to zero; production notes in the docs assume PostgreSQL.
