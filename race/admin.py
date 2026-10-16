from django.contrib import admin
from .models import Profile, Race, Participant, Comment, ChatRoom, ChatMessage, PushSubscription

admin.site.register([Profile, Race, Participant, Comment, ChatRoom, ChatMessage, PushSubscription])