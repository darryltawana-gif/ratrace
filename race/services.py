from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import (Race, Participant, Profile, ChatRoom, ChatMessage,
                     MAX_SELLERS, RACE_MINUTES)

MAX_DISCOUNT = 50


def broadcast(group, payload):
    async_to_sync(get_channel_layer().group_send)(group, {"type": "relay", "payload": payload})


def get_profile(user):
    return Profile.objects.get_or_create(user=user)[0]


def display_name(user, race):
    """The name a seller uses in a race (falls back to the username)."""
    p = Participant.objects.filter(race=race, seller=user).order_by("-id").first()
    return p.display_name if p else user.username


def lobby_race(r):
    return {"id": r.id, "product": r.product, "budget": str(r.budget),
            "started_at": r.started_at.isoformat(), "expires_at": r.expires_at.isoformat(),
            "slots_left": MAX_SELLERS - r.participants.filter(active=True).count()}


def live_races():
    expire_races()
    return Race.objects.filter(status="open", expires_at__gt=timezone.now()).order_by("-started_at")


def end_message(race):
    if race.status == "won" and race.winner:
        return f"🏆 {race.winner.display_name} the {race.winner.animal[0]} won this race!"
    if race.status == "closed":
        return "🚪 The customer closed the race. GAME OVER."
    if race.status == "expired":
        return f"⏰ Time is up. The race expired after {RACE_MINUTES} minutes. GAME OVER."
    return ""


def expire_races():
    for r in list(Race.objects.filter(status="open", expires_at__lte=timezone.now())):
        if Race.objects.filter(pk=r.pk, status="open").update(status="expired"):
            broadcast("lobby", {"type": "race_ended", "race_id": r.id,
                                "message": f"Race for “{r.product}” expired."})
            broadcast(f"race_{r.id}", {"type": "race_over",
                                       "message": f"⏰ Time is up. The race expired after {RACE_MINUTES} minutes. GAME OVER."})


def seconds_left(race_id):
    r = Race.objects.get(pk=race_id)
    return (r.expires_at - timezone.now()).total_seconds() if r.status == "open" else 0


def participant_dict(p):
    photo = p.seller.profile.photo
    return {"id": p.id, "slot": p.slot, "animal": p.animal[0], "emoji": p.animal[1],
            "seller_id": p.seller_id, "name": p.display_name, "product": p.product_name,
            "price": str(p.price), "final_price": str(p.final_price), "details": p.details,
            "image": p.image.url, "photo": photo.url if photo else "",
            "discount": p.discount, "delivery": p.delivery, "promo": p.promo,
            "promo_item": p.promo_item, "progress": p.progress}


def race_state(race_id):
    r = Race.objects.get(pk=race_id)
    parts = r.participants.filter(active=True).select_related("seller__profile").order_by("slot")
    return {"race": {"id": r.id, "product": r.product, "budget": str(r.budget), "status": r.status,
                     "started_at": r.started_at.isoformat(), "expires_at": r.expires_at.isoformat(),
                     "winner_id": r.winner_id, "slots_left": MAX_SELLERS - parts.count()},
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
        p.discount = max(0, min(MAX_DISCOUNT, p.discount + direction))   # 1% per click
    elif field == "delivery":
        p.delivery = direction > 0
    elif field == "promo":
        p.promo = max(0, min(3, p.promo + direction))
        if p.promo == 0:
            p.promo_item = ""
    else:
        return False
    p.save()
    return True


def set_promo_item(user, race_id, text):
    race = Race.objects.get(pk=race_id)
    if not race.is_live:
        return False
    updated = Participant.objects.filter(race=race, seller=user, active=True, promo__gt=0).update(
        promo_item=(text or "").strip()[:120])
    return bool(updated)


# ------------------------------- Chat -------------------------------

def get_room(race, seller):
    return ChatRoom.objects.get_or_create(race=race, seller=seller, defaults={"customer": race.customer})


def can_chat(user, room_id):
    return ChatRoom.objects.filter(pk=room_id).filter(Q(customer=user) | Q(seller=user)).exists()


def msg_dict(m):
    return {"id": m.id, "sender_id": m.sender_id, "text": m.text,
            "system": m.is_system, "created": m.created.isoformat()}


def chat_history(room_id, limit=200):
    qs = ChatMessage.objects.filter(room_id=room_id).order_by("-created", "-id")[:limit]
    return [msg_dict(m) for m in reversed(list(qs))]


def system_message(room, text):
    m = ChatMessage.objects.create(room=room, sender=None, text=text, is_system=True)
    broadcast(f"chat_{room.id}", {"type": "message", "message": msg_dict(m)})
    return m


def save_message(user, room_id, text):
    room = (ChatRoom.objects.select_related("race")
            .filter(pk=room_id).filter(Q(customer=user) | Q(seller=user)).first())
    if not room:
        return None
    m = ChatMessage.objects.create(room=room, sender=user, text=text)
    to_user = room.seller_id if user.id == room.customer_id else room.customer_id
    return {"message": msg_dict(m), "to_user_id": to_user, "room_url": f"/chat/{room.id}/",
            "from_label": display_name(user, room.race)}