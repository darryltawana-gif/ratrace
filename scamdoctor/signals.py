"""Automatic, private Scam Doctor warnings for chat messages.

Only the rules engine runs here. It is fast and free, and it never blocks the
chat on a cloud API. The person who receives a flagged message gets a private
warning; the sender is never told and no public accusation is made.

Each warning is stored as a Scan so the safety team can review it.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse

from race.models import ChatMessage, CircleMessage

from . import engine
from .models import Scan

log = logging.getLogger(__name__)


def _threshold():
    return int(getattr(settings, "SCAM_DOCTOR_AUTO_WARN_SCORE", 35))


def _notify(user_id, scan, sender_name):
    url = reverse("scamdoctor:scan_detail", args=[scan.id])
    message = (f"Scam Doctor: {scan.get_risk_display().lower()} in a message from "
               f"{sender_name}. Review it before you pay.")
    layer = get_channel_layer()
    if layer:
        try:
            async_to_sync(layer.group_send)(
                f"user_{user_id}",
                {"type": "relay", "payload": {
                    "type": "scam_warning", "scan_id": scan.id, "risk": scan.risk,
                    "message": message, "url": url}})
        except Exception:
            log.exception("Scam Doctor warning could not be sent over the lobby")

    try:
        from race import services as race_services
        race_services.push_to_users([user_id], {
            "title": "Scam Doctor warning",
            "body": message,
            "url": url,
            "tag": f"scam-{scan.id}",
        }, 3600)
    except Exception:
        log.exception("Scam Doctor push warning failed")


def _store(user_id, sender, result, **fields):
    dedupe_key = fields.pop("dedupe_key", None)
    if dedupe_key:
        scan, made = Scan.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "user_id": user_id,
                "risk": result["risk"],
                "score": result["score"],
                "flags": result["flags"],
                "summary": result["summary"],
                "advice": result["advice"],
                "engine": result["engine"],
                "ai_used": result["ai_used"],
                "ai_note": result["ai_note"],
                "other_user": sender,
                "warning_sent": True,
                **fields,
            })
    else:
        scan = Scan.objects.create(
            user_id=user_id,
            risk=result["risk"],
            score=result["score"],
            flags=result["flags"],
            summary=result["summary"],
            advice=result["advice"],
            engine=result["engine"],
            ai_used=result["ai_used"],
            ai_note=result["ai_note"],
            other_user=sender,
            warning_sent=True,
            **fields,
        )
        made = True
    return scan, made


@receiver(post_save, sender=ChatMessage)
def scan_private_message(sender, instance, created, **kwargs):
    if not created or instance.is_system or not instance.sender_id:
        return
    text = (instance.text or "").strip()
    if not text:
        return
    try:
        room = instance.room
        recipient_id = (room.seller_id if instance.sender_id == room.customer_id
                        else room.customer_id)
    except Exception:
        return
    if not recipient_id or recipient_id == instance.sender_id:
        return

    result = engine.analyse_text(text, use_ai=False)
    if result["score"] < _threshold():
        return

    scan, made = _store(
        recipient_id, instance.sender, result,
        source="private_chat", kind="text", text=text,
        chat_room=room, message_id=instance.id,
        dedupe_key=f"chat:{instance.id}")
    if made:
        _notify(recipient_id, scan, instance.sender.username)


@receiver(post_save, sender=CircleMessage)
def scan_circle_message(sender, instance, created, **kwargs):
    if not created or not instance.sender_id:
        return
    text = (instance.text or "").strip()
    if not text:
        return
    try:
        circle = instance.circle
    except Exception:
        return

    result = engine.analyse_text(text, use_ai=False)
    if result["score"] < _threshold():
        return

    recipients = set()
    if circle.customer_id != instance.sender_id:
        recipients.add(circle.customer_id)
    recipients.update(
        circle.members.filter(status="accepted")
        .exclude(seller_id=instance.sender_id)
        .values_list("seller_id", flat=True))
    if not recipients:
        return

    for user_id in recipients:
        scan, made = _store(
            user_id, instance.sender, result,
            source="circle_chat", kind="text", text=text,
            circle=circle, message_id=instance.id,
            dedupe_key=f"circle:{instance.id}:{user_id}")
        if made:
            _notify(user_id, scan, instance.sender.username)
