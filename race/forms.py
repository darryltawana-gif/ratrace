from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from .models import Race, Participant, Profile, Comment


def normalize_username(value):
    """'John  Doe' -> 'John_Doe' so people can type names naturally."""
    return "_".join(value.split())


class SignupForm(forms.Form):
    username = forms.CharField(
        max_length=150, label="Username",
        widget=forms.TextInput(attrs={"placeholder": "Any name you like", "autocomplete": "username",
                                      "autocapitalize": "none"}))
    password = forms.CharField(
        min_length=4, label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "At least 4 characters", "autocomplete": "new-password"}))

    def clean_username(self):
        name = normalize_username(self.cleaned_data["username"])
        if User.objects.filter(username__iexact=name).exists():
            raise forms.ValidationError("That username is already taken. Please try another one.")
        return name

    def clean_username(self):
        name = normalize_username(self.cleaned_data["username"])
        if User.objects.filter(username__iexact=name).exists():
            raise forms.ValidationError("That username is already taken. Please try another one.")
        return name

    def clean(self):
        cd = super().clean()
        if cd.get("role") == "seller" and not cd.get("photo"):
            self.add_error("photo", "Sellers must add a profile photo. It identifies you in races.")
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
        fields = ["product", "budget"]
        labels = {"product": "What product do you want?", "budget": "Your budget"}

    def clean_budget(self):
        b = self.cleaned_data["budget"]
        if b <= 0:
            raise forms.ValidationError("Budget must be more than 0.")
        return b


class JoinForm(forms.ModelForm):
    profile_photo = forms.ImageField(label="Your profile photo (identifies you in races)", required=True,
                                     widget=forms.FileInput(attrs={"accept": "image/*"}))

    class Meta:
        model = Participant
        fields = ["display_name", "product_name", "price", "details", "image"]
        labels = {"display_name": "Name you use", "product_name": "Product name", "image": "Product picture"}

    def __init__(self, *a, needs_photo=False, **k):
        super().__init__(*a, **k)
        self.fields["image"].widget.attrs["accept"] = "image/*"
        if not needs_photo:
            del self.fields["profile_photo"]

    def clean_price(self):
        p = self.cleaned_data["price"]
        if p <= 0:
            raise forms.ValidationError("Price must be more than 0.")
        return p


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["photo"]
        labels = {"photo": "Profile photo (your unique identifier)"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["photo"].required = True
        self.fields["photo"].widget.attrs["accept"] = "image/*"

class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ["name", "message"]