from django.urls import path

from . import views

urlpatterns = [
    path("summary/", views.OrderSummaryView.as_view(), name="order-summary"),
]
