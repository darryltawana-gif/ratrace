from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import (Profile, Race, Participant, Post, Circle, CircleMember,
                     CircleMessage, CircleReaction, ChatRoom, ChatMessage, Review,
                     Report, Comment, PushSubscription, AccountGroup, AccountIdentity,
                     QualitySession)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "category", "email", "phone", "account_group",
                    "linked_accounts", "wins", "photo_preview")
    list_filter = ("role", "category", "account_group")
    search_fields = ("user__username", "email", "phone")
    readonly_fields = ("account_group",)

    def linked_accounts(self, obj):
        others = [p.user.username for p in obj.other_accounts]
        return ", ".join(others) if others else "—"
    linked_accounts.short_description = "Same person"

    def photo_preview(self, obj):
        if obj.photo:
            return format_html('<img src="{}" style="height:38px;border-radius:50%">', obj.photo.url)
        return "—"


class ProfileInline(admin.TabularInline):
    model = Profile
    fk_name = "account_group"
    fields = ("user", "role", "email", "phone", "category")
    extra = 0
    can_delete = False
    show_change_link = True


@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ("__str__", "accounts_count", "created")
    search_fields = ("label", "profiles__user__username", "profiles__email", "profiles__phone")
    inlines = [ProfileInline]

    def accounts_count(self, obj):
        return obj.profiles.count()
    accounts_count.short_description = "Accounts"


@admin.register(AccountIdentity)
class AccountIdentityAdmin(admin.ModelAdmin):
    list_display = ("kind", "value", "group", "created")
    list_filter = ("kind",)
    search_fields = ("value",)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("title", "seller", "price", "media_verified", "active", "created")
    list_filter = ("active", "media_verified", "created")
    search_fields = ("title", "seller__username")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("seller", "customer", "good_quality", "comfortable", "reliable",
                    "fair_price", "poor_quality", "unfriendly", "created")
    list_filter = ("good_quality", "comfortable", "reliable", "fair_price",
                   "poor_quality", "unfriendly")
    search_fields = ("seller__username", "customer__username")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("seller", "reporter", "created", "resolved", "screenshot_link")
    list_filter = ("resolved", "created")
    search_fields = ("seller__username", "reporter__username", "problem")

    def screenshot_link(self, obj):
        if obj.screenshot:
            return format_html('<a href="{}" target="_blank">View screenshot</a>',
                               reverse("report_screenshot", args=[obj.id]))
        return "—"


@admin.register(CircleMember)
class CircleMemberAdmin(admin.ModelAdmin):
    list_display = ("seller", "circle", "status", "created", "responded")
    list_filter = ("status",)


@admin.register(CircleReaction)
class CircleReactionAdmin(admin.ModelAdmin):
    list_display = ("message", "user", "emoji", "created")
    list_filter = ("emoji",)
    search_fields = ("user__username", "message__text")


@admin.register(QualitySession)
class QualitySessionAdmin(admin.ModelAdmin):
    list_display = ("race", "seller", "started_at", "ended_at")
    list_filter = ("started_at",)
    search_fields = ("seller__username", "race__product")


admin.site.register([Race, Participant, Circle, CircleMessage, ChatRoom, ChatMessage,
                     Comment, PushSubscription])
admin.site.site_header = "The Rat Race administration"
