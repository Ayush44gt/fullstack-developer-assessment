import secrets
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order, OrderItem, Product

CUSTOMERS = {"light_customer": "light_orders", "heavy_customer": "heavy_orders"}


class Command(BaseCommand):
    help = "Create a light customer (a few orders) and a heavy customer (250 orders) for profiling."

    def add_arguments(self, parser):
        parser.add_argument("--light-orders", type=int, default=5)
        parser.add_argument("--heavy-orders", type=int, default=250)
        parser.add_argument("--items-per-order", type=int, default=4)

    @transaction.atomic
    def handle(self, *args, **opts):
        products = self._products()
        now = timezone.now()
        for username, option in CUSTOMERS.items():
            user, _ = get_user_model().objects.get_or_create(username=username)
            password = secrets.token_urlsafe(9)  # printed below; never stored in the repo
            user.set_password(password)
            user.save()
            user.orders.all().delete()

            orders = Order.objects.bulk_create(
                Order(
                    customer=user,
                    reference=f"ORD-{user.pk:03d}-{n:05d}",
                    created_at=now - timedelta(hours=n),
                )
                for n in range(opts[option])
            )
            items = []
            for order in orders:
                for i in range(opts["items_per_order"]):
                    product = products[(order.pk + i) % len(products)]
                    items.append(
                        OrderItem(
                            order=order,
                            product=product,
                            quantity=1 + i % 3,
                            unit_price=product.price,
                            # every 7th line was refunded, so FIN-231 changes the totals
                            status=OrderItem.Status.REFUNDED if (order.pk + i) % 7 == 0 else OrderItem.Status.ACTIVE,
                        )
                    )
            OrderItem.objects.bulk_create(items)
            self.stdout.write(f"{username}: {len(orders)} orders, {len(items)} items, password={password}")

    def _products(self):
        if not Product.objects.exists():
            Product.objects.bulk_create(
                Product(name=f"Product {n}", sku=f"SKU-{n:04d}", price=Decimal(5 + n % 40) + Decimal("0.99"))
                for n in range(50)
            )
        return list(Product.objects.order_by("pk"))
