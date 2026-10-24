# ANSWERS.md

Written answers, labelled by section. Section 2's architecture is in [DESIGN.md](DESIGN.md); this file has its written question (SIGKILL).

- [Section 1: Diagnose a broken system](#section-1-diagnose-a-broken-system)
- [Section 2: SIGKILL'd workers](#section-2-what-happens-to-in-flight-tasks-when-a-worker-is-sigkilld)
- [Section 3: Architecture review (Questions A and B)](#section-3-written-architecture-review)
- [Interpretations and assumptions](#interpretations-and-assumptions)

---

## Section 1: Diagnose a broken system

**Short version.** The deployment changed a *shared serializer*, not the view. The FIN-231 change (exclude refunded lines from totals) made the serializer call `obj.items.exclude(...)` three times per order. `.exclude()` returns a new QuerySet, and a new QuerySet never reads the cache that the view's `prefetch_related("items__product")` had filled. The result is an N+1: 3 queries per order, plus one product query per non-refunded line item. For a 250-order customer that is **1,611 queries instead of 2**. Fixed with a filtered `Prefetch(..., to_attr=...)`, so the query count is constant again.

The git history replays the incident: `git log --oneline` shows the baseline, the FIN-231 deploy (`git show 107fa7e`: only `serializers.py` and its functional test changed), and the fix.

### 1.1 Incident investigation log

The scenario is constructed, so steps 1–4 describe what I would check in production and what this bug would necessarily show there. Steps 5–8 were actually run in this repo, with real output.

| # | What I checked | Why this, now | What it showed / decision |
|---|---|---|---|
| 0 | **Pin down the symptom.** Latency before vs after the deploy, by customer. "30 s or more" is the gunicorn `--timeout 30` killing the worker, so the real duration is unknown and at least 30 s. | Two facts already narrow the search: it started at a **deploy**, and it hits only customers with **>200 orders**. | Latency scales with *rows per customer*, not with total traffic or table size. |
| 1 | **Mitigate first.** Roll back the release (or flag off FIN-231) before diagnosing. | The dashboard is down for the most valuable customers. Rolling back un-fixes FIN-231's refund totals for a while, but wrong totals on a display beat an outage. Investigate on staging with prod-shaped data. | Service restored; diagnosis continues without pressure. |
| 2 | **Is it global or per-row?** Compare light and heavy customers on the same release, and look at DB CPU, connection-pool waits and network. | Infrastructure causes (saturated DB, exhausted pool, network) hit *everyone*. A cost that grows with each customer's row count means per-row work: an N+1, or expensive Python per row. | Light customers are fine and latency grows linearly with order count. **Hypothesis: per-row queries.** |
| 3 | **What actually changed?** `git diff <prev>..<release> --stat`, the requirements diff, new migrations (`sqlmigrate` them: a dropped index?), settings. | "No change to the view" doesn't mean nothing on its path changed. Serializers, model managers and properties, middleware and dependencies all run inside that request. | Only `orders/serializers.py` changed (FIN-231). It is shared with the invoice job. No migrations, no dependency bumps. |
| 4 | **Database view.** `pg_stat_statements` ordered by `calls` and `total_exec_time` since the deploy; `EXPLAIN` on the hot statement. | Separates "a few slow queries" (index/plan problems) from "a huge number of fast ones" (N+1). | The `orders_orderitem WHERE order_id = $1 AND NOT status = $2` shape jumps ~100× in calls, at sub-ms mean time on the FK index. **Rules out a missing index.** |
| 5 | **Reproduce locally** with production-shaped data: `seed_orders` (a 5-order and a 250-order customer), then hit the endpoint through django-silk. | Turns telemetry into a deterministic, inspectable request. | Regressed: **38 queries** (light) vs **1,613** (heavy): 3 per order plus 1 per non-refunded line (858 of the 1,000 lines), about 6.4 per order. |
| 6 | **Pinpoint the code.** Silk stores a stack trace for each query; group the queries by shape. | Shows *which line* issues each repeated query. | 858× product lookup from `get_items`, 500× item SELECT (250 from `get_items`, 250 from `get_total`), 250× `COUNT(*)` from `get_item_count`. The view's two prefetch queries still run, but nothing reads their results. |
| 7 | **Confirm the mechanism, not just the symptom.** A test asserting the exact growth: `(N₂ − N₁) × (3 + items_per_order)` extra queries. | If my model of the ORM's behaviour is right, the formula is exact. | Exact match (`test_regressed_summary_is_n_plus_one`). **Root cause confirmed.** |
| 8 | **Fix and verify,** then add a guard against recurrence. | The FIN-231 functional tests passed against the broken code, which is why it shipped. | 1,611 → 2 queries; ~350 → ~24 ms locally; **~6 s → ~27 ms** with a 2 ms simulated DB round trip. A test now pins the endpoint at 4 queries (including 2 auth), whatever the order count. |

### 1.2 Root cause category: N+1 query (caused by silently bypassing the prefetch cache)

Why N+1 and not the other candidates:

- **Missing index:** that would slow *every* customer, with cost scaling with table size, and it shows up as a high *mean time* per statement. Here each statement is fast, there are just thousands of them, and the deploy contained no migration.
- **Serializer overhead:** the fixed endpoint serializes the identical 250-order payload in ~24 ms total. Python-side DRF work is not where 30 s goes.
- **Cache invalidation:** nothing caches this endpoint, and the cold path was 80 ms before the deploy.
- **ORM misconfiguration:** partly yes. The ORM silently threw away the view's prefetch. But the observable failure is an N+1, and the fix is an N+1 fix.

Why it reached 30 s in production when it's 0.4 s on a laptop: in-process SQLite has no network hop. Each of the 1,611 queries pays a round trip to the database, plus connection-pool waits while many heavy customers refresh at once. At 15–20 ms per query under that load, that is 24–32 s, i.e. the 30 s timeout. The `profile_summary` command adds a 2 ms simulated round trip per query to show the shape: ~350 ms becomes ~6 s.

### 1.3 The code that demonstrates the problem

[`orders/regression.py`](orders/regression.py), served at `/api/orders/summary/regressed/`, is the deployed code:

```python
# view (unchanged)
orders = Order.objects.filter(customer=request.user).prefetch_related("items__product").order_by("-created_at")

# serializer (the FIN-231 change)
def _billable_items(self, obj):
    return obj.items.exclude(status=OrderItem.Status.REFUNDED)   # new QuerySet: ignores the prefetch

def get_items(self, obj):      return OrderItemSerializer(self._billable_items(obj), many=True).data
def get_item_count(self, obj): return self._billable_items(obj).count()
def get_total(self, obj):      return sum(i.line_total for i in self._billable_items(obj))
```

### 1.4 The fix, and why it works at the ORM and database level

```python
# orders/models.py
def with_billable_items(self):
    return self.prefetch_related(Prefetch(
        "items",
        queryset=OrderItem.objects.billable().select_related("product").order_by("pk"),
        to_attr="billable_items",
    ))
# orders/views.py:       Order.objects.filter(customer=request.user).with_billable_items().order_by("-created_at")
# orders/serializers.py: items = OrderItemSerializer(source="billable_items", many=True); counts/totals from len()/sum()
```

**Why the regression happened, inside the ORM.** When the outer queryset is evaluated, `prefetch_related("items")` runs one extra `SELECT … FROM orders_orderitem WHERE order_id IN (…all order ids…)`. It groups the rows by `order_id` and stores them on each instance in `_prefetched_objects_cache["items"]`. The related manager's `get_queryset()` checks that cache, so `obj.items.all()` costs nothing. But `obj.items.exclude(...)` calls `_chain()`/`_clone()`, which copies the SQL but **not** the result cache. A different filter means a different result set, so Django must ask the database again, once per call and once per order. That queryset also builds *fresh* `OrderItem` instances, which know nothing about the prefetched products. So `item.product` goes through `ForwardManyToOneDescriptor.__get__`, misses the per-instance cache, and issues one `SELECT … FROM orders_product WHERE id = %s` per line. `.count()` on the new queryset adds a `COUNT(*)` per order.

**Why the fix works.**
- **The filter moves into the prefetch query itself.** Exactly one statement runs for all orders: `SELECT orderitem.*, product.* FROM orders_orderitem INNER JOIN orders_product ON … WHERE NOT status = 'refunded' AND order_id IN (…250 ids…) ORDER BY id`. The `IN` list is served by the FK index Django creates on `order_id`, and `select_related` turns 858 product lookups into one join on the product PK.
- **`to_attr` hands each order a plain Python list.** Django distributes the rows to their parent orders in Python, keyed on `order_id`. Item count and total are `len()`/`sum()` over that list, so no `COUNT`/`SUM` queries. Two queries in total, for any number of orders.
- **It fails loudly.** A list has no `.filter()`, so the next "small change" can't quietly re-query. If a caller forgets `with_billable_items()`, `get_item_count` raises `AttributeError` (`test_serializer_fails_loudly_without_prefetch`) instead of degrading to N+1 in production.

**Alternative considered:** `annotate(total=Sum(F("items__unit_price") * F("items__quantity"), filter=~Q(items__status="refunded")))`. It computes totals in SQL and would be the better choice if the payload didn't need the line items. Since it does, a single filtered prefetch serves both.

### 1.5 Profiler evidence (django-silk)

Full output: [docs/section1/profiling-evidence.md](docs/section1/profiling-evidence.md). Regenerate it with `python manage.py profile_summary`.

| Customer | Orders | Regressed: queries | Fixed: queries | Regressed: time | Fixed: time |
|---|---:|---:|---:|---:|---:|
| light_customer | 5 | 38 | 4 | 8 ms | 3 ms |
| heavy_customer | 250 | **1,613** | **4** | 346 ms (6.4 s with 2 ms RTT) | 24 ms (27 ms with 2 ms RTT) |

The query counts are silk's (they include 2 session/user queries). The times come from calling the view directly with no profiler, median of 5 runs. Silk's own request timings are higher because silk runs an `EXPLAIN` and stores a stack trace for every query it records. I chose silk over django-debug-toolbar because this is a JSON API consumed by a mobile app. The toolbar only injects into HTML responses, while silk records every request (including API calls and test-client calls) and keeps them in the DB for later comparison.

### 1.6 Follow-ups I would raise after the incident

1. **Query-count tests on list endpoints** (`django_assert_max_num_queries` / `CaptureQueriesContext`). FIN-231's functional tests passed, and only a query budget would have caught it.
2. **Paginate the endpoint.** It returns *every* order. A 5,000-order customer makes even the fixed version heavy, and `IN (…5,000 ids…)` is a large statement. Cursor pagination, see Section 3B.
3. **Shared serializers need an explicit queryset contract.** The invoice job and the dashboard share `OrderSummarySerializer`. The fix makes the contract explicit (`with_billable_items()` + fail-loud); alternatively, give each consumer its own serializer.
4. **Alert on queries per request** (APM span count), not only on latency. It moves before latency does.

---

## Section 2: What happens to in-flight tasks when a worker is SIGKILL'd?

**With Celery's defaults, the task is lost.** `task_acks_late=False` acknowledges the message when the worker *starts* executing it, so SIGKILL leaves nothing in the broker to redeliver. This project changes that with four settings (`config/settings.py`), and both cases below are exercised by `notifications/tests/test_worker_crash.py` against real worker subprocesses and real Redis.

```python
CELERY_TASK_ACKS_LATE = True             # ack after the task returns
CELERY_TASK_REJECT_ON_WORKER_LOST = True # child died mid-task -> reject(requeue=True), not ack
CELERY_WORKER_PREFETCH_MULTIPLIER = 1    # reserve at most `concurrency` ready messages (ETA ones come on top)
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
```

**Case 1: a pool child is killed** (the kernel OOM killer's usual target). The prefork MainProcess sees its child exit and raises `WorkerLostError`. Even with `acks_late=True`, Celery's default is still to *ack* such a message and mark the task failed, because it can't know whether the task is safe to run twice. `task_reject_on_worker_lost=True` makes it **reject with requeue** instead, and kombu pushes the message back onto the list. In the test log the message is back 35 ms after the kill:

```
11:11:20,723 ERROR/MainProcess Process 'SpawnPoolWorker-1' pid:38943 exited with 'signal 9 (SIGKILL)'
11:11:20,736 ERROR/MainProcess Task handler raised error: WorkerLostError('Worker exited prematurely: signal 9 (SIGKILL) Job: 0.')
11:11:20,771 INFO/MainProcess  Task notifications.send_email[ff62f9fb…] received
11:11:21,078 INFO/SpawnPoolWorker-2 Task notifications.send_email[ff62f9fb…] retry: Retry in 2.543974s
job 1ac4286e…: sent on attempt 2
```

**Case 2: the MainProcess, or the whole host or container, dies.** Nobody is left to reject. On Linux the children die too, because Celery sets `PR_SET_PDEATHSIG` on every pool child (`celery/concurrency/prefork.py`, `process_initializer`). The message is not lost: in the Redis transport, a delivered-but-unacked message lives in the **`unacked` hash** (and the `unacked_index` sorted set, scored by delivery time). `QoS.restore_visible()` in any live worker moves entries older than **`visibility_timeout`** back onto their queue. It runs when a worker starts and then on every 10th tick of a 10 s timer, so redelivery takes `visibility_timeout` plus up to about 100 s. The test kills the whole process group, checks that `HLEN unacked ≥ 1`, starts a fresh worker after the 5 s test timeout, and the job is delivered on attempt 2. Deferred (ETA) messages the dead worker was holding are unacked messages too, so they come back the same way. Prefetch does not bound those: Celery raises the prefetch count for every ETA message it accepts, so during a flash sale one dead worker can strand hundreds of deferred jobs until `visibility_timeout` (or the sweeper) returns them. A SIGTERM warm shutdown returns them immediately; SIGKILL cannot be caught.

**Related case: the hard time limit.** When `time_limit=45` kills a stuck attempt, Celery *acks* the message (`task_acks_on_failure_or_timeout` defaults to True), so nothing will redeliver it and the job is left `sending`. The beat sweeper re-publishes any `sending` job whose claim has expired (`test_sweeper_rescues_an_attempt_that_died_without_redelivery`). The soft limit (30 s) normally fires first and goes through the ordinary retry path.

**How the implementation makes redelivery safe.** Redelivery means at-least-once, so the task must tolerate seeing the same job twice:

1. **Finished jobs are no-ops.** The task first reads the `EmailJob` row; if it is `sent` or `dead`, it returns.
2. **No concurrent double-send.** The provider is called only after a conditional `UPDATE … SET status='sending', claimed_at=now, attempts=attempts+1 WHERE status IN ('queued','retrying') OR (status='sending' AND claimed_at < now − 90 s)`. Exactly one worker can match it. A redelivered task that finds a *fresh* `sending` claim can't tell whether the other attempt is dead or merely slow, so it re-schedules itself until the claim expires: the "Retry in 2.5s" above. The claim TTL (90 s) exceeds the task's hard `time_limit` (45 s). Once the TTL passes, the original attempt has either finished (the row says `sent`, so this is a no-op) or was certainly killed (so re-claim, attempt 2).
3. **Fencing.** Result writes are conditional on `claimed_at`, so an attempt that outlived its claim can't overwrite the newer attempt's result.
4. **Poison messages end.** The attempt is counted at claim time, before the provider call, because an attempt that kills its worker never reaches an except-branch. When a redelivery finds an expired claim with no attempts left, it dead-letters the job without calling the provider (`test_job_that_kills_its_worker_every_time_is_dead_lettered`).
5. **The remaining gap is honest and small.** If the provider accepted the email and the worker died before the `sent` UPDATE committed, a few milliseconds, the email goes out twice. Closing that needs the provider's help: `job.id` is passed as an idempotency key. Without it, a rare duplicate confirmation is the better failure than a lost one.

**The visibility-timeout trade-off.** It must exceed the longest countdown we schedule (300 s of backoff, about 63 s of throttling). Otherwise Redis would hand deferred messages to a second worker while the first still holds them. That would be harmless thanks to (2), but wasteful. A long timeout means slow recovery after a host loss, so the beat task `republish_stuck_jobs` also re-publishes any job with no progress for 30 minutes. That also covers messages Redis itself lost.

---

## Section 3: Written architecture review

I answered **Questions A and B**.

### Question A: Django Admin performance on a 500,000+ row table

A primary-key index helps lookups by id, but the changelist issues other queries. I'd open the slow page with django-debug-toolbar and look at these three first:

**1. `COUNT(*)` on every page load.** `ChangeList.get_results()` asks the paginator for the filtered count. With `show_full_result_count = True` (the default), it runs a second, unfiltered `SELECT COUNT(*)` for the "N total" link. PostgreSQL keeps no cached row count (MVCC means each transaction may see a different count), so each is a full scan of 500k+ rows. Fix: set `show_full_result_count = False` and give the `ModelAdmin` a custom `paginator` whose `count` returns the planner's estimate (`SELECT reltuples FROM pg_class WHERE relname = %s`) when no filter is applied. Trade-off: page counts become approximate.

**2. N+1 from `list_display`.** Every FK column (`customer`) or callable (`customer.email`, `item_count`) costs one query per row: 100 rows, 100+ queries. Fix: `list_select_related = ("customer",)` for forward FKs. For reverse relations and aggregates, override `get_queryset()` with `.annotate(item_count=Count("items"))` and expose it through `@admin.display(ordering="item_count")`. Trade-off: the joins and aggregates make the single remaining query heavier, so keep `list_display` lean.

**3. Sorting and searching that can't use an index.** Sorting by a clicked column, or by a model `Meta.ordering` on an unindexed column, forces PostgreSQL to sort all 500k rows to return 100. `search_fields = ("email", "name")` generates `UPPER(col::text) LIKE UPPER('%term%')` per field, ORed together, and no plain B-tree can serve that. Fix: add `Meta.indexes` matching the default ordering and limit `sortable_by` to indexed columns. Make searches exact (`"=email"`) or prefix (`"^name"`) lookups backed by expression indexes, `Index(Upper("email"))` and, for prefix matching, `Index(OpClass(Upper("name"), name="text_pattern_ops"))`. Use a `pg_trgm` GIN index only where substring search is required. Trade-off: each index slows writes and costs disk.

Also worth checking: `list_filter` on a large FK renders every related row in the sidebar, so use a `SimpleListFilter` or an autocomplete filter. And FK `<select>` widgets on the change form should become `raw_id_fields` or `autocomplete_fields`.

### Question B: Offset vs cursor pagination for a 10,000-record endpoint

**Offset** (DRF `LimitOffsetPagination` / `PageNumberPagination`) runs `ORDER BY created_at DESC LIMIT 20 OFFSET 9980`. The database can't jump to row 9,980: it walks the index (or sorts) and discards 9,980 rows, so cost grows linearly with depth, and page 500 costs roughly 500× page 1. DRF also issues a `COUNT(*)` on every page for `count` and the page links, which is another scan.

The bigger product problem is **mutation**. Infinite scroll fetches page 2 *after* the user has read page 1. If three orders were inserted at the top meanwhile, every row shifts down three, so page 2 opens with three items the user has already seen. A deletion shifts rows up, and one item is silently never shown. Clients can de-duplicate by id but can't recover skipped rows.

**Cursor** (DRF `CursorPagination`) encodes the position of the last row seen: `WHERE created_at < :last ORDER BY created_at DESC LIMIT 21`. With an index on `(customer_id, created_at)` this is an index seek: page 500 costs the same as page 1, and there is no `COUNT`. Rows inserted above the cursor shift nothing, so infinite scroll never duplicates or skips.

What it sacrifices: no "jump to page 37", no total count, and no arbitrary client-chosen sorting (each ordering needs its own index and a stable key). DRF filters on the *first* ordering field only and breaks ties with an offset stored inside the cursor, so order by something unique and immutable (`-id`, or a high-resolution `created_at`). Ordering by a mutable field such as `updated_at` lets an edited row jump past the cursor and be skipped.

**When to choose which.** Cursor for user-facing, append-heavy lists: mobile feeds, order history, sync or export jobs that walk a whole table. Offset where random access and totals matter and the data is small or rarely changes: admin tables, search results nobody reads past page 5. For this 10,000-record endpoint consumed by a mobile app, cursor. It also caps Section 1's class of bug at one page per request.

---

## Interpretations and assumptions

- **Section 1 is constructed**, as the brief asks ("You construct this yourself"). The production telemetry in the investigation log (gunicorn timeout, `pg_stat_statements`) describes what I would check and what this bug necessarily shows there. Everything from the local reproduction onward is real output from this repo.
- **"Celery with a Redis backend"** is read as Redis as the **broker**. No result backend is configured (`task_ignore_result=True`), because job state lives in the `EmailJob` row; storing results in Redis as well would be a second, weaker source of truth.
- **The 500-job test scales the limit** from 200/min to 50/s so it runs in about 15 s instead of 3 minutes. The limiter code and Lua script are the production ones, and the production 200/min numbers are tested separately on a pinned clock (`test_rate_limiter.py`) and were observed in a live run (DESIGN.md, "Measured behaviour").
- **No `time.sleep()` in the limiter or the task path.** A denied send is re-scheduled with `self.retry(countdown=…)`, and the worker is free in the meantime. `git grep time.sleep` finds only tooling and simulations: the simulated provider's `+slow` mode (a hung provider for the SIGKILL test), `profile_summary`'s simulated DB round trip, `queue_monitor`'s refresh interval, and test polling loops.
- **"Never exceeded" is measured at the provider**, counting *every* call, failed ones included, because a provider meters requests, not successful deliveries.
- **Database:** SQLite (WAL mode) so the project runs in minutes with no setup. The ORM behaviour in Section 1 is database-independent; the production notes assume PostgreSQL.
- **macOS:** billiard ≥ 4.3 starts prefork children with `spawn` on macOS, and Celery then needs `FORKED_BY_MULTIPROCESSING=1` to initialise them (otherwise every task fails with "not enough values to unpack"). `config/celery.py` sets it on macOS only; Linux is unaffected.
