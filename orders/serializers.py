from decimal import Decimal

from rest_framework import serializers

from .models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "product_name", "quantity", "unit_price", "line_total"]


class OrderSummarySerializer(serializers.ModelSerializer):
    """One row of the mobile dashboard. Also reused by the invoice PDF job."""

    items = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "reference", "status", "created_at", "item_count", "total", "items"]

    def _billable_items(self, obj):
        # FIN-231: refunded lines must not be shown or counted towards totals.
        return obj.items.exclude(status=OrderItem.Status.REFUNDED)

    def get_items(self, obj):
        return OrderItemSerializer(self._billable_items(obj), many=True).data

    def get_item_count(self, obj):
        return self._billable_items(obj).count()

    def get_total(self, obj):
        total = sum((item.line_total for item in self._billable_items(obj)), Decimal("0.00"))
        return f"{total:.2f}"
