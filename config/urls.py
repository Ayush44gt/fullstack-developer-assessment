from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/orders/", include("orders.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api-auth/", include("rest_framework.urls")),  # log-in link for DRF's browsable API
]

if settings.ENABLE_SILK:
    urlpatterns.append(path("silk/", include("silk.urls", namespace="silk")))
