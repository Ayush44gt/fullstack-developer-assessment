from django.urls import path

from . import regression, views

urlpatterns = [
    path("summary/", views.OrderSummaryView.as_view(), name="order-summary"),
    # The deployed (regressed) version, kept for side-by-side profiling.
    path(
        "summary/regressed/",
        regression.RegressedOrderSummaryView.as_view(),
        name="order-summary-regressed",
    ),
]
