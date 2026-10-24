"""Query-count guards. This is the test that would have stopped FIN-231 in CI:
the functional tests in test_summary.py passed against the regressed code."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from orders.models import Order
from orders.serializers import OrderSummarySerializer

URL = "/api/orders/summary/"
REGRESSED_URL = "/api/orders/summary/regressed/"


def count_queries(client, user, url):
    client.force_login(user)
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200
    return len(ctx.captured_queries)


def test_fixed_summary_runs_constant_queries(client, make_customer):
    # session + user (auth) + orders + billable items JOIN product
    assert count_queries(client, make_customer(orders=3), URL) == 4
    assert count_queries(client, make_customer(orders=40), URL) == 4


def test_regressed_summary_is_n_plus_one(client, make_customer):
    """Pins down the root cause: per order, 2 item SELECTs + 1 COUNT + 1 product SELECT per line."""
    items = 3
    small = count_queries(client, make_customer(orders=3, items=items), REGRESSED_URL)
    large = count_queries(client, make_customer(orders=40, items=items), REGRESSED_URL)
    assert large - small == (40 - 3) * (3 + items)


def test_serializer_fails_loudly_without_prefetch(make_customer):
    make_customer(orders=1)
    with pytest.raises(AttributeError):
        OrderSummarySerializer(Order.objects.all(), many=True).data
