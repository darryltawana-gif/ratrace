import io
import json
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q, Max
from django.http import JsonResponse, HttpResponse, FileResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_POST

from .forms import (RaceForm, JoinForm, ProfileForm, CommentForm, SignupForm,
                    PostForm, CircleMessageForm, ReviewForm, ReportForm)
from .models import (Race, Participant, Profile, Post, Circle, CircleMember,
                     CircleMessage, Review, Report, Comment, ChatRoom,
                     PushSubscription, CIRCLE_TOPICS, CATEGORY_LABELS, RACE_MINUTES)
from . import services as sv


def ensure_role(request, role):
    p = sv.get_profile(request.user)
    if p.role == "none":
        p.role = role
        p.save()
        return True
    if p.role != role:
        messages.error(request, f"You are in {p.role} mode. Cancel it first to use the {role} side.")
        return False
    return True


def home(request):
    sv.expire_races()
    sellers = sv.annotated_sellers()

    profile = None
    circle = None
    circle_members = []
    circle_pending = 0
    seller_circles = []
    seller_posts = []
    deals = []
    if request.user.is_authenticated:
        profile = sv.get_profile(request.user)
        if profile.role == "customer":
            circle = sv.ensure_circle(request.user)
            circle_members = [sv.circle_member_dict(m) for m in circle.accepted_members()]
            circle_pending = circle.members.filter(status="pending").count()
        elif profile.role == "seller":
            seller_circles = sv.circle_for_seller(request.user)
            circle_pending = sv.pending_requests(request.user).count()
            seller_posts = sv.seller_posts(request.user)[:8]
            deals = sv.recent_deals(request.user)

    hall = [p for p in sellers if p.hall_of_fame]
    return render(request, "home.html", {
        "sellers": sellers, "hall": hall, "categories": CATEGORY_LABELS,
        "circle": circle, "circle_members": circle_members,
        "circle_pending": circle_pending, "seller_circles": seller_circles,
        "seller_posts": seller_posts, "deals": deals, "my_profile": profile,
        "live_count": sv.live_races().count(),
    })


def signup(request):
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        user = User.objects.create_user(username=cd["username"], password=cd["password"])
        profile = sv.get_profile(user)
        email = sv.normalize_email(cd.get("email"))
        phone = (cd.get("phone") or "").strip()
        profile.email = email
        profile.phone = phone
        profile.save(update_fields=["email", "phone"])
        # Group this account with any other account that used the same email,
        # phone number or browser. It stays private in the database/admin.
        device = request.COOKIES.get("rr_device") or uuid.uuid4().hex
        sv.link_account(profile, email=email, phone=phone, device=device)
        login(request, user)
        messages.success(request, f"Welcome, {user.username}! Choose below how you want to use The Rat Race.")
        response = redirect("home")
        response.set_cookie("rr_device", device, max_age=2 * 365 * 24 * 60 * 60,
                            httponly=True, samesite="Lax", secure=not settings.DEBUG)
        return response
    device = request.COOKIES.get("rr_device") or uuid.uuid4().hex
    response = render(request, "registration/signup.html", {"form": form})
    if not request.COOKIES.get("rr_device"):
        response.set_cookie("rr_device", device, max_age=2 * 365 * 24 * 60 * 60,
                            httponly=True, samesite="Lax", secure=not settings.DEBUG)
    return response


def _role_busy(user):
    """True while the user is in a live race on either side."""
    return (Race.objects.filter(customer=user, status="open", expires_at__gt=timezone.now()).exists()
            or Participant.objects.filter(seller=user, active=True, race__status="open",
                                          race__expires_at__gt=timezone.now()).exists())


def _safe_next(request, fallback="home"):
    target = request.META.get("HTTP_REFERER", "")
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return fallback


@require_POST
@login_required
def set_role(request, role):
    if role not in ("customer", "seller", "none"):
        return redirect("home")
    p = sv.get_profile(request.user)
    old = p.role

    if role == "none":
        if _role_busy(request.user):
            messages.error(request, "Exit your current race first, then cancel your mode.")
        else:
            p.role = "none"
            p.save()
            if old != "none":
                messages.info(request, f"You left {old} mode. Choose how you want to continue.")
        return redirect("home")

    # You can switch straight from customer to seller (or back), but only when
    # you are not in the middle of a race.
    if old != "none" and old != role and _role_busy(request.user):
        messages.error(request, "Finish or exit your current race before switching modes.")
        return redirect("home")

    p.role = role
    p.save()
    if role == "customer":
        sv.ensure_circle(request.user)
    if role == "seller" and (not p.photo or not p.category):
        messages.info(request, "Add your profile photo and choose the category you sell in.")
        return redirect("profile")
    if old not in ("none", role):
        messages.success(request, f"You switched from {old} mode to {role} mode.")
    return redirect(_safe_next(request))


@login_required
def customer_start(request):
    sv.expire_races()
    if not ensure_role(request, "customer"):
        return redirect("home")
    sv.ensure_circle(request.user)
    existing = Race.objects.filter(customer=request.user, status="open", expires_at__gt=timezone.now()).first()
    if existing:
        return redirect("race_room", existing.id)
    form = RaceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        race = form.save(commit=False)
        race.customer = request.user
        race.save()
        sv.broadcast("lobby", {"type": "race_started", "race": sv.lobby_race(race),
                               "message": f"🟢 A customer started a new race: {race.product} (budget {race.budget})"})
        sv.push_race_started(race)          # phones get alerted even when the site is closed
        return redirect("race_room", race.id)
    reopenable = Race.objects.filter(customer=request.user, status="closed", expires_at__gt=timezone.now())
    return render(request, "customer_start.html", {"form": form, "reopenable": reopenable,
                                                    "minutes": RACE_MINUTES})


@login_required
def race_room(request, race_id):
    sv.expire_races()
    race = get_object_or_404(Race, pk=race_id)
    profile = sv.get_profile(request.user)
    is_owner = race.customer_id == request.user.id
    if profile.role == "customer" and not is_owner:
        messages.error(request, "Customers can only open their own race.")
        return redirect("home")
    mine = race.participants.filter(seller=request.user, active=True).first()
    form = None
    if not is_owner and not mine and race.is_live and profile.role in ("seller", "none"):
        needs_photo = not profile.photo
        form = JoinForm(request.POST or None, request.FILES or None, needs_photo=needs_photo)
        if request.method == "POST" and form.is_valid():
            if not ensure_role(request, "seller"):
                return redirect("home")
            with transaction.atomic():
                locked = Race.objects.select_for_update().get(pk=race.pk)
                if locked.participants.filter(seller=request.user, active=True).exists():
                    messages.info(request, "You are already in this race.")
                    return redirect("race_room", race.id)
                if not locked.is_live:
                    messages.error(request, "This race has already ended.")
                    return redirect("race_room", race.id)
                slot = sv.next_slot(locked)
                if slot is None:
                    messages.error(request, "Race is full (10 sellers).")
                    return redirect("race_room", race.id)
                if needs_photo:
                    prof = sv.get_profile(request.user)
                    prof.photo = form.cleaned_data["profile_photo"]
                    prof.save(update_fields=["photo"])
                p = form.save(commit=False)
                p.race, p.seller, p.slot = locked, request.user, slot
                p.display_name = request.user.username
                p.product_name = locked.product
                p.media_verified = form.media_verified
                p.save()
            sv.broadcast(f"race_{race.id}", {"type": "state", "state": sv.race_state(race.id)})
            sv.broadcast(f"race_{race.id}", {"type": "event",
                                             "message": f"{p.display_name} entered the race"})
            sv.broadcast("lobby", {"type": "slots_changed", "silent": True})
            return redirect("race_room", race.id)

    chat_url = ""
    if race.status == "won" and race.winner and (is_owner or race.winner.seller_id == request.user.id):
        room, _ = sv.get_room(race, race.winner.seller)
        chat_url = reverse("chat_room", args=[room.id])

    # Review popup: the customer is asked to review the winner once.
    review_form = None
    if (race.status == "won" and race.winner and is_owner
            and not sv.has_reviewed(race, request.user, race.winner.seller)):
        review_form = ReviewForm()

    return render(request, "race_room.html", {
        "race": race, "is_owner": is_owner, "mine": mine, "form": form, "profile": profile,
        "chat_url": chat_url, "review_form": review_form,
        "review_seller": race.winner.seller if race.winner else None,
        "end_message": sv.end_message(race) if race.status != "open" else "",
        "quality_seconds": sv.QUALITY_SECONDS,
        "ice_servers": sv.ice_servers(),
    })


@require_POST
@login_required
def customer_exit(request, race_id):
    race = get_object_or_404(Race, pk=race_id, customer=request.user)
    if race.status == "open":
        race.status = "closed"
        race.save()
        sv.broadcast(f"race_{race.id}", {"type": "race_over", "message": "🚪 The customer closed the race. GAME OVER."})
        sv.broadcast("lobby", {"type": "race_ended", "race_id": race.id,
                               "message": f"Customer closed the race for “{race.product}”. Game over."})
        messages.info(request, "You left the race. You can re-enter it below while time remains.")
    return redirect("start")


@require_POST
@login_required
def customer_reopen(request, race_id):
    race = get_object_or_404(Race, pk=race_id, customer=request.user, status="closed")
    if race.expires_at > timezone.now():
        race.status = "open"
        race.save()
        sv.broadcast("lobby", {"type": "race_started", "race": sv.lobby_race(race),
                               "message": f"🟢 A customer re-entered the race: {race.product}"})
        sv.push_race_started(race, reopened=True)
        return redirect("race_room", race.id)
    messages.error(request, "That race has expired.")
    return redirect("start")


@require_POST
@login_required
def pick_winner(request, race_id, pid):
    race = get_object_or_404(Race, pk=race_id, customer=request.user)
    p = get_object_or_404(Participant, pk=pid, race=race, active=True)
    if not race.is_live:
        return redirect("race_room", race.id)

    race.status, race.winner = "won", p
    race.save()
    Profile.objects.filter(user=p.seller).update(wins=F("wins") + 1)

    room, _ = sv.get_room(race, p.seller)
    sv.system_message(room, f"🏆 {request.user.username} chose {p.display_name} as the winner of “{race.product}”. "
                            f"Chat here to arrange the deal.")
    chat_url = reverse("chat_room", args=[room.id])

    sv.broadcast(f"race_{race.id}", {"type": "race_over", "message": f"🏆 {p.display_name} won the race!",
                                     "winner_user_id": p.seller_id, "chat_url": chat_url})
    sv.broadcast("lobby", {"type": "race_ended", "race_id": race.id,
                           "message": f"{p.display_name} won the race for “{race.product}”!"})
    sv.broadcast(f"user_{p.seller_id}", {"type": "chat_request", "room_id": room.id, "url": chat_url,
                                         "message": f"🏆 You won “{race.product}”! {request.user.username} is waiting in the chat."})
    sv.push_to_users([p.seller_id], {"title": "🏆 You won the race!",
                                     "body": f"{request.user.username} chose you for “{race.product}”. Tap to chat.",
                                     "url": chat_url, "tag": f"win-{race.id}", "sticky": True}, 3600)
    messages.success(request, f"You chose {p.display_name} as the winner! Say hello below.")
    return redirect("chat_room", room.id)


@require_POST
@login_required
def seller_leave(request, race_id):
    entries = list(Participant.objects.filter(race_id=race_id, seller=request.user, active=True))
    for p in entries:
        p.active = False
        p.save()
    if entries:
        p = entries[0]
        sv.broadcast(f"race_{race_id}", {"type": "event", "message": f"{p.display_name} left the race."})
        sv.broadcast(f"race_{race_id}", {"type": "state", "state": sv.race_state(race_id)})
        sv.broadcast("lobby", {"type": "slots_changed",
                               "message": f"A spot opened in the race for “{p.race.product}”. Jump in!"})
    return redirect("home")


# ------------------------------- Reviews -------------------------------

@login_required
def review_seller(request, race_id, seller_id):
    race = get_object_or_404(Race, pk=race_id, customer=request.user)
    seller = get_object_or_404(User, pk=seller_id)
    if not race.participants.filter(seller=seller).exists():
        messages.error(request, "That seller was not in this race.")
        return redirect("home")
    # Reviews are earned: only the winning seller can be reviewed, and only
    # after the customer has actually chosen them.
    if race.status != "won" or not race.winner or race.winner.seller_id != seller.id:
        messages.error(request, "You can review a seller only after choosing them as the winner.")
        return redirect("race_room", race.id)
    existing = Review.objects.filter(race=race, seller=seller).first()
    initial = None
    if existing:
        initial = {key: getattr(existing, key) for key in
                   ("good_quality", "poor_quality", "comfortable", "reliable",
                    "fair_price", "unfriendly")}
    form = ReviewForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        Review.objects.update_or_create(
            race=race, seller=seller,
            defaults={"customer": request.user, **{k: form.cleaned_data[k] for k in
                                                   ("good_quality", "poor_quality", "comfortable",
                                                    "reliable", "fair_price", "unfriendly")}})
        messages.success(request, f"Thank you! Your review of {seller.username} was saved.")
        nxt = request.POST.get("next") or "home"
        if nxt == "chat":
            room, _ = sv.get_room(race, seller)
            return redirect("chat_room", room.id)
        return redirect("race_room", race.id)
    return render(request, "review.html", {"form": form, "race": race, "seller": seller,
                                           "existing": existing, "next": request.GET.get("next", "")})


# ------------------------------- Chat -------------------------------

@require_POST
@login_required
def start_chat(request, race_id, pid):
    race = get_object_or_404(Race, pk=race_id, customer=request.user)
    p = get_object_or_404(Participant, pk=pid, race=race)
    allowed = (race.is_live and p.active) or (race.status == "won" and race.winner_id == p.id)
    if not allowed:
        messages.error(request, "You can only chat with sellers while the race is running.")
        return redirect("race_room", race.id)
    room, created = sv.get_room(race, p.seller)
    if created:
        sv.system_message(room, f"💬 Chat started about “{race.product}”.")
    url = reverse("chat_room", args=[room.id])
    sv.broadcast(f"user_{p.seller_id}", {"type": "chat_request", "room_id": room.id, "url": url,
                                         "message": f"💬 {request.user.username} wants to chat about “{race.product}”"})
    sv.push_to_users([p.seller_id], {"title": "💬 A customer wants to chat",
                                     "body": f"{request.user.username} is asking about “{race.product}”. Tap to reply.",
                                     "url": url, "tag": f"chat-{room.id}", "sticky": True}, 3600)
    return redirect("chat_room", room.id)


@login_required
def chat_list(request):
    rooms = (ChatRoom.objects.filter(Q(customer=request.user) | Q(seller=request.user))
             .select_related("race", "customer", "seller")
             .annotate(last_at=Max("messages__created"))
             .order_by(F("last_at").desc(nulls_last=True), "-created"))
    items = []
    for r in rooms:
        other = r.seller if r.customer_id == request.user.id else r.customer
        prof = sv.get_profile(other)
        last = r.messages.order_by("-created", "-id").first()
        items.append({"room": r, "name": sv.display_name(other, r.race),
                      "photo": prof.photo.url if prof.photo else "", "last": last})
    circles = sv.circle_for_seller(request.user)
    return render(request, "chat_list.html", {"items": items, "seller_circles": circles})


@login_required
def chat_room(request, room_id):
    room = get_object_or_404(ChatRoom.objects.select_related("race", "customer", "seller"), pk=room_id)
    if request.user.id not in (room.customer_id, room.seller_id):
        messages.error(request, "That chat isn't yours.")
        return redirect("home")
    is_customer = request.user.id == room.customer_id
    other = room.seller if is_customer else room.customer
    prof = sv.get_profile(other)
    offer = Participant.objects.filter(race=room.race, seller=room.seller).order_by("-id").first()
    can_review = (is_customer and room.race.status == "won" and room.race.winner
                  and room.race.winner.seller_id == room.seller_id
                  and not sv.has_reviewed(room.race, request.user, room.seller))
    return render(request, "chat_room.html", {
        "room": room, "race": room.race, "offer": offer, "is_customer": is_customer,
        "other_name": sv.display_name(other, room.race),
        "other_photo": prof.photo.url if prof.photo else "",
        "other_id": other.id, "can_review": can_review})


# ------------------------------- Push alerts -------------------------------

@require_POST
@login_required
def push_subscribe(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
        endpoint = data["endpoint"]
        keys = data["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "bad subscription"}, status=400)
    if not str(endpoint).startswith("https://"):
        return JsonResponse({"ok": False, "error": "bad endpoint"}, status=400)
    # One device = one subscription. Whoever is signed in on that device receives its alerts.
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth,
                  "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300]})
    return JsonResponse({"ok": True})


@require_POST
@login_required
def push_test(request):
    sent = sv.push_to_users([request.user.id], {
        "title": "✅ Alerts are working!",
        "body": "You will be told when a customer starts a race, even when the site is closed.",
        "url": "/", "tag": "test", "force": True}, 300)
    return JsonResponse({"sent": sent})


# ------------------------------- Other pages -------------------------------
# ------------------------------- Profile & seller pages -------------------------------

@login_required
def profile(request):
    profile = sv.get_profile(request.user)
    form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        if profile.role == "seller" and not form.cleaned_data.get("category"):
            form.add_error("category", "Please choose the category you sell in.")
        else:
            profile = form.save()
            device = request.COOKIES.get("rr_device", "")
            sv.link_account(profile, email=profile.email, phone=profile.phone, device=device)
            messages.success(request, "Profile saved.")
            return redirect("home")
    return render(request, "profile.html", {"form": form, "profile": profile})


def seller_public(request, user_id):
    profile = get_object_or_404(Profile.objects.select_related("user"),
                                user__id=user_id, role="seller")
    posts = sv.seller_posts(profile.user)
    counts = profile._counts()
    reviews = profile.user.reviews_received.select_related("customer").order_by("-created")[:20]
    in_circle = ""
    if request.user.is_authenticated and request.user.id != profile.user_id:
        member = CircleMember.objects.filter(circle__customer=request.user, seller=profile.user).first()
        in_circle = member.status if member else ""
    return render(request, "seller_public.html", {
        "seller": profile, "posts": posts, "counts": counts, "reviews": reviews,
        "in_circle": in_circle, "categories": CATEGORY_LABELS})


@login_required
def my_posts(request):
    profile = sv.get_profile(request.user)
    if profile.role != "seller":
        messages.error(request, "Only sellers can post products.")
        return redirect("home")
    posts = sv.seller_posts(request.user, include_inactive=True)
    return render(request, "posts.html", {"posts": posts})


@login_required
def post_create(request):
    profile = sv.get_profile(request.user)
    if profile.role != "seller":
        messages.error(request, "Only sellers can post products.")
        return redirect("home")
    form = PostForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        post = form.save(commit=False)
        post.seller = request.user
        post.media_verified = form.media_verified
        post.save()
        messages.success(request, "Your post is live.")
        return redirect("my_posts")
    return render(request, "post_form.html", {"form": form})


@require_POST
@login_required
def post_delete(request, post_id):
    post = get_object_or_404(Post, pk=post_id, seller=request.user)
    post.active = False
    post.save(update_fields=["active"])
    messages.info(request, "Post removed.")
    return redirect("my_posts")


# ------------------------------- My Circle -------------------------------

@login_required
def my_circle(request):
    profile = sv.get_profile(request.user)
    if profile.role == "customer":
        circle = sv.ensure_circle(request.user)
        members = [sv.circle_member_dict(m) for m in circle.accepted_members()]
        pending = [sv.circle_member_dict(m) for m in circle.members.filter(status="pending")
                   .select_related("seller__profile")]
        sellers = sv.annotated_sellers().exclude(user=request.user)
        in_circle_ids = set(circle.members.values_list("seller_id", flat=True))
        suggestions = [s for s in sellers if s.user_id not in in_circle_ids]
        return render(request, "circle.html", {
            "mode": "customer", "circle": circle, "members": members,
            "pending": pending, "suggestions": suggestions, "topics": CIRCLE_TOPICS,
        })

    # Seller: every circle they belong to, plus requests waiting for an answer.
    circles = sv.circle_for_seller(request.user)
    pending = sv.pending_requests(request.user)
    deals = sv.recent_deals(request.user)
    return render(request, "circle.html", {
        "mode": "seller", "circles": circles, "pending": pending, "deals": deals,
    })


@require_POST
@login_required
def circle_request(request, seller_id):
    if sv.get_profile(request.user).role != "customer":
        messages.error(request, "Only customers build a Circle.")
        return redirect("home")
    seller = get_object_or_404(Profile, user__id=seller_id, role="seller").user
    if seller.id == request.user.id:
        return redirect("home")
    circle = sv.ensure_circle(request.user)
    member, created = CircleMember.objects.get_or_create(circle=circle, seller=seller)
    if not created and member.status == "accepted":
        messages.info(request, f"{seller.username} is already in your Circle.")
    elif not created and member.status == "pending":
        messages.info(request, "Your request is still waiting for the seller to answer.")
    else:
        member.status = "pending"
        member.save(update_fields=["status"])
        sv.broadcast(f"user_{seller.id}", {"type": "chat_request", "room_id": 0,
                                          "url": reverse("my_circle"),
                                          "message": f"🤝 {request.user.username} wants to add you to their Circle."})
        messages.success(request, f"Request sent to {seller.username}.")
    return redirect("seller_public", seller.id)


@require_POST
@login_required
def circle_respond(request, member_id, action):
    member = get_object_or_404(CircleMember, pk=member_id, seller=request.user, status="pending")
    if action not in ("accept", "decline"):
        return redirect("my_circle")
    member.status = "accepted" if action == "accept" else "declined"
    member.responded = timezone.now()
    member.save(update_fields=["status", "responded"])
    label = "added you to their Circle" if action == "accept" else "declined your Circle request"
    sv.broadcast(f"user_{member.circle.customer_id}", {
        "type": "chat_request", "room_id": 0, "url": reverse("my_circle"),
        "message": f"🤝 {request.user.username} {label}."})
    messages.success(request, "Circle request answered.")
    return redirect("my_circle")


@require_POST
@login_required
def circle_remove(request, member_id):
    """A customer can remove a seller from their Circle at any time."""
    member = get_object_or_404(CircleMember, pk=member_id, circle__customer=request.user)
    seller_id = member.seller_id
    seller_name = member.seller.username
    circle_id = member.circle_id
    member.delete()
    sv.broadcast(f"user_{seller_id}", {
        "type": "chat_request", "room_id": 0, "url": reverse("my_circle"),
        "message": f"👋 {request.user.username} removed you from their Circle."})
    sv.broadcast(f"circle_{circle_id}", {
        "type": "system", "message": f"{seller_name} was removed from the Circle."})
    messages.success(request, f"{seller_name} was removed from your Circle.")
    return redirect("my_circle")


@login_required
def circle_chat(request, circle_id):
    circle = get_object_or_404(Circle.objects.select_related("customer__profile"), pk=circle_id)
    if not sv.is_circle_member(circle, request.user):
        messages.error(request, "That Circle isn't yours.")
        return redirect("home")
    members = [sv.circle_member_dict(m) for m in circle.accepted_members()]
    is_owner = circle.customer_id == request.user.id
    return render(request, "circle_chat.html", {
        "circle": circle, "members": members, "is_owner": is_owner,
        "topics": CIRCLE_TOPICS, "circle_id": circle.id,
        "current_topic": circle.current_topic,
        "current_topic_label": circle.current_topic_label,
    })


@require_POST
@login_required
def circle_talk(request, circle_id):
    circle = get_object_or_404(Circle, pk=circle_id, customer=request.user)
    topic = request.POST.get("topic", "")
    if topic not in dict(CIRCLE_TOPICS):
        messages.error(request, "Choose one of the Circle topics.")
        return redirect("circle_chat", circle.id)
    label = dict(CIRCLE_TOPICS)[topic]
    changed = sv.set_circle_topic(request.user, circle.id, topic)
    msg = CircleMessage.objects.create(
        circle=circle, sender=request.user, topic=topic,
        text=f"Let's talk: {label}.")
    sv.broadcast(f"circle_{circle.id}",
                 {"type": "message", "message": sv.circle_message_dict(msg)})
    if changed:
        sv.broadcast(f"circle_{circle.id}",
                     {"type": "topic", "topic": topic, "label": label,
                      "by": request.user.username})
    for uid in sv.circle_recipient_ids(circle.id, exclude=request.user.id):
        sv.broadcast(f"user_{uid}", {"type": "chat_message", "room_id": circle.id,
                                     "message": f"👥 {request.user.username} started a topic: {label}",
                                     "url": reverse("circle_chat", args=[circle.id])})
    return redirect("circle_chat", circle.id)


# ------------------------------- Reports -------------------------------

@login_required
def report_seller(request, seller_id):
    seller = get_object_or_404(Profile, user__id=seller_id, role="seller").user
    form = ReportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        Report.objects.create(
            seller=seller, reporter=request.user,
            problem=form.cleaned_data["problem"],
            screenshot=form.cleaned_data.get("screenshot"))
        messages.success(request, "Your report has been sent to the Rat Race team. Thank you.")
        return redirect("seller_public", seller.id)
    return render(request, "report.html", {"form": form, "seller": seller})


@login_required
def report_screenshot(request, report_id):
    """Report screenshots are private evidence for staff only."""
    if not request.user.is_staff:
        raise PermissionDenied
    report = get_object_or_404(Report, pk=report_id)
    if not report.screenshot:
        raise Http404("No screenshot attached.")
    try:
        return FileResponse(report.screenshot.open("rb"))
    except FileNotFoundError:
        raise Http404("Screenshot file is missing.")


# ------------------------------- Other pages -------------------------------

def comments(request):
    form = CommentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Thank you for your comment!")
        return redirect("comments")
    return render(request, "comments.html", {"form": form, "items": Comment.objects.all()[:50]})


def api_live(request):
    return JsonResponse({"races": [sv.lobby_race(r) for r in sv.live_races()]})


def sw_js(request):
    js = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));

// A push arrives from the server, even when the website and browser are closed
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil((async () => {
    const wins = await clients.matchAll({type: 'window', includeUncontrolled: true});
    const visible = wins.some(c => c.visibilityState === 'visible');
    if (visible && !d.force) return;   // the open page already shows its own alert
    await self.registration.showNotification(d.title || 'The Rat Race', {
      body: d.body || '',
      data: d.url || '/',
      tag: d.tag || 'rat-race',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      vibrate: [200, 100, 200, 100, 200],
      requireInteraction: true
    });
  })());
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data || '/';
  e.waitUntil((async () => {
    const wins = await clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of wins) {
      if ('focus' in c) {
        await c.focus();
        if ('navigate' in c) { try { await c.navigate(url); } catch (err) {} }
        return;
      }
    }
    await clients.openWindow(url);
  })());
});
"""
    resp = HttpResponse(js, content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"
    resp["Service-Worker-Allowed"] = "/"
    return resp


def manifest(request):
    return JsonResponse({
        "name": "The Rat Race", "short_name": "RatRace", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#0b1f4b", "theme_color": "#0b1f4b",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                  {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


@cache_control(max_age=86400, public=True)
def icon_png(request, size):
    """App icon (navy square, red circle, white R). Used for the home-screen icon and notifications."""
    from PIL import Image, ImageDraw, ImageFont
    size = size if size in (180, 192, 512) else 192
    img = Image.new("RGB", (size, size), "#0b1f4b")
    d = ImageDraw.Draw(img)
    m = int(size * 0.10)
    d.ellipse([m, m, size - m, size - m], fill="#d90429")
    try:
        font = ImageFont.load_default(size=int(size * 0.5))
        d.text((size / 2, size / 2), "R", fill="#ffffff", font=font, anchor="mm")
    except Exception:
        d.text((size * 0.42, size * 0.38), "R", fill="#ffffff")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")
