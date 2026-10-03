from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Race, Participant, Profile, Comment


class RaceForm(forms.ModelForm):
    class Meta:
        model = Race
        fields = ["product", "budget"]
        labels = {"product": "What product do you want?", "budget": "Your budget"}


class JoinForm(forms.ModelForm):
    class Meta:
        model = Participant
        fields = ["display_name", "product_name", "price", "details", "image"]
        labels = {"display_name": "Name you use", "image": "Product picture"}


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["photo"]
        labels = {"photo": "Profile photo (your unique identifier)"}


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["name", "message"]


class SignupForm(UserCreationForm):
    class Meta:
        model = User
        fields = ["username"]