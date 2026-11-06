import json
import logging
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (Race, Participant, Profile, ChatRoom, ChatMessage,
                     Post, Circle, CircleMember, CircleMessage, PushSubscription,
                     MAX_SELLERS, RACE_MINUTES, POSITIVE_OPTIONS)

log = logging.getLogger(__name__)
MAX_DISCOUNT = 50
QUALITY_SECONDS = 30


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
            "location": r.location,
            "started_at": r.started_at.isoformat(), "expires_at": r.expires_at.isoformat(),
            "slots_left": MAX_SELLERS - r.participants.filter(active=True).count()}


def live_races():
    expire_races()
    return Race.objects.filter(status="open", expires_at__gt=timezone.now()).order_by("-started_at")


def end_message(race):
    if race.status == "won" and race.winner:
        return f"🏆 {race.winner.display_name} won this race!"
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
    photo = getattr(getattr(p.seller, "profile", None), "photo", None)
    category = getattr(getattr(p.seller, "profile", None), "category_label", "")
    return {"id": p.id, "slot": p.slot, "seller_id": p.seller_id, "name": p.display_name,
            "product": p.product_name, "price": str(p.price), "final_price": str(p.final_price),
            "details": p.details, "image": p.image.url, "video": p.video.url if p.video else "",
            "media_verified": p.media_verified, "category": category,
            "photo": photo.url if photo else "",
            "discount": p.discount, "delivery": p.delivery, "promo": p.promo,
            "promo_item": p.promo_item, "progress": p.progress}


def race_state(race_id):
    r = Race.objects.get(pk=race_id)
    parts = r.participants.filter(active=True).select_related("seller__profile").order_by("slot")
    return {"race": {"id": r.id, "product": r.product, "budget": str(r.budget),
                     "location": r.location,
                     "status": r.status,
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


# ------------------------------- Sellers & reviews -------------------------------

def annotated_sellers():
    """Every signed-up seller, with review counts in one query."""
    return (Profile.objects.filter(role="seller").select_related("user")
            .annotate(
                good_count=Count("user__reviews_received",
                                 filter=Q(user__reviews_received__good_quality=True), distinct=True),
                comfortable_count=Count("user__reviews_received",
                                        filter=Q(user__reviews_received__comfortable=True), distinct=True),
                reliable_count=Count("user__reviews_received",
                                     filter=Q(user__reviews_received__reliable=True), distinct=True),
                fair_count=Count("user__reviews_received",
                                 filter=Q(user__reviews_received__fair_price=True), distinct=True),
                poor_count=Count("user__reviews_received",
                                 filter=Q(user__reviews_received__poor_quality=True), distinct=True),
                unfriendly_count=Count("user__reviews_received",
                                       filter=Q(user__reviews_received__unfriendly=True), distinct=True),
            )
            .order_by("-wins", "user__username"))


def hall_of_fame():
    return [p for p in annotated_sellers() if p.hall_of_fame]


def seller_posts(seller, include_inactive=False):
    qs = Post.objects.filter(seller=seller)
    if not include_inactive:
        qs = qs.filter(active=True)
    return qs


def recent_deals(user, limit=20):
    """Pairs of customer and seller from races that were completed."""
    return (Race.objects.filter(status="won", winner__isnull=False)
            .filter(Q(customer=user) | Q(winner__seller=user))
            .select_related("customer__profile", "winner__seller__profile")
            .order_by("-started_at")[:limit])


def review_for(race, seller):
    return race.reviews.filter(seller=seller).first()


def has_reviewed(race, customer, seller):
    return race.reviews.filter(customer=customer, seller=seller).exists()


# ------------------------------- My Circle -------------------------------

def ensure_circle(customer):
    return Circle.objects.get_or_create(customer=customer)[0]


def is_circle_member(circle, user):
    if circle.customer_id == user.id:
        return True
    return circle.members.filter(seller=user, status="accepted").exists()


def circle_for_seller(user):
    """The circles this seller has been accepted into."""
    return (Circle.objects.filter(members__seller=user, members__status="accepted")
            .select_related("customer__profile").distinct())


def circle_member_dict(m):
    prof = getattr(m.seller, "profile", None)
    photo = getattr(prof, "photo", None)
    return {"user": m.seller, "name": m.seller.username,
            "photo": photo.url if photo else "",
            "category": getattr(prof, "category_label", ""),
            "badge": getattr(prof, "badge", ""), "status": m.status, "id": m.id}


def pending_requests(user):
    return (CircleMember.objects.filter(seller=user, status="pending")
            .select_related("circle__customer__profile"))


def circle_message_dict(m):
    from .models import CIRCLE_TOPICS
    return {"id": m.id, "sender_id": m.sender_id,
            "sender": m.sender.username if m.sender else "System",
            "topic": m.topic, "topic_label": dict(CIRCLE_TOPICS).get(m.topic, "Any other talks"),
            "text": m.text, "created": m.created.isoformat()}


def circle_history(circle_id, limit=200):
    qs = CircleMessage.objects.filter(circle_id=circle_id).order_by("-created", "-id")[:limit]
    return [circle_message_dict(m) for m in reversed(list(qs))]


def save_circle_message(user, circle_id, text, topic="other"):
    circle = Circle.objects.filter(pk=circle_id).first()
    if not circle or not is_circle_member(circle, user):
        return None
    m = CircleMessage.objects.create(circle=circle, sender=user, text=text[:1000], topic=topic)
    return {"message": circle_message_dict(m)}


def get_circle(circle_id):
    return Circle.objects.filter(pk=circle_id).first()


def circle_recipient_ids(circle_id, exclude=None):
    ids = list(CircleMember.objects.filter(circle_id=circle_id, status="accepted")
               .values_list("seller_id", flat=True))
    circle = Circle.objects.filter(pk=circle_id).first()
    if circle:
        ids.append(circle.customer_id)
    return [i for i in set(ids) if i != exclude]


# ------------------------------- Quality Proof video -------------------------------

def can_stream_quality(user, race_id):
    race = Race.objects.filter(pk=race_id, status="open", expires_at__gt=timezone.now()).first()
    if not race:
        return False
    if race.customer_id == user.id:
        return True
    return race.participants.filter(seller=user, active=True).exists()


def race_role(user, race_id):
    race = Race.objects.get(pk=race_id)
    return "customer" if race.customer_id == user.id else "seller"


# ------------------------------- Private chat -------------------------------

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
