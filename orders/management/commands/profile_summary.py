"""Profile /api/orders/summary/ before and after the fix.

1. Sends each request through the full middleware stack so django-silk records
   it (query count, time, every SQL statement with its Python stack trace).
2. Times the views again without the profiler, optionally adding a simulated
   DB round trip per query to show how a remote database turns N+1 into seconds.
3. Writes the results to docs/section1/profiling-evidence.md.
"""

import re
import statistics
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Sum
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from orders.regression import RegressedOrderSummaryView
from orders.views import OrderSummaryView

ENDPOINTS = [
    ("regressed", "/api/orders/summary/regressed/", RegressedOrderSummaryView),
    ("fixed", "/api/orders/summary/", OrderSummaryView),
]
CUSTOMERS = ["light_customer", "heavy_customer"]
OUTPUT = Path(settings.BASE_DIR) / "docs" / "section1" / "profiling-evidence.md"
FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>\w+)')


class Command(BaseCommand):
    help = "Profile the summary endpoint before/after the fix and write docs/section1/profiling-evidence.md"

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=5, help="timed runs per endpoint")
        parser.add_argument("--rtt-ms", type=float, default=2.0, help="simulated DB round trip")

    def handle(self, *args, runs, rtt_ms, **opts):
        if not settings.ENABLE_SILK:
            raise CommandError("django-silk is disabled; run with ENABLE_SILK=1")
        if not get_user_model().objects.filter(username="heavy_customer").exists():
            call_command("seed_orders", stdout=self.stdout)

        silk_rows, hot_spots = self._profile_with_silk()
        timing_rows = self._time_without_profiler(runs, rtt_ms)
        OUTPUT.write_text(self._render(silk_rows, timing_rows, hot_spots, runs, rtt_ms))
        self.stdout.write(self.style.SUCCESS(f"Wrote {OUTPUT.relative_to(settings.BASE_DIR)}"))

    def _profile_with_silk(self):
        from silk.models import Request as SilkRequest

        client = Client()
        rows, hot_spots = [], None
        for username in CUSTOMERS:
            user = get_user_model().objects.get(username=username)
            client.force_login(user)
            for label, path, _ in ENDPOINTS:
                started = timezone.now()
                response = client.get(path)
                if response.status_code != 200:
                    raise CommandError(f"{path} returned {response.status_code}")
                recorded = SilkRequest.objects.filter(path=path, start_time__gte=started).latest("start_time")
                sql_ms = recorded.queries.aggregate(total=Sum("time_taken"))["total"] or 0
                rows.append(
                    {
                        "customer": username,
                        "orders": user.orders.count(),
                        "endpoint": label,
                        "queries": recorded.num_sql_queries,
                        "time_ms": recorded.time_taken,
                        "sql_ms": sql_ms,
                        "silk_id": recorded.id,
                    }
                )
                if username == "heavy_customer" and label == "regressed":
                    hot_spots = self._query_shapes(recorded)
                self.stdout.write(f"silk  {username:15} {label:9} {recorded.num_sql_queries:5} queries")
        return rows, hot_spots

    def _query_shapes(self, recorded):
        """Every query shape in the request, with how often each line of our code issued it."""
        origins = defaultdict(Counter)
        for query in recorded.queries.all():
            shape = re.sub(r"'[^']*'", "?", query.query)
            shape = re.sub(r"\b\d+(\.\d+)?\b", "?", shape)
            shape = re.sub(r"IN \((\?, )*\?\)", "IN (...)", shape)
            origins[shape][self._origin(query.traceback)] += 1
        rows = [(sum(c.values()), shape, c) for shape, c in origins.items()]
        return sorted(rows, key=lambda row: -row[0])

    def _origin(self, traceback):
        """First stack frame (silk stores the stack newest-first) inside orders/."""
        app_dir = str(Path(settings.BASE_DIR) / "orders")
        for frame in FRAME.finditer(traceback):
            if frame["path"].startswith(app_dir) and "management" not in frame["path"]:
                path = Path(frame["path"]).relative_to(settings.BASE_DIR)
                return f"{path}:{frame['line']} `{frame['func']}`"
        return "Django/DRF internals"

    def _time_without_profiler(self, runs, rtt_ms):
        from silk.collector import DataCollector

        # Silk keeps the last request in a thread-local; left set, it would keep
        # recording (and EXPLAIN-ing) every query below.
        DataCollector().clear()
        factory = APIRequestFactory()
        rows = []
        for username in CUSTOMERS:
            user = get_user_model().objects.get(username=username)
            for label, path, view_class in ENDPOINTS:
                view = view_class.as_view()
                local = [self._timed_call(view, factory, path, user) for _ in range(runs)]
                with self._simulated_rtt(rtt_ms):
                    remote = [self._timed_call(view, factory, path, user) for _ in range(runs)]
                rows.append(
                    {
                        "customer": username,
                        "orders": user.orders.count(),
                        "endpoint": label,
                        "queries": local[0][1],
                        "local_ms": statistics.median(ms for ms, _ in local),
                        "remote_ms": statistics.median(ms for ms, _ in remote),
                    }
                )
        return rows

    def _timed_call(self, view, factory, path, user):
        request = factory.get(path)
        force_authenticate(request, user=user)
        queries = 0

        def count(execute, sql, params, many, context):
            nonlocal queries
            queries += 1
            return execute(sql, params, many, context)

        with connection.execute_wrapper(count):
            started = time.perf_counter()
            response = view(request)
            elapsed = (time.perf_counter() - started) * 1000
        assert response.status_code == 200
        return elapsed, queries

    @contextmanager
    def _simulated_rtt(self, rtt_ms):
        def delay(execute, sql, params, many, context):
            time.sleep(rtt_ms / 1000)  # stands in for the network hop to a managed database
            return execute(sql, params, many, context)

        with connection.execute_wrapper(delay):
            yield

    def _render(self, silk_rows, timing_rows, hot_spots, runs, rtt_ms):
        out = [
            "# Section 1 — profiling evidence",
            "",
            f"Generated by `python manage.py profile_summary` on {timezone.now():%Y-%m-%d %H:%M} UTC "
            "(SQLite, local machine). Re-run it to reproduce; timings will vary, query counts will not.",
            "",
            "`regressed` = the code as deployed with FIN-231 (`orders/regression.py`, served at "
            "`/api/orders/summary/regressed/`). `fixed` = `/api/orders/summary/`.",
            "",
            "## 1. django-silk, request through the full middleware stack",
            "",
            "| Customer | Orders | Endpoint | SQL queries | Request time (ms) | Time in SQL (ms) | silk request id |",
            "|---|---:|---|---:|---:|---:|---|",
        ]
        for r in silk_rows:
            out.append(
                f"| {r['customer']} | {r['orders']} | {r['endpoint']} | **{r['queries']}** | "
                f"{r['time_ms']:.0f} | {r['sql_ms']:.0f} | `{r['silk_id']}` |"
            )
        out += [
            "",
            "Query counts include the 2 auth queries (session + user). Silk's own timings are inflated: for "
            "every query it captures a stack trace, runs an `EXPLAIN` and writes a row, so the regressed "
            "request pays that overhead ~1,600 times. Section 2 has numbers without the profiler.",
            "",
            "To browse these requests: `python manage.py createsuperuser`, `python manage.py runserver`, "
            "log in at `/admin/`, then open `/silk/requests/` (or `/silk/request/<id>/sql/`).",
            "",
            f"## 2. Without the profiler (view called directly, median of {runs} runs)",
            "",
            "Called through DRF's `APIRequestFactory` with `force_authenticate`, so there are no session/user queries.",
            "",
            f"| Customer | Orders | Endpoint | SQL queries | Local SQLite (ms) | With {rtt_ms:g} ms DB round trip (ms) |",
            "|---|---:|---|---:|---:|---:|",
        ]
        for r in timing_rows:
            out.append(
                f"| {r['customer']} | {r['orders']} | {r['endpoint']} | **{r['queries']}** | "
                f"{r['local_ms']:.1f} | {r['remote_ms']:.1f} |"
            )
        out += [
            "",
            "In-process SQLite has no network hop, which hides most of the cost. The last column adds a "
            f"{rtt_ms:g} ms sleep per query, roughly one round trip to a managed Postgres in the same region. "
            "Production adds more: connection-pool waits and a busy database while every heavy customer "
            "refreshes the dashboard. At 15–20 ms per query under that load, ~1,600 queries is 24–32 s: "
            "the 30 s worker timeout in the incident.",
            "",
            "## 3. What silk shows inside the regressed request (heavy_customer)",
            "",
            "Query shapes with literals replaced by `?`, grouped and counted. The file:line comes from the "
            "stack trace silk stores with each query.",
            "",
            "| Times run | Query shape | Issued from |",
            "|---:|---|---|",
        ]
        for n, shape, origins in hot_spots:
            shape = shape if len(shape) <= 160 else shape[:157] + "..."
            where = "<br>".join(f"{origin} ×{count}" for origin, count in origins.most_common())
            out.append(f"| {n} | `{shape}` | {where} |")
        out.append(f"| **{sum(n for n, _, _ in hot_spots)}** | total | |")
        out += [
            "",
            "Per order: one item SELECT from `get_items` and one from `get_total` (the same shape, so "
            "one row above), plus one `COUNT(*)` from `get_item_count`. Then one product SELECT per "
            "non-refunded line. The two `IN (...)` rows are the view's "
            "`prefetch_related(\"items__product\")`: they still run, but nothing reads their results.",
            "",
        ]
        return "\n".join(out)
