import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from race import services as race_services
from race.models import ChatMessage, CircleMessage

from . import ai, engine
from .forms import ScanForm
from .models import Scan

log = logging.getLogger(__name__)


def _limit():
    return int(getattr(settings, "SCAM_DOCTOR_MAX_CHECKS_PER_HOUR", 40))


def _over_limit(user):
    since = timezone.now() - timedelta(hours=1)
    # Automatic warnings do not count against the user's own check limit.
    return (Scan.objects.filter(user=user, created__gte=since)
            .exclude(warning_sent=True).count() >= _limit())


def _report_url(other_user):
    if not other_user:
        return ""
    profile = getattr(other_user, "profile", None)
    if profile and profile.role == "seller":
        return reverse("report_seller", args=[other_user.id])
    return ""


def _save_scan(user, result, **fields):
    return Scan.objects.create(
        user=user,
        risk=result["risk"],
        score=result["score"],
        flags=result["flags"],
        summary=result["summary"],
        advice=result["advice"],
        engine=result["engine"],
        ai_used=result["ai_used"],
        ai_note=result["ai_note"],
        **fields,
    )


@login_required
def checker(request):
    form = ScanForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        if _over_limit(request.user):
            messages.error(request, "You have run many checks in the last hour. "
                                    "Please wait a little and try again.")
            return redirect("scamdoctor:checker")
        text = form.cleaned_data.get("text") or ""
        kind = form.kind
        upload = form.upload
        result = engine.analyse(kind, file=upload, text=text, use_ai=True)
        scan = _save_scan(
            request.user, result, source="checker", kind=kind, text=text,
            file=upload, original_name=(getattr(upload, "name", "") or "")[:255])
        messages.success(request, "Scan complete. Read the result below.")
        return redirect("scamdoctor:scan_detail", scan.id)

    recent = Scan.objects.filter(user=request.user)[:5]
    return render(request, "scam_doctor.html", {
        "form": form,
        "recent": recent,
        "ai_available": ai.available(),
        "limit": _limit(),
    })


@login_required
def scan_file(request, scan_id):
    """Serve evidence only to its owner or the safety team."""
    scan = get_object_or_404(Scan, pk=scan_id)
    if scan.user_id != request.user.id and not request.user.is_staff:
        raise Http404("That file is not yours.")
    if not scan.file:
        raise Http404("No file was stored with this scan.")
    try:
        return FileResponse(scan.file.open("rb"), as_attachment=False,
                            filename=scan.original_name or None)
    except FileNotFoundError:
        raise Http404("The stored file is missing.")


@login_required
def scan_detail(request, scan_id):
    scan = get_object_or_404(Scan, pk=scan_id)
    if scan.user_id != request.user.id and not request.user.is_staff:
        raise Http404("That scan is not yours.")
    return render(request, "scam_scan.html", {
        "scan": scan,
        "flags": scan.flags or [],
        "advice": scan.advice or [],
        "report_url": _report_url(scan.other_user),
        "ai_available": ai.available(),
    })


def _message_text(request, source, message_id):
    """Return (text, fields, error) after checking that the user may see it."""
    if source == "circle_chat":
        message = (CircleMessage.objects.select_related("circle", "sender")
                   .filter(pk=message_id).first())
        if not message or not race_services.is_circle_member(message.circle, request.user):
            return None, None, "That message is not in your Circle."
        return message.text, {
            "circle": message.circle,
            "message_id": message.id,
            "other_user": message.sender if message.sender_id != request.user.id else None,
        }, None

    if source == "private_chat":
        message = (ChatMessage.objects.select_related("room", "sender")
                   .filter(pk=message_id).first())
        if not message or not race_services.can_chat(request.user, message.room_id):
            return None, None, "That message is not in your chat."
        return message.text, {
            "chat_room": message.room,
            "message_id": message.id,
            "other_user": message.sender if message.sender_id != request.user.id else None,
        }, None

    return None, None, "Unknown chat source."


@require_POST
@login_required
def check_message(request):
    """Called by the 'Check for scam' button inside a chat."""
    if _over_limit(request.user):
        return JsonResponse({"ok": False, "error": "Too many checks. Please wait a little."},
                            status=429)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Bad request."}, status=400)

    source = str(body.get("source") or "checker")
    fields = {}
    text = str(body.get("text") or "").strip()[:8000]
    message_id = body.get("message_id")
    if source in ("circle_chat", "private_chat") and message_id:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Bad message."}, status=400)
        text, fields, error = _message_text(request, source, message_id)
        if error:
            return JsonResponse({"ok": False, "error": error}, status=403)
    else:
        source = "checker"

    if not text:
        return JsonResponse({"ok": False, "error": "Paste a message to check."}, status=400)

    context = ("This message came from a chat on The Rat Race. "
               "The user is asking Scam Doctor to check it.")
    result = engine.analyse_text(text, use_ai=True, context=context)
    scan = _save_scan(request.user, result, source=source, kind="text",
                      text=text, **fields)
    return JsonResponse({
        "ok": True,
        "scan_id": scan.id,
        "scan_url": reverse("scamdoctor:scan_detail", args=[scan.id]),
        "risk": scan.risk,
        "risk_label": scan.get_risk_display(),
        "score": scan.score,
        "summary": scan.summary,
        "flags": scan.flags,
        "advice": scan.advice,
        "engine": scan.engine,
        "ai_note": scan.ai_note,
        "report_url": _report_url(scan.other_user),
    })
