# DESIGN.md: Section 2, rate-limited email queue

## The problem in numbers

The provider accepts **200 emails/minute**. A flash sale delivers **2,000 requests in under 10 seconds**: ten minutes of sending at the provider's maximum, arriving in ten seconds. More workers would not help, because the bottleneck is the provider's quota. The queue has to do three things:

1. Hold every job durably while it waits, through worker crashes and Redis restarts.
2. Release jobs at no more than 200/min, as the provider counts them.
3. Keep time-critical mail moving. Behind a FIFO of 2,000 order confirmations, an OTP would wait about 10 minutes and expire before it arrived.

## Architecture

```
 POST /api/notifications/emails/            (or enqueue_email() from any view)
        │  INSERT EmailJob(status=queued)   ← source of truth, written first
        │  transaction.on_commit ──────────────► Redis list  emails.critical  (OTP, alerts)
        ▼                                                    emails.bulk      (order confirmations)
   202 Accepted                                                 │ BRPOP, critical first
                                                                ▼
                       Celery worker: send_email(job_id)   (notifications/processing.py)
                         1. load row; already sent/dead? ─────────────► drop (duplicate message)
                         2. held by a live worker?  ──────────────────► self.retry(countdown = claim TTL left)
                         3. rate limiter (Lua on Redis); no slot ─────► self.retry(countdown = when a slot frees)
                         4. claim: UPDATE … SET status=sending, attempts=attempts+1 WHERE status IN (…)
                         5. provider.send(idempotency_key=job.id)
                         6. sent ✓ │ transient: retrying + self.retry(backoff) │ permanent/exhausted: dead + DeadLetter

 celery beat, every 5 min: republish_stuck_jobs (on emails.critical), which re-publishes jobs the broker
                           never got or lost, and attempts that died with nothing left to redeliver them
```

A Celery message carries only a job id. Everything that matters (status, attempt count, last error, provider message id) lives in the `EmailJob` row. That one decision lets every failure mode be handled by asking the row what is true, instead of trusting that a message was delivered exactly once.

## Choice of queue: Celery + Redis vs Django Q vs custom

| | **Celery + Redis** (chosen) | **Django Q2** | **Custom on Redis Streams** |
|---|---|---|---|
| Delayed re-run ("come back when a slot frees", backoff) | Native: `self.retry(countdown=…)` publishes an ETA message | Failed tasks are re-presented after a fixed `retry` timeout; precise per-task delays need the polled `Schedule` model | Build it: a sorted set of due times plus a mover process |
| Crash recovery | `acks_late` + `reject_on_worker_lost`; Redis `visibility_timeout` for vanished workers | `retry` re-presents unacknowledged tasks; `ack_failures`, `max_attempts` | Best primitives of the three: `XREADGROUP`/`XACK`, `XAUTOCLAIM` by idle time, per-message delivery counts |
| Global rate limit | **None**: `rate_limit="200/m"` is per worker *instance*, so 4 workers = 800/min | None | Yours to write |
| Routing / priorities | Named queues, `-Q` order, transport `queue_order_strategy` | One queue per cluster; a priority lane means running a second cluster | Yours |
| Operations | Flower, signals, beat; most Django teams already run it | Simple; can use the Django DB as broker; admin views | You own supervision, graceful shutdown, monitoring and dead-letters |
| Main footguns | Defaults ack *early*; Redis `visibility_timeout` must exceed every countdown; ETA messages sit in worker memory | Smaller ecosystem; the original `django-q` is unmaintained (`django-q2` is the fork) | Everything above |

I chose Celery + Redis (the brief also asks for it), and here is why it fits. The hard generic parts are solid and built in: timed re-scheduling, routing, and hooks for crash recovery. What it lacks is small and domain-specific anyway: a *global* limiter, attempt accounting that ignores throttling, dead-letters, and idempotency. Those are the ~300 lines in `notifications/`.

What it costs: the Redis transport is *at-least-once with a visibility timeout*, not a broker with per-message acknowledgements like RabbitMQ. A deferred message is pulled into a worker's memory and sits in the `unacked` hash until its ETA. If that worker vanishes, the message waits out `visibility_timeout` before anyone else may take it. I accept that because the DB row plus the sweeper make lost or late messages recoverable, and running one Redis for both the broker and the limiter is simpler than adding RabbitMQ. A custom Streams queue would have better crash-recovery primitives, but it would mean re-implementing everything Celery already gives for free. That is the wrong place to spend effort for a 200/min workload.

## Rate limiter: sliding-window log (Option B)

`notifications/rate_limiter.py`. Each admitted send is a member of a sorted set, scored by its admission time in ms. One Lua script trims, counts and admits:

```lua
ZREMRANGEBYSCORE key -inf (now - window)   -- forget sends older than the window
ZCARD key                                  -- sends still in the window
if count < limit: ZADD key now <uuid>; PEXPIRE key window; return allowed
else: return retry_after = score of the (count-limit+1)-th oldest + window - now
```

### 1. Why this over the other two

The provider's "200 per minute" could be a fixed calendar minute or a rolling one; their docs rarely say which. A **sliding-window guarantee is the only one of the three that is safe under either reading**, because every calendar minute is also a rolling minute.

- **Fixed window (INCR + EXPIRE)**: one command, O(1) memory. But 200 sends at 00:59.9 and 200 at 01:00.0 are 400 in 0.2 s. That's double the limit for any provider that measures a rolling minute.
- **Token bucket**: smooth, with O(1) state. But with capacity *C* it admits *C + 200* in some 60 s window. To stay under 200 in every rolling minute, *C* must be near zero, which makes it a fixed-rate pacer (one send per 300 ms) that throws away the burst the provider allows. The refill is also a read-modify-write, so it needs Lua regardless.
- **Sliding-window log** is exact: never more than 200 admissions in any 60 s. Its cost is memory proportional to the limit: 200 members, a few KB, O(log N) per call. That is nothing at 200/min. At 100k/s I would switch to a sliding-window *counter* approximation or GCRA (generic cell rate algorithm).

The test `test_sustained_overload_never_exceeds_limit_in_any_rolling_minute` runs 10 minutes at 6× the limit. It shows both sides: the busiest rolling minute has exactly 200, and 2,000 are admitted in total, so the limiter is never stricter than it must be.

### 2. How atomicity is guaranteed

**A Lua script** (`register_script` calls EVALSHA; on `NOSCRIPT`, redis-py runs SCRIPT LOAD and retries). Redis executes a script to completion without interleaving any other client's commands, so two workers can never both see 199 and both add the 200th.

- A **pipeline** is not atomic at all: it only batches round trips, and other clients' commands interleave.
- **MULTI/EXEC** cannot branch on a value read inside the transaction (the ZCARD result). It would need WATCH plus an optimistic retry loop, and that loop degrades under exactly the contention a flash-sale burst creates.
- `test_concurrent_callers_never_over_admit` has 16 threads race for 100 slots; exactly 100 are granted and the set holds 100 members.

The script reads the clock with Redis `TIME`, so all workers share one clock and worker clock skew can't stretch the window. Redis ≥ 5 replicates script *effects*, which is what allows TIME before writes.

**Guard band.** The limiter admits a send at time *T*, but the provider counts it at *T + δ*, where δ (our network hop plus their ingress) varies. Two sends admitted exactly 60 s apart can arrive 59.9 s apart. So the window is configured as 60 s **+ 1 s guard** (`RATE_GUARD_MS`), at a cost of about 1.6% of throughput. This is not hypothetical. With the guard set to 0, the 500-job test, which measures *at the provider*, saw 51 calls in some 1 s window (limit 50) in 4 of 8 runs, across two separate batches. With the guard, every run has stayed at or below 50.

### 3. Redis failure: fail closed

If Redis can't be reached, `try_acquire` raises `RateLimiterUnavailable`. The job is deferred for 5 s without calling the provider and without using an attempt (`test_fails_closed_when_rate_limiter_is_unavailable`). Why closed:

- **The asymmetry of the damage.** Over-sending earns 429s at best. At worst the provider throttles or suspends the account, and then *all* mail stops, including OTPs, for hours. A delayed order confirmation is recoverable; a suspended sending account is not.
- **Failing open buys almost nothing here.** The limiter's Redis is also the Celery broker. When it is down, workers can't fetch new messages anyway; only messages already in memory could go out, and those are exactly the ones we'd be sending blind.
- **The cost:** a Redis outage is an email outage. The mitigation belongs at the infrastructure level: a replicated or managed Redis with failover. If the limiter ever moved to a separate Redis from the broker, I would revisit this. A middle ground would be to fail over to a conservative local limit of `limit / number_of_workers` per process instead of fully open.

The client uses 1 s socket timeouts, so a *hung* Redis fails as fast as a dead one instead of pinning workers.

### Priority: reserved headroom plus queue order

Bulk mail calls the limiter with `limit = 200 − 20`, while OTPs and alerts use the full 200, against the **same** sorted set. So the global count can never exceed 200, and bulk can never fill the last 20 slots. However big the flash sale, 20 critical emails per minute always have room (`test_bulk_traffic_cannot_use_the_critical_reserve`).

The two kinds are also routed to separate lists. The transport option `queue_order_strategy="priority"` makes a worker started with `-Q emails.critical,emails.bulk` BRPOP critical first; kombu's default `round_robin` would treat them fairly instead. In production I would also give `emails.critical` its own small worker, so an OTP never waits for a busy bulk worker.

## Retries, backoff and dead-letters

- **Transient** errors (timeouts, 5xx, 429, and unknown exceptions such as bugs) are retried with exponential backoff and "equal jitter". The delay is drawn from [d/2, d] with d = 10 s × 2^(attempt−1), capped at 300 s. Across 6 attempts that gives 5 retries, with ceilings of 10, 20, 40, 80 and 160 s. The fixed half guarantees a real pause; the random half stops a thousand jobs that failed during the same provider blip from retrying in lockstep.
- **Permanent** errors (invalid address, rejected content) are dead-lettered immediately. That matters more here than usual: **every retry spends a slot of the 200/min budget**, so retrying a hard bounce six times directly delays six good emails.
- **Attempts are counted in the DB row**, not with Celery's `request.retries`. Throttling deferrals use `self.retry()` too, and must not use up attempts. The count is incremented **when the job is claimed, before the provider call**. So a job that crashes its worker every time (say, an attachment that OOMs the process) still runs out of attempts instead of crash-looping forever.
- **Dead letters** go to the `DeadLetter` table (reason, last error, attempts), with an admin action "Replay" that resets attempts and re-publishes. I chose a table over a Redis DLQ list because it survives Redis, can be queried and joined with the job, and replaying needs human context.

## Not losing jobs

| What fails | What happens | Where |
|---|---|---|
| Request rolls back after enqueueing | No row, no message (`transaction.on_commit`) | `test_rolled_back_request_queues_nothing` |
| Redis down when the job is created | Row stays `queued` with `enqueued_at=NULL`; the next sweep (every 5 min) publishes it once it is over 60 s old | `test_broker_outage_at_enqueue_is_recovered_by_the_sweeper` |
| Pool child SIGKILL'd mid-send | Requeued at once (`task_reject_on_worker_lost`); the claim TTL stops a concurrent double-send; delivered on attempt 2 | `test_pool_child_killed_mid_send_is_requeued_and_delivered` |
| Whole worker / host dies | Message stays in Redis `unacked`; restored after `visibility_timeout`; delivered on attempt 2 | `test_whole_worker_killed_mid_send_is_redelivered_after_visibility_timeout` |
| Deferred (ETA) messages on a dying worker | Same path: they are unacked messages too. Prefetch doesn't bound them (Celery raises the prefetch count per ETA message), so one dead worker can strand hundreds during a sale | — |
| Hard `time_limit` kills an attempt | Celery *acks* the message (`task_acks_on_failure_or_timeout`), so no redelivery; the sweeper re-publishes `sending` jobs whose claim has expired | `test_sweeper_rescues_an_attempt_that_died_without_redelivery` |
| A job that kills its worker on every attempt | Attempts are counted at claim time; a redelivery that finds an expired claim with no attempts left dead-letters it without calling the provider | `test_job_that_kills_its_worker_every_time_is_dead_lettered` |
| Redis restarts | AOF with `appendfsync everysec` loses at most ~1 s of writes; anything lost is re-published by the sweeper after 30 min without progress | `docker-compose.yml`, `test_sweeper_republishes_lost_messages_and_leaves_finished_jobs` |
| Two messages for one job (redelivery, or a sweeper duplicate) | The conditional-UPDATE claim lets exactly one through; the other finds the job sent, or waits | `test_duplicate_message_for_a_finished_job_is_a_no_op`, `test_job_held_by_a_live_worker_waits_for_the_claim_to_expire` |
| An attempt overruns its claim and another worker takes over | Result writes are conditional on `claimed_at` (a fencing token), so the late write is discarded | `test_late_result_from_a_superseded_attempt_is_discarded` |

**Delivery semantics: at-least-once.** One duplicate window remains: the provider accepts the email and the worker dies before `UPDATE … status='sent'` commits, a few milliseconds. Closing it needs the provider's cooperation, so `job.id` is passed as an idempotency key for providers that honour one. Otherwise the trade is deliberate: a rare duplicate OTP or order confirmation is better than a lost one.

## Measured behaviour

- **500-job test** (limit scaled to 50/s with a 0.1 s guard so it runs in ~15 s): 509 provider calls (500 jobs plus the scripted failures' retries); busiest 1 s window = **50**; 498 sent exactly once, plus 2 dead-letters, as scripted.
- **Live run against real Redis, production config (200/min), one worker with 4 prefork processes, 450 emails:** the limiter window peaked at **195/200**: 180 bulk plus OTPs using the reserve. The backlog drained in one-minute waves: 190 sent, then 179, then 80. Final state: 449 sent, 1 dead-letter (the scripted hard bounce). In total there were 508 re-schedules, about 1.1 per job.

## Known limitations and what I'd do next

- **Thundering herd.** Denied jobs are each told "a slot frees at *T*", and they wake together (spread only by up to 2 s of jitter), so most bounce again. That cost about 1.1 re-schedules per job above. For 2,000 jobs it is roughly 10k cheap limiter calls over 11 minutes, about 15/s, trivial for Redis. At 100× the scale I would switch to *reservations* (GCRA): each denied job gets its own future slot and wakes exactly once.
- **ETA messages live in worker memory**, outside the prefetch limit, and Redis hides them behind `visibility_timeout`. That's fine for thousands. For millions I would store a `not_before` column and have a dispatcher publish only jobs that are due.
- **The `visibility_timeout` trade-off.** It must exceed the longest countdown we schedule: 300 s of backoff and about 63 s of throttling. Otherwise Redis redelivers deferred messages that a live worker still holds. Duplicates would be harmless thanks to the claim, but wasteful. A longer timeout means slower recovery after a host dies, which is why it is 1 h with the 30-min sweeper as the faster backstop.
- **A late OTP is useless.** Add `expires_at` and dead-letter expired OTPs instead of sending them.
- **The provider's 429 `Retry-After`** should pause the shared limiter for everyone. Today it is just another transient error for that one job.
- **SQLite** is used here so the project runs anywhere in minutes. On Postgres, the sweeper should take `select_for_update(skip_locked=True)` if beat ever runs on more than one node.
- **Metrics to alert on:** queue depth, limiter window occupancy, deferral rate, dead-letter rate, and the age of the oldest queued job.
