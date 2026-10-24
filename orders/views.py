from decimal import Decimal

from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order
from .serializers import OrderSummarySerializer


class OrderSummaryView(APIView):
    """GET /api/orders/summary/ — dashboard data for the signed-in customer."""

    def get(self, request):
        orders = (
            Order.objects.filter(customer=request.user)
            .prefetch_related("items__product")
            .order_by("-created_at")
        )
        rows = OrderSummarySerializer(orders, many=True).data
        lifetime_spend = sum((Decimal(row["total"]) for row in rows), Decimal("0.00"))
        return Response(
            {"order_count": len(rows), "lifetime_spend": f"{lifetime_spend:.2f}", "orders": rows}
        )
