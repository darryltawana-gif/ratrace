from django.shortcuts import render
from functools import wraps
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import RaceForm, JoinForm, ProfileForm, CommentForm, SignupForm
from .models import Race, Participant, Profile, ANIMALS, MAX_SELLERS
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
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("home")
    return render(request, "registration/signup.html", {"form": form})


@require_POST
@login_required
def set_role(request, role):
    p = sv.get_profile(request.user)
    if role == "none":
        busy = (Race.objects.filter(customer=request.user, status="open", expires_at__gt=timezone.now()).exists()
                or Participant.objects.filter(seller=request.user, active=True, race__status="open",
                                              race__expires_at__gt=timezone.now()).exists())
        if busy:
            messages.error(request, "Exit your current race first.")
        else:
            p.role = "none"
            p.save()
    elif p.role in ("none", role):
        p.role = role
        p.save()
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
    return render(request, "customer_start.html", {"form": form, "reopenable": reopenable})


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
        form = JoinForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            if not ensure_role(request, "seller"):
                return redirect("home")
            if not profile.photo:
                messages.error(request, "Upload your profile photo first.")
                return redirect("profile")
            slot = sv.next_slot(race)
            if slot is None:
                messages.error(request, "Race is full (10 sellers).")
                return redirect("race_room", race.id)
            p = form.save(commit=False)
            p.race, p.seller, p.slot = race, request.user, slot
            p.save()
            sv.broadcast(f"race_{race.id}", {"type": "state", "state": sv.race_state(race.id)})
            sv.broadcast(f"race_{race.id}", {"type": "event", "message": f"{p.display_name} entered as the {p.animal[0]} {p.animal[1]}"})
            sv.broadcast("lobby", {"type": "slots_changed", "silent": True})
            return redirect("race_room", race.id)
    return render(request, "race_room.html", {"race": race, "is_owner": is_owner, "mine": mine,
                                              "form": form, "profile": profile})


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
    if race.is_live:
        race.status, race.winner = "won", p
        race.save()
        Profile.objects.filter(user=p.seller).update(wins=F("wins") + 1)
        sv.broadcast(f"race_{race.id}", {"type": "race_over", "message": f"🏆 {p.display_name} the {p.animal[0]} won the race!"})
        sv.broadcast("lobby", {"type": "race_ended", "race_id": race.id,
                               "message": f"{p.display_name} won the race for “{race.product}”!"})
    return redirect("race_room", race.id)


@require_POST
@login_required
def seller_leave(request, race_id):
    p = Participant.objects.filter(race_id=race_id, seller=request.user, active=True).first()
    if p:
        p.active = False
        p.save()
        sv.broadcast(f"race_{race_id}", {"type": "event", "message": f"😵 {p.display_name} the {p.animal[0]} fainted in the race!"})
        sv.broadcast(f"race_{race_id}", {"type": "state", "state": sv.race_state(race_id)})
        sv.broadcast("lobby", {"type": "slots_changed",
                               "message": f"A spot opened in the race for “{p.race.product}”. Jump in!"})
    return redirect("home")


@login_required
def seller_profile(request):
    if not ensure_role(request, "seller"):
        return redirect("home")
    profile = sv.get_profile(request.user)
    form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile photo saved.")
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
                         "display": "standalone", "background_color": "#14110f", "theme_color": "#14110f",
                         "icons": []})
# Create your views here.
