from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone

ANIMALS = [("Cheetah", "🐆"), ("Lion", "🦁"), ("Ostrich", "🦤"), ("Greyhound", "🐕"),
           ("Buffalo", "🐃"), ("Kudu", "🦌"), ("Rhino", "🦏"), ("Hare", "🐇"),
           ("Stray Dog", "🐕‍🦺"), ("Tortoise", "🐢")]
MAX_SELLERS = 10
RACE_MINUTES = 20


class Profile(models.Model):
    ROLES = [("none", "None"), ("customer", "Customer"), ("seller", "Seller")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLES, default="none")
    photo = models.ImageField(upload_to="profiles/", blank=True, null=True)
    wins = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class Race(models.Model):
    STATUS = [("open", "Open"), ("closed", "Closed"), ("expired", "Expired"), ("won", "Won")]
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="races")
    product = models.CharField(max_length=200)
    budget = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS, default="open")
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(blank=True, null=True)
    winner = models.ForeignKey("Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def save(self, *a, **k):
        if not self.expires_at:
            self.expires_at = self.started_at + timedelta(minutes=RACE_MINUTES)
        super().save(*a, **k)

    @property
    def is_live(self):
        return self.status == "open" and self.expires_at > timezone.now()

    def __str__(self):
        return f"Race #{self.pk}: {self.product}"


class Participant(models.Model):
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="participants")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    slot = models.PositiveSmallIntegerField()            # 1 = Cheetah ... 10 = Tortoise
    display_name = models.CharField(max_length=60)
    product_name = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    details = models.TextField()
    image = models.ImageField(upload_to="products/")
    discount = models.PositiveSmallIntegerField(default=0)   # percent, 1% steps, max 50
    delivery = models.BooleanField(default=False)
    promo = models.PositiveSmallIntegerField(default=0)      # buy 1 get N free, max 3
    promo_item = models.CharField(max_length=120, blank=True, default="")
    active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    @property
    def animal(self):
        return ANIMALS[self.slot - 1]

    @property
    def final_price(self):
        return (self.price * (100 - self.discount) / Decimal(100)).quantize(Decimal("0.01"))

    @property
    def progress(self):
        # discount: 1 point per %, delivery: 20 points, promo: 10 points per free item. Max 100.
        return min(100, min(self.discount, 50) + (20 if self.delivery else 0) + 10 * self.promo)


class ChatRoom(models.Model):
    """One private conversation between the customer of a race and one seller."""
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="chats")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats_as_customer")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chats_as_seller")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("race", "seller")]

    def __str__(self):
        return f"Chat #{self.pk} (race {self.race_id})"


class ChatMessage(models.Model):
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    text = models.TextField()
    is_system = models.BooleanField(default=False)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created", "id"]

    def __str__(self):
        return f"{self.text[:40]}"


class Comment(models.Model):
    name = models.CharField(max_length=80)
    message = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}: {self.message[:40]}"