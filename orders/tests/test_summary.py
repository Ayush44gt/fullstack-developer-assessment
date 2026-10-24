import pytest

URL = "/api/orders/summary/"


@pytest.mark.django_db
def test_requires_authentication(client):
    assert client.get(URL).status_code == 403


def test_summary_lists_orders_with_totals(client, make_customer):
    user = make_customer(orders=2, items=3)
    client.force_login(user)

    body = client.get(URL).json()

    assert body["order_count"] == 2
    assert body["lifetime_spend"] == "60.00"
    assert [o["total"] for o in body["orders"]] == ["30.00", "30.00"]
    assert all(o["item_count"] == 3 for o in body["orders"])
