from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F, Max, Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import RaceForm, JoinForm, ProfileForm, CommentForm, SignupForm
from .models import Race, Participant, Profile, ChatRoom, ANIMALS, RACE_MINUTES
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
    animals = []
    for i, (name, emoji) in enumerate(ANIMALS, 1):
        text = ("1st to enter the race: the fastest responder!" if i == 1
                else "Last to enter: the slowest responder." if i == 10
                else f"Rank #{i} responder")
        animals.append({"rank": i, "name": name, "emoji": emoji, "text": text})
    hall = Profile.objects.filter(wins__gte=5).select_related("user").order_by("-wins")[:20]
    winners = Race.objects.filter(status="won", winner__isnull=False).select_related(
        "winner__seller__profile").order_by("-started_at")[:6]
    return render(request, "home.html", {"animals": animals, "hall": hall, "winners": winners})


def signup(request):
    # Signed-in users may also create another account: it simply switches them to the new one.
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        user = User.objects.create_user(username=cd["username"], password=cd["password"])
        sv.get_profile(user)
        login(request, user)
        messages.success(request, f"Welcome, {user.username}! Choose below how you want to use The Rat Race.")
        return redirect("home")
    return render(request, "registration/signup.html", {"form": form})


@require_POST
@login_required
def set_role(request, role):
    if role not in ("customer", "seller", "none"):
        return redirect("home")
    p = sv.get_profile(request.user)

    # Cancel the current mode -> always go to the home page, which shows both options
    if role == "none":
        busy = (Race.objects.filter(customer=request.user, status="open", expires_at__gt=timezone.now()).exists()
                or Participant.objects.filter(seller=request.user, active=True, race__status="open",
                                              race__expires_at__gt=timezone.now()).exists())
        if busy:
            messages.error(request, "Exit your current race first, then cancel your mode.")
        else:
            old = p.role
            p.role = "none"
            p.save()
            if old != "none":
                messages.info(request, f"You left {old} mode. Choose how you want to continue.")
        return redirect("home")

    if p.role in ("none", role):
        p.role = role
        p.save()
        if role == "seller" and not p.photo:
            messages.info(request, "Add your profile photo to continue. It identifies you in every race, so you only do this once.")
            return redirect("profile")
    else:
        messages.error(request, f"Cancel {p.role} mode first.")
    return redirect(request.META.get("HTTP_REFERER", "home"))


@login_required
def customer_start(request):
    sv.expire_races()
    if not ensure_role(request, "customer"):
        return redirect("home")
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
                # Lock the race row so a double click can never create two entries
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
                p.save()
            sv.broadcast(f"race_{race.id}", {"type": "state", "state": sv.race_state(race.id)})
            sv.broadcast(f"race_{race.id}", {"type": "event",
                                             "message": f"{p.display_name} entered as the {p.animal[0]} {p.animal[1]}"})
            sv.broadcast("lobby", {"type": "slots_changed", "silent": True})
            return redirect("race_room", race.id)

    # Link to the winner chat (for the customer and for the winning seller)
    chat_url = ""
    if race.status == "won" and race.winner and (is_owner or race.winner.seller_id == request.user.id):
        room, _ = sv.get_room(race, race.winner.seller)
        chat_url = reverse("chat_room", args=[room.id])

    return render(request, "race_room.html", {
        "race": race, "is_owner": is_owner, "mine": mine, "form": form, "profile": profile,
        "chat_url": chat_url,
        "end_message": sv.end_message(race) if race.status != "open" else ""})


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

    # Open a chat with the winner and tell them
    room, _ = sv.get_room(race, p.seller)
    sv.system_message(room, f"🏆 {request.user.username} chose {p.display_name} as the winner of “{race.product}”. "
                            f"Chat here to arrange the deal.")
    chat_url = reverse("chat_room", args=[room.id])

    sv.broadcast(f"race_{race.id}", {"type": "race_over", "message": f"🏆 {p.display_name} the {p.animal[0]} won the race!",
                                     "winner_user_id": p.seller_id, "chat_url": chat_url})
    sv.broadcast("lobby", {"type": "race_ended", "race_id": race.id,
                           "message": f"{p.display_name} won the race for “{race.product}”!"})
    sv.broadcast(f"user_{p.seller_id}", {"type": "chat_request", "room_id": room.id, "url": chat_url,
                                         "message": f"🏆 You won “{race.product}”! {request.user.username} is waiting in the chat."})
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
        sv.broadcast(f"race_{race_id}", {"type": "event", "message": f"😵 {p.display_name} the {p.animal[0]} fainted in the race!"})
        sv.broadcast(f"race_{race_id}", {"type": "state", "state": sv.race_state(race_id)})
        sv.broadcast("lobby", {"type": "slots_changed",
                               "message": f"A spot opened in the race for “{p.race.product}”. Jump in!"})
    return redirect("home")


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
    return render(request, "chat_list.html", {"items": items})


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
    return render(request, "chat_room.html", {
        "room": room, "race": room.race, "offer": offer, "is_customer": is_customer,
        "other_name": sv.display_name(other, room.race),
        "other_photo": prof.photo.url if prof.photo else ""})


# ------------------------------- Other pages -------------------------------

@login_required
def seller_profile(request):
    if not ensure_role(request, "seller"):
        return redirect("home")
    profile = sv.get_profile(request.user)
    form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile photo saved. You're ready to enter races!")
        return redirect("home")
    return render(request, "profile.html", {"form": form, "profile": profile})


def comments(request):
    form = CommentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Thank you for your comment!")
        return redirect("comments")
    return render(request, "comments.html", {"form": form})


def api_live(request):
    return JsonResponse({"races": [sv.lobby_race(r) for r in sv.live_races()]})


def sw_js(request):
    js = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data || '/'));
});
self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || 'The Rat Race', {body: d.body, data: d.url || '/'}));
});
"""
    return HttpResponse(js, content_type="application/javascript")


def manifest(request):
    return JsonResponse({"name": "The Rat Race", "short_name": "RatRace", "start_url": "/",
                         "display": "standalone", "background_color": "#ffffff", "theme_color": "#0b1f4b",
                         "icons": []})