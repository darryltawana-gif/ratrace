from django.conf import settings


def role(request):
    ctx = {"vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", "")}
    if request.user.is_authenticated:
        from .services import get_profile, pending_requests
        p = get_profile(request.user)
        ctx["role"] = p.role
        ctx["circle_pending"] = pending_requests(request.user).count() if p.role == "seller" else 0
    else:
        ctx["role"] = "none"
        ctx["circle_pending"] = 0
    return ctx
