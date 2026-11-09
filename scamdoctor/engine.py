"""Orchestration for Scam Doctor checks.

Rules always run. AI runs only when a key is configured and only for checks
the user explicitly asks for (never for automatic chat scanning). Images get
local metadata checks even without AI; videos get an honest "needs human
review" note when ffmpeg or AI is unavailable.
"""
from PIL import ExifTags, Image

from . import ai, rules

EDITING_APPS = (
    "photoshop", "snapseed", "picsart", "canva", "lightroom", "meitu", "remini",
    "facetune", "pixlr", "gimp", "affinity", "express", "inshot", "capcut",
    "snapchat", "paint", "pixlr", "vsco", "afterlight",
)
CAMERA_TAGS = ("Make", "Model", "DateTimeOriginal", "DateTimeDigitized")


def _image_metadata(file):
    """Local, explainable checks that do not need a cloud key."""
    flags = []
    note = ""
    file.seek(0)
    try:
        img = Image.open(file)
        img.load()
        fmt = (img.format or "").upper()
        width, height = img.size
        exif = img.getexif()
        software = ""
        has_camera = False
        for tag_id, value in exif.items():
            name = ExifTags.TAGS.get(tag_id, str(tag_id))
            text = str(value).strip()
            if name in CAMERA_TAGS and text:
                has_camera = True
            if name in ("Software", "ProcessingSoftware", "Artist",
                        "ImageDescription", "UserComment") and text:
                software += " " + text
        low = software.lower()

        if any(app in low for app in EDITING_APPS):
            flags.append({
                "code": "edited_image",
                "label": "Image carries photo-editing software",
                "severity": "high",
                "detail": ("Editing metadata found: "
                           + software.strip()[:160]
                           + ". Edited receipts and payment alerts are easy to fake."),
                "advice": "Do not trust a payment screenshot. Confirm the money in your own bank or mobile-money app.",
                "source": "rules",
            })
        elif fmt == "PNG" or (not has_camera and width >= 700 and height >= 700):
            flags.append({
                "code": "screenshot",
                "label": "Screenshot rather than a camera photo",
                "severity": "medium",
                "detail": ("This looks like a screenshot. Screenshots of payment alerts, "
                           "chats and receipts can be edited."),
                "advice": "Open your own bank or mobile-money app to confirm any payment shown in a screenshot.",
                "source": "rules",
            })
        elif not has_camera:
            flags.append({
                "code": "no_camera_info",
                "label": "No camera information",
                "severity": "low",
                "detail": "The image has no camera metadata, so its origin cannot be confirmed.",
                "advice": "Ask for a live Quality Proof video if the item or payment proof matters.",
                "source": "rules",
            })
    except Exception:
        note = "This image format could not be read on the server."
    finally:
        file.seek(0)
    return flags, note


def analyse_text(text, use_ai=True, context=""):
    result = rules.analyse_text(text)
    note = ""
    if use_ai:
        if ai.available():
            ai_result = ai.analyse_text(text, context)
            if ai_result:
                result = rules.merge([result, ai_result])
            else:
                note = "The AI check could not run, so the rules result is shown."
        else:
            note = ("AI text analysis is off until the safety team configures the "
                    "AI key. The rules check is still active.")
    result["ai_note"] = note[:255]
    return result


def _media_summary(result, kind):
    """A summary that talks about a file, not 'this text'."""
    risk = result.get("risk")
    if risk == "high":
        return f"This {kind} shows serious scam warning signs. Do not trust it or pay because of it."
    if risk == "medium":
        return (f"This {kind} shows several scam warning signs. Verify it through an "
                "official app before you act.")
    if risk == "low":
        return f"This {kind} has a few caution signs. Read the advice before you trust it."
    return (f"We did not find common scam signs in this {kind}. That is not a "
            "guarantee, so still trust your instincts.")


def analyse_image(file, text="", use_ai=True):
    results = [rules.finish(0, [], engine="rules")]
    if text and text.strip():
        results.append(rules.analyse_text(text))
    meta_flags, note = _image_metadata(file)
    results.append(rules.finish(rules.score_flags(meta_flags), meta_flags, engine="rules"))

    if use_ai:
        if ai.available():
            ai_result = ai.analyse_image(file, text)
            if ai_result:
                results.append(ai_result)
            else:
                note = (note + " The AI could not analyse this image; it is stored "
                        "for human review.").strip()
        else:
            note = (note + " AI visual analysis is off until the safety team "
                    "configures the key; the image is stored for human review.").strip()

    result = rules.merge(results)
    result["ai_note"] = note[:255]
    if not (text and text.strip()) and not result.get("ai_used"):
        result["summary"] = _media_summary(result, "image")
    return result


def analyse_video(file, text="", use_ai=True):
    results = []
    if text and text.strip():
        results.append(rules.analyse_text(text))
    note = ""
    if use_ai:
        if ai.available():
            ai_result = ai.analyse_video(file, text)
            if ai_result:
                results.append(ai_result)
            else:
                note = ("Video frame and audio analysis needs ffmpeg on the server. "
                        "The video is stored for human review.")
        else:
            note = ("AI video analysis is off until the safety team configures the "
                    "key. The video is stored for human review.")
    else:
        note = "The video is stored for human review."
    result = rules.merge(results) if results else rules.finish(0, [])
    result["ai_note"] = note[:255]
    if not (text and text.strip()) and not result.get("ai_used"):
        result["summary"] = _media_summary(result, "video")
    return result


def analyse(kind, file=None, text="", use_ai=True):
    """Run the right check for a scan kind and combine any accompanying text."""
    if kind == "image" and file is not None:
        return analyse_image(file, text=text, use_ai=use_ai)
    if kind == "video" and file is not None:
        return analyse_video(file, text=text, use_ai=use_ai)
    return analyse_text(text, use_ai=use_ai)
