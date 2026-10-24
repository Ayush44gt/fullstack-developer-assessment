from django.conf import settings
from django.db import models
from django.utils import timezone


class Product(models.Model):
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=32, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return self.name


class OrderQuerySet(models.QuerySet):
    def with_billable_items(self):
        """Attach `order.billable_items`: non-refunded lines with their product.

        Costs one extra query for the whole queryset (`order_id IN (...)` with
        product JOINed), however many orders there are. Readers use the plain
        list attribute, so nothing can silently fall back to a query per order.
        """
        return self.prefetch_related(
            models.Prefetch(
                "items",
                queryset=OrderItem.objects.billable().select_related("product").order_by("pk"),
                to_attr="billable_items",
            )
        )


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PAID = "paid"
        SHIPPED = "shipped"
        DELIVERED = "delivered"
        CANCELLED = "cancelled"

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    reference = models.CharField(max_length=24, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PAID)
    created_at = models.DateTimeField(default=timezone.now)

    objects = OrderQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["customer", "-created_at"], name="order_customer_recent")]

    def __str__(self):
        return self.reference


class OrderItemQuerySet(models.QuerySet):
    def billable(self):
        return self.exclude(status=OrderItem.Status.REFUNDED)


class OrderItem(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        REFUNDED = "refunded"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    objects = OrderItemQuerySet.as_manager()

    @property
    def line_total(self):
        return self.unit_price * self.quantity
