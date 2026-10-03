from django.contrib import admin
from .models import Profile, Race, Participant, Comment

admin.site.register([Profile, Race, Participant, Comment])
# Register your models here.
