from django import forms
from django.core.exceptions import ValidationError

from race.validators import (MAX_IMAGE_BYTES, MAX_VIDEO_BYTES,
                             _looks_like_image, _video_container)


class ScanForm(forms.Form):
    text = forms.CharField(
        required=False, max_length=8000,
        label="Paste the message, or describe what happened",
        widget=forms.Textarea(attrs={
            "rows": 6,
            "placeholder": "Example: “I have paid, check your momo and send the difference.”",
        }))
    image = forms.FileField(
        required=False, label="Or upload a screenshot / photo",
        widget=forms.FileInput(attrs={"accept": "image/*"}))
    video = forms.FileField(
        required=False, label="Or upload a video",
        widget=forms.FileInput(attrs={"accept": "video/*"}))
    consent = forms.BooleanField(
        required=True,
        label="I understand this scan is stored so the safety team can review it.")

    def clean_image(self):
        f = self.cleaned_data.get("image")
        if not f:
            return f
        if f.size > MAX_IMAGE_BYTES:
            raise ValidationError("That image is too large. Please use one under 12 MB.")
        f.seek(0)
        head = f.read(32)
        f.seek(0)
        if not _looks_like_image(head):
            raise ValidationError("That file is not an image. Please upload a screenshot or photo.")
        return f

    def clean_video(self):
        f = self.cleaned_data.get("video")
        if not f:
            return f
        if f.size > MAX_VIDEO_BYTES:
            raise ValidationError("That video is too large. Please keep it under 80 MB.")
        f.seek(0)
        head = f.read(2 * 1024 * 1024)
        f.seek(0)
        if not _video_container(head):
            raise ValidationError("That file is not a video. Please upload a phone recording.")
        return f

    def clean(self):
        cleaned = super().clean()
        text = (cleaned.get("text") or "").strip()
        image = cleaned.get("image")
        video = cleaned.get("video")
        if not text and not image and not video:
            raise ValidationError("Paste a message or upload a photo/video to check.")
        if image and video:
            raise ValidationError("Please upload either a photo or a video, not both.")
        cleaned["text"] = text
        return cleaned

    @property
    def kind(self):
        if self.cleaned_data.get("image"):
            return "image"
        if self.cleaned_data.get("video"):
            return "video"
        return "text"

    @property
    def upload(self):
        return self.cleaned_data.get("image") or self.cleaned_data.get("video")
