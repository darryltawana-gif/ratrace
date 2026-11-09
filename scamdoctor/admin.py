from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Scan


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "risk", "score", "kind", "source",
                    "status", "warning_sent", "created")
    list_filter = ("risk", "kind", "source", "status", "warning_sent")
    search_fields = ("user__username", "other_user__username", "text",
                     "summary", "original_name")
    date_hierarchy = "created"
    readonly_fields = ("created", "score", "risk", "flags", "summary", "advice",
                       "engine", "ai_used", "ai_note", "warning_sent", "dedupe_key",
                       "evidence")
    fieldsets = (
        ("Who and where", {
            "fields": ("user", "source", "kind", "other_user", "circle",
                       "chat_room", "message_id", "created")}),
        ("What was checked", {
            "fields": ("text", "file", "evidence", "original_name")}),
        ("Result", {
            "fields": ("risk", "score", "summary", "advice", "flags",
                       "engine", "ai_used", "ai_note", "warning_sent")}),
        ("Safety team review", {
            "fields": ("status", "staff_note", "reviewed_by", "reviewed_at",
                       "dedupe_key")}),
    )
    actions = ("mark_reviewing", "mark_closed")

    @admin.display(description="Evidence")
    def evidence(self, obj):
        if not obj.file:
            return "—"
        url = reverse("scamdoctor:scan_file", args=[obj.id])
        return format_html('<a href="{}" target="_blank" rel="noopener">Open evidence</a>', url)

    def save_model(self, request, obj, form, change):
        if obj.status != "new" and not obj.reviewed_at:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark selected scans as being reviewed")
    def mark_reviewing(self, request, queryset):
        queryset.update(status="reviewing", reviewed_by=request.user,
                        reviewed_at=timezone.now())

    @admin.action(description="Mark selected scans as closed")
    def mark_closed(self, request, queryset):
        queryset.update(status="closed", reviewed_by=request.user,
                        reviewed_at=timezone.now())
