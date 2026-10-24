from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from .models import EmailJob
from .services import enqueue_email


class EmailJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailJob
        fields = ["id", "kind", "to", "subject", "body", "status", "attempts", "created_at", "sent_at"]
        read_only_fields = ["id", "status", "attempts", "created_at", "sent_at"]


class EmailJobCreateView(generics.CreateAPIView):
    """POST /api/notifications/emails/ — accept an email for delivery.

    Returns 202 as soon as the row is committed; delivery happens in a worker.
    """

    serializer_class = EmailJobSerializer
    permission_classes = [IsAdminUser]  # called by other internal services

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = enqueue_email(**serializer.validated_data)
        return Response(self.get_serializer(job).data, status=status.HTTP_202_ACCEPTED)


class EmailJobDetailView(generics.RetrieveAPIView):
    queryset = EmailJob.objects.all()
    serializer_class = EmailJobSerializer
    permission_classes = [IsAdminUser]
