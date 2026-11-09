import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone


def scan_upload_path(instance, filename):
    """Keep evidence private and make the stored name hard to guess."""
    ext = Path(filename or "").suffix.lower()[:10]
    return f"private/scam_scans/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class Scan(models.Model):
    """One Scam Doctor check, kept so the safety team can review it.

    The user is shown a privacy notice before a scan. Text and uploaded
    evidence are stored for review; files are private to staff through the
    admin and the owner's own result page.
    """

    SOURCES = [
        ("checker", "Scam Doctor page"),
        ("circle_chat", "My Circle chat"),
        ("private_chat", "Private chat"),
    ]
    KINDS = [("text", "Text"), ("image", "Photo"), ("video", "Video")]
    RISKS = [
        ("safe", "No common signs"),
        ("low", "Low risk"),
        ("medium", "Medium risk"),
        ("high", "High risk"),
    ]
    STATUSES = [
        ("new", "New"),
        ("reviewing", "Being reviewed"),
        ("closed", "Closed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="scam_scans")
    source = models.CharField(max_length=20, choices=SOURCES, default="checker")
    kind = models.CharField(max_length=10, choices=KINDS, default="text")

    text = models.TextField(blank=True, default="")
    file = models.FileField(upload_to=scan_upload_path, blank=True, null=True)
    original_name = models.CharField(max_length=255, blank=True, default="")

    risk = models.CharField(max_length=10, choices=RISKS, default="safe")
    score = models.PositiveSmallIntegerField(default=0)
    flags = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True, default="")
    advice = models.JSONField(default=list, blank=True)
    engine = models.CharField(max_length=40, blank=True, default="rules")
    ai_used = models.BooleanField(default=False)
    ai_note = models.CharField(max_length=255, blank=True, default="")

    # Where the check came from, so staff can see the conversation context.
    circle = models.ForeignKey("race.Circle", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="scam_scans")
    chat_room = models.ForeignKey("race.ChatRoom", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="scam_scans")
    message_id = models.PositiveIntegerField(null=True, blank=True)
    other_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="scam_scans_about")
    warning_sent = models.BooleanField(default=False)
    dedupe_key = models.CharField(max_length=80, null=True, blank=True, unique=True)

    status = models.CharField(max_length=12, choices=STATUSES, default="new")
    staff_note = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="scam_scans_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        who = self.user.username if self.user_id else "unknown"
        return f"Scam scan #{self.pk} for {who} ({self.risk})"

    @property
    def risk_rank(self):
        return {"safe": 0, "low": 1, "medium": 2, "high": 3}.get(self.risk, 0)

    @property
    def is_flagged(self):
        return self.risk in ("medium", "high")
