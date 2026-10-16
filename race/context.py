from django.conf import settings


def role(request):
    ctx = {"vapid_public_key": getattr(settings, "VAPID_PUBLIC_KEY", "")}
    if request.user.is_authenticated:
        from .services import get_profile
        ctx["role"] = get_profile(request.user).role
    else:
        ctx["role"] = "none"
    return ctx