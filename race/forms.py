from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import (Race, Participant, Profile, Post, Comment, CircleMessage,
                     CIRCLE_TOPICS, CATEGORIES)
from .validators import inspect_image, inspect_video


def normalize_username(value):
    """'John  Doe' -> 'John_Doe' so people can type names naturally."""
    return "_".join(value.split())


class SignupForm(forms.Form):
    username = forms.CharField(
        max_length=150, label="Username",
        widget=forms.TextInput(attrs={"placeholder": "Any name you like", "autocomplete": "username",
                                      "autocapitalize": "none"}))
    email = forms.EmailField(
        required=False, label="Email address",
        widget=forms.EmailInput(attrs={"placeholder": "you@example.com", "autocomplete": "email"}))
    phone = forms.CharField(
        required=False, max_length=32, label="Phone number",
        widget=forms.TextInput(attrs={"placeholder": "+263 77 000 0000", "autocomplete": "tel"}))
    password = forms.CharField(
        min_length=8, label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "At least 8 characters", "autocomplete": "new-password"}))

    def clean_username(self):
        name = normalize_username(self.cleaned_data["username"])
        if User.objects.filter(username__iexact=name).exists():
            raise forms.ValidationError("That username is already taken. Please try another one.")
        return name

    def clean_password(self):
        password = self.cleaned_data["password"]
        candidate = User(username=self.cleaned_data.get("username", ""))
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages)
        return password

    def clean(self):
        cd = super().clean()
        email = (cd.get("email") or "").strip()
        phone = (cd.get("phone") or "").strip()
        if not email and not phone:
            raise forms.ValidationError("Please give us an email address or a phone number so we can reach you.")
        # We deliberately allow the same email/phone on a second account.
        # The accounts are linked in the database so we can see it is one person.
        return cd


class LoginForm(AuthenticationForm):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["username"].widget.attrs.update({"placeholder": "Your username", "autocapitalize": "none"})
        self.fields["password"].widget.attrs.update({"placeholder": "Your password"})
        self.error_messages = {**self.error_messages,
                               "invalid_login": "Wrong username or password. Please try again."}

    def clean_username(self):
        return normalize_username(self.cleaned_data["username"])


class RaceForm(forms.ModelForm):
    class Meta:
        model = Race
        fields = ["product", "budget", "location"]
        labels = {"product": "What product or service do you want?",
                  "budget": "Your budget",
                  "location": "Where are you right now?"}
        widgets = {
            "product": forms.TextInput(attrs={"placeholder": "e.g. A leather handbag"}),
            "budget": forms.NumberInput(attrs={"placeholder": "e.g. 150.00", "step": "0.01"}),
            "location": forms.TextInput(attrs={"placeholder": "e.g. Avondale, Harare"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].required = True

    def clean_budget(self):
        b = self.cleaned_data["budget"]
        if b <= 0:
            raise forms.ValidationError("Budget must be more than 0.")
        return b

    def clean_location(self):
        location = (self.cleaned_data.get("location") or "").strip()
        if not location:
            raise forms.ValidationError("Tell sellers where you are so they know where to deliver.")
        return location


class JoinForm(forms.ModelForm):
    """Seller entry. Username and the race's product are filled in automatically."""
    profile_photo = forms.ImageField(label="Your profile photo (identifies you in races)", required=True,
                                     widget=forms.FileInput(attrs={"accept": "image/*"}))

    class Meta:
        model = Participant
        fields = ["price", "details", "image", "video"]
        labels = {"price": "Your price", "details": "Tell the customer about your offer",
                  "image": "Product photo (from your phone camera)",
                  "video": "Product video (optional, recorded on your phone)"}
        widgets = {
            "price": forms.NumberInput(attrs={"placeholder": "e.g. 120.00", "step": "0.01"}),
            "details": forms.Textarea(attrs={"rows": 3, "placeholder": "What makes your offer great?"}),
        }

    media_verified = False

    def __init__(self, *a, needs_photo=False, **k):
        super().__init__(*a, **k)
        self.fields["image"].widget.attrs["accept"] = "image/*"
        self.fields["video"].widget.attrs["accept"] = "video/*"
        if not needs_photo:
            del self.fields["profile_photo"]

    def clean_price(self):
        p = self.cleaned_data["price"]
        if p <= 0:
            raise forms.ValidationError("Price must be more than 0.")
        return p

    def clean_image(self):
        f = self.cleaned_data.get("image")
        if f:
            self.media_verified = inspect_image(f)
        return f

    def clean_video(self):
        f = self.cleaned_data.get("video")
        if f:
            inspect_video(f)
        return f


class ProfileForm(forms.ModelForm):
    username = forms.CharField(
        label="Display name", max_length=150, required=False,
        widget=forms.TextInput(attrs={"autocomplete": "username"}))

    class Meta:
        model = Profile
        fields = ["photo", "category", "email", "phone"]
        labels = {"photo": "Profile photo (your unique identifier)",
                  "category": "What do you sell?",
                  "email": "Email address", "phone": "Phone number"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        if self.instance and self.instance.pk:
            self.fields["username"].initial = self.instance.user.username

    def clean_username(self):
        name = normalize_username((self.cleaned_data.get("username") or "").strip())
        if not name:
            return name
        qs = User.objects.filter(username__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.user_id)
        if qs.exists():
            raise forms.ValidationError("That username is already taken. Please try another one.")
        return name

    def clean_photo(self):
        f = self.cleaned_data.get("photo")
        if f:
            inspect_image(f)
        return f

    def clean(self):
        cd = super().clean()
        if not (cd.get("email") or "").strip() and not (cd.get("phone") or "").strip():
            raise forms.ValidationError("Keep at least an email address or a phone number on your account.")
        if cd.get("email"):
            cd["email"] = cd["email"].strip().lower()
        return cd

    def save(self, commit=True):
        profile = super().save(commit=commit)
        name = self.cleaned_data.get("username")
        if name:
            profile.user.username = normalize_username(name)
            if commit:
                profile.user.save(update_fields=["username"])
        return profile


class PostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ["title", "description", "price", "image", "video"]
        labels = {"title": "Product or service name",
                  "description": "Describe it",
                  "price": "Price (optional)",
                  "image": "Photo (taken with your phone)",
                  "video": "Video (optional, recorded with your phone)"}
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "price": forms.NumberInput(attrs={"step": "0.01"}),
        }

    media_verified = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["image"].widget.attrs["accept"] = "image/*"
        self.fields["video"].widget.attrs["accept"] = "video/*"
        self.fields["image"].required = False
        self.fields["video"].required = False

    def clean_image(self):
        f = self.cleaned_data.get("image")
        if f:
            self.media_verified = inspect_image(f)
        return f

    def clean_video(self):
        f = self.cleaned_data.get("video")
        if f:
            inspect_video(f)
        return f

    def clean(self):
        cd = super().clean()
        if not cd.get("image") and not cd.get("video"):
            raise forms.ValidationError("Add at least a photo or a short video of what you are selling.")
        return cd


class CircleMessageForm(forms.ModelForm):
    topic = forms.ChoiceField(choices=CIRCLE_TOPICS, label="What do you want to talk about?")

    class Meta:
        model = CircleMessage
        fields = ["topic", "text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 2, "placeholder": "Write a message to your circle…"})}

    def clean_text(self):
        text = self.cleaned_data["text"].strip()
        if not text:
            raise forms.ValidationError("Write a message first.")
        return text[:1000]


class ReviewForm(forms.Form):
    OPTIONS = [
        ("good_quality", "Good Quality"),
        ("poor_quality", "Poor Quality"),
        ("comfortable", "Comfortable to talk to"),
        ("reliable", "Very reliable"),
        ("fair_price", "Fair Price"),
        ("unfriendly", "Unfriendly"),
    ]
    good_quality = forms.BooleanField(required=False)
    poor_quality = forms.BooleanField(required=False)
    comfortable = forms.BooleanField(required=False)
    reliable = forms.BooleanField(required=False)
    fair_price = forms.BooleanField(required=False)
    unfriendly = forms.BooleanField(required=False)

    def clean(self):
        cd = super().clean()
        ticks = [k for k, _ in self.OPTIONS if cd.get(k)]
        if len(ticks) < 2:
            raise forms.ValidationError("Please tick at least two options.")
        return cd


class ReportForm(forms.Form):
    problem = forms.CharField(
        label="Tell us exactly what happened",
        widget=forms.Textarea(attrs={"rows": 4,
                                     "placeholder": "Describe how the seller treated you. Include dates and what was said."}))
    screenshot = forms.ImageField(
        required=False, label="Upload your screenshot here",
        widget=forms.FileInput(attrs={"accept": "image/*"}),
        help_text="A screenshot of the chat is the best evidence.")

    def clean_screenshot(self):
        f = self.cleaned_data.get("screenshot")
        if f:
            inspect_image(f)
        return f


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["name", "message"]
