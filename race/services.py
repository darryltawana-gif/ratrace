import json
import logging
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from .models import (Race, Participant, Profile, ChatRoom, ChatMessage, PushSubscription,
                     MAX_SELLERS, RACE_MINUTES)

log = logging.getLogger(__name__)
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


# --------------------- Push notifications (work when the site is closed) ---------------------

_push_pool = ThreadPoolExecutor(max_workers=6)


def push_enabled():
    return bool(getattr(settings, "VAPID_PRIVATE_KEY", "") and getattr(settings, "VAPID_PUBLIC_KEY", ""))


def _send_one(sub, payload, ttl):
    """Runs in a background thread so the website never waits for phones."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush is not installed; push skipped")
        return
    dead = False
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"],
                               "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": "mailto:" + settings.VAPID_ADMIN_EMAIL},
            ttl=ttl,
            headers={"Urgency": "high"},
        )
    except WebPushException as ex:
        status = getattr(getattr(ex, "response", None), "status_code", None)
        if status in (404, 410):
            dead = True          # the phone removed the subscription
        else:
            log.warning("Web push failed (%s): %s", status, ex)
    except Exception:
        log.exception("Web push crashed")
    if dead:
        try:
            PushSubscription.objects.filter(endpoint=sub["endpoint"]).delete()
        finally:
            connection.close()


def _dispatch(subs, payload, ttl):
    if not push_enabled():
        return 0
    n = 0
    for s in subs:
        _push_pool.submit(_send_one, s, payload, ttl)
        n += 1
    return n


def push_to_users(user_ids, payload, ttl=3600):
    try:
        subs = list(PushSubscription.objects.filter(user_id__in=list(user_ids))
                    .values("endpoint", "p256dh", "auth"))
        return _dispatch(subs, payload, ttl)
    except Exception:
        log.exception("push_to_users failed")
        return 0


def push_race_started(race, reopened=False):
    """Alert every seller (and undecided user), except customers, even if their site is closed."""
    try:
        subs = list(PushSubscription.objects.exclude(user=race.customer)
                    .exclude(user__profile__role="customer")
                    .values("endpoint", "p256dh", "auth"))
        payload = {"title": "🟢 Race re-opened!" if reopened else "🟢 New race started!",
                   "body": f"A customer wants “{race.product}” (budget {race.budget}). Tap to enter the race!",
                   "url": f"/race/{race.id}/", "tag": f"race-{race.id}", "sticky": True}
        return _dispatch(subs, payload, RACE_MINUTES * 60)
    except Exception:
        log.exception("push_race_started failed")
        return 0