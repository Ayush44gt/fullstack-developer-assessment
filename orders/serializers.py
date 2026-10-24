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
    """One row of the mobile dashboard. Also reused by the invoice PDF job.

    Expects orders from `Order.objects.with_billable_items()`. Every field reads
    the prefetched `billable_items` list, so serialising N orders runs zero
    queries. Without the prefetch, get_item_count raises AttributeError rather
    than quietly issuing a query per order (which is what FIN-231 shipped).
    """

    items = OrderItemSerializer(source="billable_items", many=True, read_only=True)
    item_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "reference", "status", "created_at", "item_count", "total", "items"]

    def get_item_count(self, obj):
        return len(obj.billable_items)

    def get_total(self, obj):
        total = sum((item.line_total for item in obj.billable_items), Decimal("0.00"))
        return f"{total:.2f}"
