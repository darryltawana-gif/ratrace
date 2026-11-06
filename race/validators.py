"""Upload checks for the 'original media only' rule.

There is no reliable way to detect every AI-generated image from its bytes.
The practical, honest approach used here:

* Real phone photos carry EXIF camera metadata (make, model, date). When we
  find it we mark the upload as verified.
* AI tools either strip that metadata or leave tell-tale signatures
  (Midjourney, DALL-E, Stable Diffusion, Firefly, C2PA, ...). When we find
  such a signature we reject the file.
* A file with no camera metadata and no AI signature is allowed but marked
  unverified, so you can still review it in the admin.

This blocks casual AI uploads. A determined person can forge metadata; no
server-side check can prevent that completely.
"""
import io

from django.core.exceptions import ValidationError

MAX_IMAGE_BYTES = 12 * 1024 * 1024      # 12 MB
MAX_VIDEO_BYTES = 80 * 1024 * 1024      # 80 MB

AI_SIGNATURES = (
    b"midjourney", b"dall-e", b"dall\xc2\xb7e", b"stable diffusion", b"stablediffusion",
    b"adobe firefly", b"firefly", b"runway", b"sora", b"generative fill",
    b"ai generated", b"ai-generated", b"synthetic image", b"c2pa",
    b"comfyui", b"automatic1111", b"novelai", b"leonardo.ai",
)

EXIF_CAMERA_TAGS = ("Make", "Model", "DateTimeOriginal", "DateTimeDigitized")
EXIF_TEXT_TAGS = ("Software", "Artist", "ImageDescription", "UserComment", "ProcessingSoftware")


def _raw_has_ai_signature(f, limit=4 * 1024 * 1024):
    f.seek(0)
    chunk = f.read(limit)
    f.seek(0)
    low = chunk.lower()
    return any(sig in low for sig in AI_SIGNATURES)


def _image_info(f):
    """Return (verified, ai_signature, exif_software_text)."""
    from PIL import Image
    f.seek(0)
    try:
        img = Image.open(f)
        exif = img.getexif()
    except Exception:
        f.seek(0)
        return False, False, ""
    finally:
        f.seek(0)

    software = ""
    has_camera = False
    for tag_id, value in exif.items():
        name = None
        try:
            from PIL.ExifTags import TAGS
            name = TAGS.get(tag_id, str(tag_id))
        except Exception:
            name = str(tag_id)
        text = str(value).strip()
        if name in EXIF_CAMERA_TAGS and text:
            has_camera = True
        if name in EXIF_TEXT_TAGS and text:
            software += " " + text
        # Some tools stash JSON in MakerNote / XPComment.
        if name in ("MakerNote", "XPComment", "XPKeywords") and text:
            software += " " + text

    low = software.lower()
    ai = any(sig.decode("utf-8", "ignore").lower() in low for sig in AI_SIGNATURES)
    return has_camera, ai, software


def inspect_image(f):
    """Validate an uploaded image. Returns True when camera-verified."""
    if f.size > MAX_IMAGE_BYTES:
        raise ValidationError("That photo is too large. Please use one under 12 MB.")
    f.seek(0)
    if _raw_has_ai_signature(f):
        raise ValidationError(
            "This looks like an AI-generated image. Please upload an original photo taken with your phone camera.")
    verified, ai, software = _image_info(f)
    if ai:
        raise ValidationError(
            "This image carries AI-tool metadata. Please upload an original photo taken with your phone camera.")
    return bool(verified)


def inspect_video(f):
    """Validate an uploaded video. Returns True when it looks camera-original."""
    if f.size > MAX_VIDEO_BYTES:
        raise ValidationError("That video is too large. Please keep it under 80 MB.")
    f.seek(0)
    head = f.read(2 * 1024 * 1024)
    f.seek(0)
    low = head.lower()
    if any(sig in low for sig in AI_SIGNATURES):
        raise ValidationError(
            "This looks like an AI-generated video. Please upload a real video recorded with your phone.")
    return False   # video metadata is not reliable enough to mark as verified
