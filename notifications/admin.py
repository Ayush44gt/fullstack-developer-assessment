from django.contrib import admin

from .models import DeadLetter, EmailJob
from .services import replay_dead_letter


@admin.register(EmailJob)
class EmailJobAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "to", "status", "attempts", "created_at", "sent_at"]
    list_filter = ["status", "kind"]
    search_fields = ["=id", "to"]
    show_full_result_count = False
    readonly_fields = [f.name for f in EmailJob._meta.fields]


@admin.register(DeadLetter)
class DeadLetterAdmin(admin.ModelAdmin):
    list_display = ["job", "reason", "attempts", "created_at", "replayed_at"]
    list_filter = ["reason"]
    list_select_related = ["job"]
    actions = ["replay"]

    @admin.action(description="Replay selected jobs (fresh attempts)")
    def replay(self, request, queryset):
        replayed = sum(replay_dead_letter(dead_letter) for dead_letter in queryset.select_related("job"))
        self.message_user(request, f"Re-queued {replayed} job(s).")
