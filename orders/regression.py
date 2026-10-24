"""The summary endpoint exactly as it ran after the FIN-231 deployment.

Kept only so the regression can be reproduced and profiled next to the fix
(`manage.py profile_summary`, orders/tests/test_query_counts.py). The view is
the unchanged pre-fix view; the deployment only touched the serializer.
In a real codebase this module would not exist; git history would be enough.
"""

from decimal import Decimal

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order, OrderItem
from .serializers import OrderItemSerializer


class RegressedOrderSummarySerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "reference", "status", "created_at", "item_count", "total", "items"]

    def _billable_items(self, obj):
        # .exclude() builds a new QuerySet, which never looks at the
        # prefetch cache populated by the view: one query per call, per order.
        return obj.items.exclude(status=OrderItem.Status.REFUNDED)

    def get_items(self, obj):
        # Fresh OrderItem instances, so item.product is lazy-loaded per line too.
        return OrderItemSerializer(self._billable_items(obj), many=True).data

    def get_item_count(self, obj):
        return self._billable_items(obj).count()

    def get_total(self, obj):
        total = sum((item.line_total for item in self._billable_items(obj)), Decimal("0.00"))
        return f"{total:.2f}"


class RegressedOrderSummaryView(APIView):
    def get(self, request):
        orders = (
            Order.objects.filter(customer=request.user)
            .prefetch_related("items__product")
            .order_by("-created_at")
        )
        rows = RegressedOrderSummarySerializer(orders, many=True).data
        lifetime_spend = sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))
        return Response(
            {"order_count": len(rows), "lifetime_spend": f"{lifetime_spend:.2f}", "orders": rows}
        )
