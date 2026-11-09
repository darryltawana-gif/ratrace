"""Optional cloud AI for Scam Doctor.

The rules engine works with no internet and no API key. When
``SCAM_DOCTOR_AI_API_KEY`` is configured, this module adds a vision/text model
on top. It speaks the OpenAI-compatible chat-completions protocol, so it can
point at OpenAI, Azure-compatible gateways, OpenRouter, Groq or a self-hosted
gateway by changing ``SCAM_DOCTOR_AI_BASE_URL``.

Nothing here is called automatically on every chat message. Automatic checks
use rules only, so a slow or paid API can never hold up a conversation.
"""
import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

import requests
from django.conf import settings

from .rules import WEIGHTS, finish, merge

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Scam Doctor, a safety assistant for a Ghanaian online marketplace. "
    "Detect scams: fake payment screenshots, advance-fee or overpayment scams, "
    "phishing and impersonation, romance scams, fake jobs or investment offers, "
    "and fake delivery or courier fees. The text may be English, Twi or Ghanaian "
    "Pidgin. Return ONLY a JSON object with these keys: "
    "score (integer 0-100), risk (safe|low|medium|high), summary (one short "
    "sentence), advice (array of short strings), and flags (array of objects with "
    "label, severity (low|medium|high) and detail). Describe warning signs; do not "
    "accuse a named person of a crime. A normal marketplace message may mention "
    "delivery or payment, so do not mark it high without several signs."
)


def available():
    return bool(getattr(settings, "SCAM_DOCTOR_AI_API_KEY", ""))


def _base_url():
    return getattr(settings, "SCAM_DOCTOR_AI_BASE_URL",
                   "https://api.openai.com/v1").rstrip("/")


def _headers():
    return {
        "Authorization": "Bearer " + getattr(settings, "SCAM_DOCTOR_AI_API_KEY", ""),
        "Content-Type": "application/json",
    }


def _model(vision=False):
    if vision:
        return (getattr(settings, "SCAM_DOCTOR_AI_VISION_MODEL", "")
                or getattr(settings, "SCAM_DOCTOR_AI_MODEL", "gpt-4o-mini"))
    return getattr(settings, "SCAM_DOCTOR_AI_MODEL", "gpt-4o-mini")


def _extract_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def _post(path, payload, timeout):
    url = _base_url() + path
    response = requests.post(url, headers=_headers(), json=payload, timeout=timeout)
    if response.status_code >= 400:
        log.warning("Scam Doctor AI error %s: %s", response.status_code,
                    response.text[:300])
        return None
    try:
        return response.json()
    except ValueError:
        log.warning("Scam Doctor AI returned non-JSON")
        return None


def _chat(messages, timeout=25, vision=False):
    data = _post("/chat/completions", {
        "model": _model(vision=vision),
        "messages": messages,
        "temperature": 0.1,
    }, timeout)
    if not data:
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("Scam Doctor AI response had no message")
        return None
    return _normalise(_extract_json(content))


def _normalise(payload):
    if not isinstance(payload, dict):
        return None
    try:
        score = int(float(payload.get("score", 0)))
    except (TypeError, ValueError):
        score = 0

    flags = []
    for item in payload.get("flags") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "medium")).lower()
        if severity not in ("low", "medium", "high"):
            severity = "medium"
        label = str(item.get("label") or "Warning sign").strip()[:120]
        code = "ai_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
        flags.append({
            "code": code or "ai_flag",
            "label": label,
            "severity": severity,
            "detail": str(item.get("detail") or "")[:400],
            "advice": str(item.get("advice") or "")[:300],
            "source": "ai",
        })

    # A high finding must never be hidden by a low model score.
    score = max(score, sum(WEIGHTS.get(f["severity"], 0) for f in flags))
    risk = str(payload.get("risk", "")).lower()
    if risk not in ("safe", "low", "medium", "high"):
        risk = ""
    if risk == "high":
        score = max(score, 70)
    elif risk == "medium":
        score = max(score, 35)
    elif risk == "low":
        score = max(score, 10)

    result = finish(score, flags, engine="rules+ai", ai_used=True)
    summary = str(payload.get("summary") or "").strip()
    if summary:
        result["summary"] = summary[:500]

    advice = payload.get("advice")
    if isinstance(advice, str):
        advice = [advice]
    if isinstance(advice, list):
        extra = [str(a).strip()[:300] for a in advice if str(a).strip()]
        result["advice"] = list(dict.fromkeys(extra + result["advice"]))
    return result


def analyse_text(text, context=""):
    """Ask the text model for a second opinion. Returns None on any failure."""
    if not available() or not (text or "").strip():
        return None
    prompt = (
        "Check this message for scam warning signs.\n\n"
        f'"""\n{text[:8000]}\n"""\n\n'
        f"Context: {context or 'A user pasted this into Scam Doctor.'}"
    )
    try:
        return _chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], timeout=25)
    except requests.RequestException as exc:
        log.warning("Scam Doctor AI text check failed: %s", exc)
        return None


def _image_data_url(file):
    """Downscale where possible, then return a data URL. Resets the file."""
    from PIL import Image

    file.seek(0)
    try:
        img = Image.open(file)
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
        mime = "image/jpeg"
    except Exception:
        file.seek(0)
        data = file.read()
        mime = _guess_image_mime(file.name)
    finally:
        file.seek(0)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _guess_image_mime(name):
    low = (name or "").lower()
    if low.endswith((".png",)):
        return "image/png"
    if low.endswith((".webp",)):
        return "image/webp"
    if low.endswith((".gif",)):
        return "image/gif"
    if low.endswith((".heic", ".heif")):
        return "image/heic"
    return "image/jpeg"


def _analyse_image_url(data_url, text=""):
    prompt = (
        "Read all visible text in this image, including screenshots of chats, "
        "receipts, bank alerts and mobile-money confirmations. Look for scam "
        "signs: fake payment proof, edited numbers, wrong fonts, missing details, "
        "phishing instructions, impersonation, or pressure to pay. "
        + (f"The user also wrote: {text[:1000]}" if text else "")
    )
    try:
        return _chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ], timeout=60, vision=True)
    except requests.RequestException as exc:
        log.warning("Scam Doctor AI image check failed: %s", exc)
        return None


def analyse_image(file, text=""):
    if not available():
        return None
    try:
        return _analyse_image_url(_image_data_url(file), text)
    except Exception:
        log.exception("Scam Doctor image preparation failed")
        return None


def _transcribe(path):
    if not available():
        return ""
    model = getattr(settings, "SCAM_DOCTOR_AI_TRANSCRIBE_MODEL", "whisper-1")
    try:
        with open(path, "rb") as fh:
            response = requests.post(
                _base_url() + "/audio/transcriptions",
                headers={"Authorization": "Bearer " + getattr(settings, "SCAM_DOCTOR_AI_API_KEY", "")},
                data={"model": model},
                files={"file": ("audio.mp3", fh, "audio/mpeg")},
                timeout=90,
            )
        if response.status_code >= 400:
            log.warning("Scam Doctor transcription error %s: %s",
                        response.status_code, response.text[:200])
            return ""
        return (response.json() or {}).get("text", "")[:8000]
    except (requests.RequestException, ValueError) as exc:
        log.warning("Scam Doctor transcription failed: %s", exc)
        return ""


def _run(cmd, timeout=45):
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def analyse_video(file, text=""):
    """Extract frames and audio with ffmpeg, then analyse both.

    Video cannot be sent to a chat-completions model directly. If ffmpeg is not
    installed the caller shows an honest "needs human review" note instead of
    pretending the video was analysed.
    """
    if not available():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    suffix = os.path.splitext(getattr(file, "name", "") or "")[1][:8] or ".mp4"
    results = []
    with tempfile.TemporaryDirectory(prefix="scamdoctor_") as tmp:
        src = os.path.join(tmp, "input" + suffix)
        file.seek(0)
        with open(src, "wb") as out:
            for chunk in file.chunks():
                out.write(chunk)
        file.seek(0)

        audio = os.path.join(tmp, "audio.mp3")
        if _run([ffmpeg, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", audio]):
            transcript = _transcribe(audio)
            if transcript:
                results.append(analyse_text(
                    transcript, context="This is the spoken audio from a video."))

        for index, seconds in enumerate((0, 2, 5, 10)):
            frame = os.path.join(tmp, f"frame{index}.jpg")
            if not _run([ffmpeg, "-y", "-ss", str(seconds), "-i", src,
                         "-frames:v", "1", "-vf", "scale=1280:-1", frame]):
                continue
            if os.path.exists(frame) and os.path.getsize(frame):
                with open(frame, "rb") as fh:
                    data = fh.read()
                url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                results.append(_analyse_image_url(url, text))
    return merge(results) if results else None
