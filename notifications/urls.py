from django.urls import path

from . import views

urlpatterns = [
    path("emails/", views.EmailJobCreateView.as_view(), name="email-create"),
    path("emails/<uuid:pk>/", views.EmailJobDetailView.as_view(), name="email-detail"),
]
