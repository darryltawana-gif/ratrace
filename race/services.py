from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone
from .models import Race, Participant, Profile, MAX_SELLERS


def broadcast(group, payload):
    async_to_sync(get_channel_layer().group_send)(group, {"type": "relay", "payload": payload})


def get_profile(user):
    return Profile.objects.get_or_create(user=user)[0]


def lobby_race(r):
    return {"id": r.id, "product": r.product, "budget": str(r.budget),
            "started_at": r.started_at.isoformat(), "expires_at": r.expires_at.isoformat(),
            "slots_left": MAX_SELLERS - r.participants.filter(active=True).count()}


def live_races():
    expire_races()
    return Race.objects.filter(status="open", expires_at__gt=timezone.now()).order_by("-started_at")


def expire_races():
    for r in list(Race.objects.filter(status="open", expires_at__lte=timezone.now())):
        if Race.objects.filter(pk=r.pk, status="open").update(status="expired"):
            broadcast("lobby", {"type": "race_ended", "race_id": r.id,
                                "message": f"Race for “{r.product}” expired."})
            broadcast(f"race_{r.id}", {"type": "race_over",
                                       "message": "⏰ Time is up. The race expired after 5 minutes. GAME OVER."})


def seconds_left(race_id):
    r = Race.objects.get(pk=race_id)
    return (r.expires_at - timezone.now()).total_seconds() if r.status == "open" else 0


def participant_dict(p):
    photo = p.seller.profile.photo
    return {"id": p.id, "slot": p.slot, "animal": p.animal[0], "emoji": p.animal[1],
            "seller_id": p.seller_id, "name": p.display_name, "product": p.product_name,
            "price": str(p.price), "details": p.details, "image": p.image.url,
            "photo": photo.url if photo else "", "discount": p.discount,
            "delivery": p.delivery, "promo": p.promo, "progress": p.progress}


def race_state(race_id):
    r = Race.objects.get(pk=race_id)
    parts = r.participants.filter(active=True).select_related("seller__profile").order_by("slot")
    return {"race": {"id": r.id, "product": r.product, "budget": str(r.budget), "status": r.status,
                     "started_at": r.started_at.isoformat(), "expires_at": r.expires_at.isoformat(),
                     "slots_left": MAX_SELLERS - parts.count()},
            "participants": [participant_dict(p) for p in parts]}


def next_slot(race):
    used = set(race.participants.filter(active=True).values_list("slot", flat=True))
    return next((s for s in range(1, MAX_SELLERS + 1) if s not in used), None)


@transaction.atomic
def adjust(user, race_id, field, direction):
    if direction not in (1, -1):
        return False
    race = Race.objects.get(pk=race_id)
    if not race.is_live:
        return False
    p = Participant.objects.select_for_update().filter(race=race, seller=user, active=True).first()
    if not p:
        return False
    if field == "discount":
        p.discount = max(0, min(50, p.discount + 5 * direction))
    elif field == "delivery":
        p.delivery = direction > 0
    elif field == "promo":
        p.promo = max(0, min(3, p.promo + direction))
    else:
        return False
    p.save()
    return True