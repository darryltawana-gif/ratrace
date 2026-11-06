from django.contrib import admin
from django.utils.html import format_html

from .models import (Profile, Race, Participant, Post, Circle, CircleMember,
                     CircleMessage, ChatRoom, ChatMessage, Review, Report,
                     Comment, PushSubscription)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "category", "email", "phone", "wins", "photo_preview")
    list_filter = ("role", "category")
    search_fields = ("user__username", "email", "phone")

    def photo_preview(self, obj):
        if obj.photo:
            return format_html('<img src="{}" style="height:38px;border-radius:50%">', obj.photo.url)
        return "—"


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
            return format_html('<a href="{}" target="_blank">View screenshot</a>', obj.screenshot.url)
        return "—"


@admin.register(CircleMember)
class CircleMemberAdmin(admin.ModelAdmin):
    list_display = ("seller", "circle", "status", "created", "responded")
    list_filter = ("status",)


admin.site.register([Race, Participant, Circle, CircleMessage, ChatRoom, ChatMessage,
                     Comment, PushSubscription])
admin.site.site_header = "The Rat Race administration"
