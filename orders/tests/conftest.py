from decimal import Decimal
from itertools import count

import pytest
from django.contrib.auth import get_user_model

from orders.models import Order, OrderItem, Product

_seq = count()


@pytest.fixture
def make_customer(db):
    """Create a customer with `orders` orders of `items` lines each.

    Every line costs 10.00 x quantity 1; the first line of each order is refunded
    when `refund_first_line` is set.
    """
    product = Product.objects.create(name="Widget", sku=f"W-{next(_seq)}", price=Decimal("10.00"))

    def factory(orders, items=3, refund_first_line=False):
        user = get_user_model().objects.create_user(username=f"customer{next(_seq)}")
        for n in range(orders):
            order = Order.objects.create(customer=user, reference=f"R-{next(_seq)}")
            OrderItem.objects.bulk_create(
                OrderItem(
                    order=order,
                    product=product,
                    unit_price=Decimal("10.00"),
                    status=OrderItem.Status.REFUNDED if refund_first_line and i == 0 else OrderItem.Status.ACTIVE,
                )
                for i in range(items)
            )
        return user

    return factory
