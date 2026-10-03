def role(request):
    if request.user.is_authenticated:
        from .services import get_profile
        return {"role": get_profile(request.user).role}
    return {"role": "none"}