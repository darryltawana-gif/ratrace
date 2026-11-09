import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (Race, Participant, Profile, ChatRoom, ChatMessage,
                     Post, Circle, CircleMember, CircleMessage, CircleReaction,
                     PushSubscription, AccountGroup, AccountIdentity, QualitySession,
                     MAX_SELLERS, RACE_MINUTES, CIRCLE_TOPICS, CIRCLE_TOPIC_KEYS,
                     CIRCLE_REACTIONS, CIRCLE_REACTION_SET)

log = logging.getLogger(__name__)
MAX_DISCOUNT = 50
QUALITY_SECONDS = getattr(settings, "QUALITY_SECONDS", 48 * 60)


def ice_servers():
    """The ICE server list handed to the browser for WebRTC.

    STUN is enough on many home networks but fails on mobile data. TURN is
    configured with environment variables so real credentials are never
    committed to the repository.
    """
    servers = [{"urls": url} for url in getattr(settings, "STUN_URLS", [])]
    turn_urls = getattr(settings, "TURN_URLS", [])
    if turn_urls:
        server = {"urls": turn_urls}
        username = getattr(settings, "TURN_USERNAME", "")
        credential = getattr(settings, "TURN_CREDENTIAL", "")
        if username:
            server["username"] = username
        if credential:
            server["credential"] = credential
        servers.append(server)
    if not servers:
        servers = [{"urls": "stun:stun.l.google.com:19302"}]
    return servers


def broadcast(group, payload):
    async_to_sync(get_channel_layer().group_send)(group, {"type": "relay", "payload": payload})


def get_profile(user):
    return Profile.objects.get_or_create(user=user)[0]


# ------------------------------- Account linking -------------------------------

def normalize_email(value):
    return (value or "").strip().lower()


def normalize_phone(value):
    """Keep a comparable phone form: +263771234567 and 0771234567 differ,
    but spaces, dashes and brackets are ignored."""
    raw = (value or "").strip()
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    return re.sub(r"[^\d+]", "", raw)


def link_account(profile, email="", phone="", device=""):
    """Put this profile in the shared group of the same email/phone/device.

    The function never merges people just because they share a username; only
    contact details or the same sign-up browser create a link.
    """
    pairs = []
    email = normalize_email(email)
    phone = normalize_phone(phone)
    device = (device or "").strip()
    if email:
        pairs.append(("email", email))
    if phone:
        pairs.append(("phone", phone))
    if device:
        pairs.append(("device", device))

    groups = []
    if profile.account_group_id:
        groups.append(profile.account_group)
    for kind, value in pairs:
        ident = AccountIdentity.objects.select_related("group").filter(kind=kind, value=value).first()
        if ident:
            groups.append(ident.group)

    if groups:
        group = groups[0]
    else:
        group = AccountGroup.objects.create(label=profile.user.username)

    # If the clues point at several groups, merge them into one.
    for other in {g.pk: g for g in groups}.values():
        if other.pk == group.pk:
            continue
        Profile.objects.filter(account_group=other).update(account_group=group)
        AccountIdentity.objects.filter(group=other).update(group=group)
        AccountGroup.objects.filter(pk=other.pk).delete()

    if profile.account_group_id != group.pk:
        profile.account_group = group
        profile.save(update_fields=["account_group"])

    for kind, value in pairs:
        identity, created = AccountIdentity.objects.get_or_create(
            kind=kind, value=value, defaults={"group": group})
        if not created and identity.group_id != group.pk:
            identity.group = group
            identity.save(update_fields=["group"])
    return group


def display_name(user, race):
    """The name a seller uses in a race (falls back to the username)."""
    p = Participant.objects.filter(race=race, seller=user).order_by("-id").first()
    return p.display_name if p else user.username


def lobby_race(r):
    # No customer location here: this payload is visible to everyone on the site.
    return {"id": r.id, "product": r.product, "budget": str(r.budget),
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
            # A proof cannot outlive the race it belongs to.
            end_quality_for_race(r.id)
            broadcast("lobby", {"type": "race_ended", "race_id": r.id,
                                "message": f"Race for “{r.product}” expired."})
            broadcast(f"race_{r.id}", {"type": "race_over",
                                       "message": f"⏰ Time is up. The race expired after {RACE_MINUTES} minutes. GAME OVER."})
            broadcast(f"race_{r.id}", {"type": "quality_over"})


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


def _group_reactions(rows):
    """Group (emoji, user_id) pairs into the shape the chat renders."""
    order = {emoji: i for i, emoji in enumerate(CIRCLE_REACTIONS)}
    grouped = {}
    for emoji, user_id in rows:
        grouped.setdefault(emoji, []).append(user_id)
    return [{"emoji": emoji, "count": len(users), "users": users}
            for emoji, users in sorted(grouped.items(),
                                       key=lambda item: order.get(item[0], 99))]


def reactions_for(message_id):
    rows = (CircleReaction.objects.filter(message_id=message_id)
            .order_by("created", "id").values_list("emoji", "user_id"))
    return _group_reactions(rows)


def reaction_map_for(message_ids):
    """One query for a whole page of messages, so history stays fast."""
    rows = (CircleReaction.objects.filter(message_id__in=message_ids)
            .order_by("created", "id")
            .values_list("message_id", "emoji", "user_id"))
    grouped = {}
    for message_id, emoji, user_id in rows:
        grouped.setdefault(message_id, []).append((emoji, user_id))
    return {message_id: _group_reactions(pairs)
            for message_id, pairs in grouped.items()}


def circle_message_dict(m, reactions=None):
    if reactions is None:
        reactions = reactions_for(m.id)
    reply_to = None
    if m.reply_to_id:
        original = m.reply_to
        reply_to = {"id": original.id,
                    "sender": original.sender.username if original.sender else "System",
                    "text": original.text[:140]}
    return {"id": m.id, "sender_id": m.sender_id,
            "sender": m.sender.username if m.sender else "System",
            "topic": m.topic, "topic_label": dict(CIRCLE_TOPICS).get(m.topic, "Any other talks"),
            "text": m.text, "created": m.created.isoformat(),
            "reply_to": reply_to, "reactions": reactions}


def circle_history(circle_id, limit=200):
    qs = (CircleMessage.objects.filter(circle_id=circle_id)
          .select_related("sender", "reply_to__sender")
          .order_by("-created", "-id")[:limit])
    messages = list(reversed(list(qs)))
    reaction_map = reaction_map_for([m.id for m in messages])
    return [circle_message_dict(m, reaction_map.get(m.id, [])) for m in messages]


def react_to_circle_message(user, circle_id, message_id, emoji):
    """Toggle one emoji from one member on one message."""
    if emoji not in CIRCLE_REACTION_SET:
        return None
    circle = Circle.objects.filter(pk=circle_id).first()
    if not circle or not is_circle_member(circle, user):
        return None
    message = CircleMessage.objects.filter(pk=message_id, circle=circle).first()
    if not message:
        return None
    existing = CircleReaction.objects.filter(
        message=message, user=user, emoji=emoji).first()
    if existing:
        existing.delete()
    else:
        CircleReaction.objects.create(message=message, user=user, emoji=emoji)
    return {"message_id": message.id, "reactions": reactions_for(message.id)}


def set_circle_topic(user, circle_id, topic):
    """Only the customer may choose the topic, and it stays until changed."""
    if topic not in CIRCLE_TOPIC_KEYS:
        return None
    circle = Circle.objects.filter(pk=circle_id, customer=user).first()
    if not circle:
        return None
    if circle.current_topic != topic:
        circle.current_topic = topic
        circle.save(update_fields=["current_topic"])
    return {"topic": topic, "label": dict(CIRCLE_TOPICS).get(topic, "Any other talks"),
            "by": user.username}


def save_circle_message(user, circle_id, text, topic="", reply_to_id=None):
    circle = Circle.objects.filter(pk=circle_id).first()
    if not circle or not is_circle_member(circle, user):
        return None
    # A topic sent by a seller is deliberately ignored: the customer owns it.
    changed = None
    if user.id == circle.customer_id and topic in CIRCLE_TOPIC_KEYS:
        if topic != circle.current_topic:
            circle.current_topic = topic
            circle.save(update_fields=["current_topic"])
            changed = topic
    used_topic = circle.current_topic if circle.current_topic in CIRCLE_TOPIC_KEYS else "other"
    # Only a message from this same Circle can be quoted as a reply.
    reply_to = None
    if reply_to_id:
        reply_to = CircleMessage.objects.filter(pk=reply_to_id, circle=circle).first()
    m = CircleMessage.objects.create(circle=circle, sender=user, text=text[:1000],
                                     topic=used_topic, reply_to=reply_to)
    return {"message": circle_message_dict(m, []), "topic_changed": changed}


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

def race_access(user, race_id):
    """Only the customer who owns the race or an active seller in it may watch it."""
    if not getattr(user, "is_authenticated", False):
        return False
    return Race.objects.filter(pk=race_id).filter(
        Q(customer=user) | Q(participants__seller=user, participants__active=True)
    ).exists()


def can_stream_quality(user, race_id):
    """Who may open the Quality Proof signalling socket for a race.

    Whether a proof is actually live is checked separately. Keeping this
    check about *access* means a customer who taps View Quality too early
    gets a friendly explanation instead of a silent disconnect.
    """
    race = Race.objects.filter(pk=race_id).first()
    if not race:
        return False
    if race.customer_id == user.id:
        return True
    return race.participants.filter(seller=user, active=True).exists()


def race_role(user, race_id):
    race = Race.objects.get(pk=race_id)
    return "customer" if race.customer_id == user.id else "seller"


def start_quality_session(user, race_id, connection_id=None):
    """Start or resume the seller's Quality Proof window for this race.

    A seller gets one time window per race (``QUALITY_SECONDS``, 48 minutes
    by default). Inside that window they may stop, fix the camera and start
    again as many times as they need: stopping only pauses the stream, it
    does not burn the window. The window itself always counts from the very
    first start, so refreshing or reconnecting never gives extra time.

    ``connection_id`` identifies the signalling socket that is taking over
    the proof. A late disconnect from an old socket can then be ignored
    instead of pausing a session a newer socket has already resumed.
    """
    race = Race.objects.filter(pk=race_id, status="open", expires_at__gt=timezone.now()).first()
    if not race or not race.participants.filter(seller=user, active=True).exists():
        return None
    now = timezone.now()
    with transaction.atomic():
        session = (QualitySession.objects.select_for_update()
                   .filter(race=race, seller=user).first())
        if session is None:
            return QualitySession.objects.create(
                race=race, seller=user, started_at=now,
                connection_id=connection_id or "")
        # The 48-minute window has run out: it can never be restarted.
        if session.started_at <= now - timedelta(seconds=QUALITY_SECONDS):
            if not session.ended_at:
                session.ended_at = now
                session.connection_id = ""
                session.save(update_fields=["ended_at", "connection_id"])
            return None
        # Stopping the video pauses the session. Starting again inside the
        # same window clears the pause marker so the stream can be shown.
        fields = []
        if session.ended_at:
            session.ended_at = None
            fields.append("ended_at")
        if connection_id is not None and session.connection_id != connection_id:
            session.connection_id = connection_id
            fields.append("connection_id")
        if fields:
            session.save(update_fields=fields)
        return session


def quality_for_race(race_id, seller_id=None):
    """The proof that is live right now (optionally for one seller)."""
    cutoff = timezone.now() - timedelta(seconds=QUALITY_SECONDS)
    qs = QualitySession.objects.filter(
        race_id=race_id, ended_at__isnull=True, started_at__gt=cutoff)
    if seller_id is not None:
        qs = qs.filter(seller_id=seller_id)
    return qs.select_related("seller").order_by("-started_at").first()


def quality_seconds_left(session):
    return session.seconds_left() if session else 0


def quality_session_payload(session):
    """The shape the race page uses to light up the customer's button."""
    if not session:
        return {"active": False}
    return {"active": True, "seller_id": session.seller_id,
            "seller": session.seller.username,
            "seconds": quality_seconds_left(session)}


def quality_seller_name(session_id):
    return (QualitySession.objects.filter(pk=session_id)
            .values_list("seller__username", flat=True).first() or "")


def quality_payload(race_id, seller_id=None):
    return quality_session_payload(quality_for_race(race_id, seller_id))


def end_quality_session(session_id):
    """Pause/close the proof stream. A seller can resume inside the window."""
    QualitySession.objects.filter(pk=session_id, ended_at__isnull=True).update(
        ended_at=timezone.now(), connection_id="")


def release_quality_session(session_id, connection_id):
    """Pause a proof only if this signalling socket still owns it.

    Returns the number of rows changed, so a stale socket that disconnects
    after a newer socket has taken over can stay quiet instead of pausing
    (or announcing) the live proof.
    """
    if not session_id or not connection_id:
        return 0
    return QualitySession.objects.filter(
        pk=session_id, connection_id=connection_id, ended_at__isnull=True
    ).update(ended_at=timezone.now(), connection_id="")


def end_quality_for_race(race_id):
    """Close every proof for a race (used when the race ends)."""
    QualitySession.objects.filter(race_id=race_id, ended_at__isnull=True).update(
        ended_at=timezone.now(), connection_id="")


def end_quality_for_seller(race_id, seller_id):
    """Close one seller's proof (used when they leave the race)."""
    QualitySession.objects.filter(race_id=race_id, seller_id=seller_id,
                                  ended_at__isnull=True).update(
        ended_at=timezone.now(), connection_id="")


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
