from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone

MAX_SELLERS = 10
RACE_MINUTES = 20

CATEGORIES = [
    ("clothing", "Clothing"),
    ("beauty", "Beauty and Cosmetics"),
    ("gadgets", "Gadgets"),
    ("electronics", "Electronics"),
    ("toys", "Toys"),
    ("furniture", "Furniture"),
    ("health", "Health and Medical"),
    ("accessories", "Accessories"),
    ("sports", "Sports and Entertainment"),
    ("gifts", "Gifts and School"),
    ("food", "Food and Beverage"),
    ("home", "Home and Kitchen"),
]
CATEGORY_LABELS = dict(CATEGORIES)

# A positive review is any of: Good Quality, Comfortable to talk to,
# Very reliable, Fair Price. Badges are based on that total.
BADGE_PLATINUM = 20
BADGE_GOLD = 10
BADGE_BRONZE = 5
HALL_OF_FAME_MIN = 5

REVIEW_OPTIONS = [
    ("good_quality", "Good Quality", "Diamond"),
    ("poor_quality", "Poor Quality", "Bin"),
    ("comfortable", "Comfortable to talk to", "Heart"),
    ("reliable", "Very reliable", "Delivery"),
    ("fair_price", "Fair Price", "Dollar"),
    ("unfriendly", "Unfriendly", "Angry"),
]
POSITIVE_OPTIONS = ["good_quality", "comfortable", "reliable", "fair_price"]

# How each aggregate review count is shown on seller cards and profiles.
# (count key, full label, short label, icon name, tone)
REVIEW_BADGE_DEFS = [
    ("good", "Good Quality", "Good Quality", "quality", "pos"),
    ("comfortable", "Comfortable to talk to", "Comfortable", "comfortable", "pos"),
    ("reliable", "Very reliable", "Reliable", "reliable", "pos"),
    ("fair", "Fair Price", "Fair Price", "fair", "pos"),
    ("poor", "Poor Quality", "Poor Quality", "poor", "neg"),
    ("unfriendly", "Unfriendly", "Unfriendly", "angry", "neg"),
]

CIRCLE_TOPICS = [
    ("common", "Find something in common"),
    ("jokes", "Laughter and jokes"),
    ("products", "Tell me about your products"),
    ("other", "Any other talks"),
]
CIRCLE_TOPIC_KEYS = {key for key, _label in CIRCLE_TOPICS}

# The small palette offered on every Circle message. Keeping it fixed makes
# the reaction chips predictable on phones and stops arbitrary payloads.
CIRCLE_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]
CIRCLE_REACTION_SET = set(CIRCLE_REACTIONS)


class AccountGroup(models.Model):
    """Several accounts that belong to the same person.

    Two accounts are grouped when they share an email, a phone number or the
    same browser (the device cookie set at sign-up). This is for our records
    only; nothing about the link is shown to other customers or sellers.
    """
    label = models.CharField(max_length=120, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        names = ", ".join(p.user.username for p in self.profiles.select_related("user")[:5])
        return self.label or names or f"Account group #{self.pk}"

    @property
    def accounts(self):
        return [p.user for p in self.profiles.select_related("user").order_by("user__date_joined")]


class AccountIdentity(models.Model):
    """A unique clue (email, phone or device) that links accounts together."""
    KIND = [("email", "Email"), ("phone", "Phone"), ("device", "Device")]
    group = models.ForeignKey(AccountGroup, on_delete=models.CASCADE, related_name="identities")
    kind = models.CharField(max_length=10, choices=KIND)
    value = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("kind", "value")]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"


class Profile(models.Model):
    ROLES = [("none", "None"), ("customer", "Customer"), ("seller", "Seller")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLES, default="none")
    photo = models.ImageField(upload_to="profiles/", blank=True, null=True)
    wins = models.PositiveIntegerField(default=0)
    # Contact details captured at sign-up. At least one is required by the form.
    email = models.EmailField(blank=True, default="", db_index=True)
    phone = models.CharField(max_length=32, blank=True, default="", db_index=True)
    category = models.CharField(max_length=20, choices=CATEGORIES, blank=True, default="")
    account_group = models.ForeignKey(AccountGroup, null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="profiles")

    # ------------------------------ Shop & live location ------------------------------
    # A seller's shop is optional, but the location (address) is required for
    # sellers so customers always know where to collect or meet.
    shop_name = models.CharField(max_length=120, blank=True, default="")
    shop_photo = models.ImageField(upload_to="shops/", blank=True, null=True)
    shop_location = models.CharField(max_length=200, blank=True, default="")
    shop_lat = models.FloatField(blank=True, null=True)
    shop_lng = models.FloatField(blank=True, null=True)
    # Latest live position shared from the browser. Used for the race-room map
    # and for the real-time distance between customer and seller.
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    location_at = models.DateTimeField(blank=True, null=True)

    @property
    def other_accounts(self):
        if not self.account_group_id:
            return Profile.objects.none()
        return self.account_group.profiles.exclude(pk=self.pk).select_related("user")

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def contact(self):
        return self.email or self.phone or ""

    def _counts(self):
        """Positive/negative review totals, using annotations when available."""
        if hasattr(self, "good_count"):
            return {
                "good": self.good_count, "comfortable": self.comfortable_count,
                "reliable": self.reliable_count, "fair": self.fair_count,
                "poor": self.poor_count, "unfriendly": self.unfriendly_count,
            }
        return review_counts(self.user)

    @property
    def positive_score(self):
        c = self._counts()
        return c["good"] + c["comfortable"] + c["reliable"] + c["fair"]

    @property
    def badge(self):
        score = self.positive_score
        if score >= BADGE_PLATINUM:
            return "platinum"
        if score >= BADGE_GOLD:
            return "gold"
        if score >= BADGE_BRONZE:
            return "bronze"
        return ""

    @property
    def badge_label(self):
        return {"platinum": "Platinum", "gold": "Gold", "bronze": "Bronze"}.get(self.badge, "New seller")

    @property
    def hall_of_fame(self):
        """Hall of Fame entry: 5+ combined Good Quality + Comfortable ticks."""
        c = self._counts()
        return (c["good"] + c["comfortable"]) >= HALL_OF_FAME_MIN

    @property
    def category_label(self):
        return CATEGORY_LABELS.get(self.category, "")

    @property
    def review_badges(self):
        """The seller's review counts as neat icon chips, most-mentioned first."""
        return review_badges_from_counts(self._counts())


def review_counts(user):
    """Aggregate a seller's reviews in one query."""
    from django.db.models import Count, Q
    agg = user.reviews_received.aggregate(
        good=Count("id", filter=Q(good_quality=True)),
        comfortable=Count("id", filter=Q(comfortable=True)),
        reliable=Count("id", filter=Q(reliable=True)),
        fair=Count("id", filter=Q(fair_price=True)),
        poor=Count("id", filter=Q(poor_quality=True)),
        unfriendly=Count("id", filter=Q(unfriendly=True)),
    )
    return {"good": agg["good"] or 0, "comfortable": agg["comfortable"] or 0,
            "reliable": agg["reliable"] or 0, "fair": agg["fair"] or 0,
            "poor": agg["poor"] or 0, "unfriendly": agg["unfriendly"] or 0}


def review_badges_from_counts(counts):
    """Turn a review-count dict into the chips shown around the platform."""
    order = {key: i for i, (key, *_rest) in enumerate(REVIEW_BADGE_DEFS)}
    badges = []
    for key, label, short, icon, tone in REVIEW_BADGE_DEFS:
        count = counts.get(key, 0) or 0
        if count:
            badges.append({"key": key, "label": label, "short": short,
                           "icon": icon, "tone": tone, "count": count})
    # Positive traits first, then the most-mentioned, then the fixed display order.
    badges.sort(key=lambda b: (b["tone"] != "pos", -b["count"], order[b["key"]]))
    return badges


class Race(models.Model):
    STATUS = [("open", "Open"), ("closed", "Closed"), ("expired", "Expired"), ("won", "Won")]
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="races")
    # ``product`` and ``budget`` mirror the first RaceItem so older code and
    # older races keep working. New races should always have RaceItems.
    product = models.CharField(max_length=200)
    budget = models.DecimalField(max_digits=10, decimal_places=2)
    location = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS, default="open")
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(blank=True, null=True)
    winner = models.ForeignKey("Participant", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    # The customer's live position, shared from the browser while the race is
    # open. Only people inside the race can see these coordinates.
    customer_lat = models.FloatField(blank=True, null=True)
    customer_lng = models.FloatField(blank=True, null=True)
    customer_location_at = models.DateTimeField(blank=True, null=True)

    def save(self, *a, **k):
        if not self.expires_at:
            self.expires_at = self.started_at + timedelta(minutes=RACE_MINUTES)
        super().save(*a, **k)

    @property
    def is_live(self):
        return self.status == "open" and self.expires_at > timezone.now()

    @property
    def primary_item(self):
        return self.items.first()

    @property
    def item_count(self):
        return self.items.count() or (1 if self.product else 0)

    def __str__(self):
        return f"Race #{self.pk}: {self.product}"


class RaceItem(models.Model):
    """One product or service the customer is racing for.

    A customer may post several products in a single race. Each item has its
    own budget and details, and sellers offer against the item they choose.
    """
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField(default=1)
    product = models.CharField(max_length=200)
    budget = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveSmallIntegerField(default=1)
    details = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.product} (race {self.race_id})"


class Participant(models.Model):
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="participants")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Which requested RaceItem this offer answers. Null for older races and
    # for races created before multi-product support.
    item = models.ForeignKey(RaceItem, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="offers")
    slot = models.PositiveSmallIntegerField()            # internal ordering only
    display_name = models.CharField(max_length=60)
    product_name = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    details = models.TextField()
    image = models.ImageField(upload_to="products/")
    video = models.FileField(upload_to="products/video/", blank=True, null=True)
    media_verified = models.BooleanField(default=False)
    discount = models.PositiveSmallIntegerField(default=0)   # percent, 1% steps, max 50
    delivery = models.BooleanField(default=False)
    promo = models.PositiveSmallIntegerField(default=0)      # buy 1 get N free, max 3
    promo_item = models.CharField(max_length=120, blank=True, default="")
    active = models.BooleanField(default=True)
    # Customer presence for this offer. ``customer_viewed_at`` is the first
    # time the customer saw the lane; ``customer_viewing_at`` is the latest
    # heartbeat and expires after a short TTL.
    customer_viewed_at = models.DateTimeField(blank=True, null=True)
    customer_viewing_at = models.DateTimeField(blank=True, null=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    @property
    def final_price(self):
        return (self.price * (100 - self.discount) / Decimal(100)).quantize(Decimal("0.01"))

    @property
    def progress(self):
        # discount: 1 point per %, delivery: 20 points, promo: 10 points per free item. Max 100.
        return min(100, min(self.discount, 50) + (20 if self.delivery else 0) + 10 * self.promo)


class QualitySession(models.Model):
    """One Quality Proof window per seller in a race.

    The clock starts on the server when the seller starts the proof, so
    reconnecting the camera or refreshing the page cannot reset it. The
    window length lives in ``settings.QUALITY_SECONDS`` (48 minutes).
    """
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="quality_sessions")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="quality_sessions")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(blank=True, null=True)
    # Identifies the seller's current signalling socket. An old socket that
    # disconnects late must not pause a session a newer socket has resumed.
    connection_id = models.CharField(max_length=36, blank=True, default="")

    class Meta:
        unique_together = [("race", "seller")]
        ordering = ["-started_at"]

    def __str__(self):
        return f"Quality proof: {self.seller.username} on race {self.race_id}"

    @property
    def ends_at(self):
        return self.started_at + timedelta(
            seconds=getattr(settings, "QUALITY_SECONDS", 48 * 60))

    def seconds_left(self):
        if self.ended_at:
            return 0
        return max(0, int((self.ends_at - timezone.now()).total_seconds()))


class Post(models.Model):
    """A product or service a seller has listed."""
    KIND = [("image", "Photo"), ("video", "Video")]
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="posts")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    image = models.ImageField(upload_to="posts/", blank=True, null=True)
    video = models.FileField(upload_to="posts/video/", blank=True, null=True)
    media_verified = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.seller.username}: {self.title}"


class Circle(models.Model):
    """A customer's trusted sellers.

    ``current_topic`` is chosen by the customer and stays in force until the
    customer changes it. Sellers always talk inside the customer's topic.
    """
    customer = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="circle")
    current_topic = models.CharField(max_length=20, choices=CIRCLE_TOPICS, default="other")
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Circle of {self.customer.username}"

    @property
    def current_topic_label(self):
        return dict(CIRCLE_TOPICS).get(self.current_topic, "Any other talks")

    def accepted_members(self):
        return self.members.filter(status="accepted").select_related("seller__profile")


class CircleMember(models.Model):
    STATUS = [("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined")]
    circle = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="members")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="circle_memberships")
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    created = models.DateTimeField(auto_now_add=True)
    responded = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = [("circle", "seller")]

    def __str__(self):
        return f"{self.seller.username} in {self.circle} ({self.status})"


class CircleMessage(models.Model):
    circle = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="circle_messages")
    topic = models.CharField(max_length=20, choices=CIRCLE_TOPICS, default="other")
    text = models.TextField()
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="replies")
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created", "id"]

    def __str__(self):
        return f"{self.topic}: {self.text[:40]}"


class CircleReaction(models.Model):
    """One emoji from one member on one Circle message.

    A member may add several different emojis to the same message, and
    tapping the same emoji again removes it.
    """
    message = models.ForeignKey(CircleMessage, on_delete=models.CASCADE,
                                related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="circle_reactions")
    emoji = models.CharField(max_length=8)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("message", "user", "emoji")]
        ordering = ["created", "id"]

    def __str__(self):
        return f"{self.user.username} reacted {self.emoji}"


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


class PushSubscription(models.Model):
    """One phone/browser that agreed to receive alerts, even when the site is closed."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subs")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} @ {self.endpoint[:40]}"


class Review(models.Model):
    """A customer's review of a seller after a race."""
    race = models.ForeignKey(Race, on_delete=models.CASCADE, related_name="reviews")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_written")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_received")
    good_quality = models.BooleanField(default=False)
    poor_quality = models.BooleanField(default=False)
    comfortable = models.BooleanField(default=False)
    reliable = models.BooleanField(default=False)
    fair_price = models.BooleanField(default=False)
    unfriendly = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("race", "seller")]

    def __str__(self):
        return f"Review of {self.seller.username} on race {self.race_id}"

    @property
    def ticks(self):
        return [key for key, _label, _icon in REVIEW_OPTIONS if getattr(self, key)]


class Report(models.Model):
    """A customer's report about a seller."""
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reports_against")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="reports_made")
    problem = models.TextField()
    screenshot = models.ImageField(upload_to="private/reports/%Y/%m/", blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"Report against {self.seller.username} ({self.created:%Y-%m-%d})"


class Comment(models.Model):
    name = models.CharField(max_length=80)
    message = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name}: {self.message[:40]}"
