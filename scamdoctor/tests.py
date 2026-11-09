import io
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from race import services as sv
from race.models import ChatRoom, Circle, CircleMember, CircleMessage, Race

from . import ai, engine, rules
from .models import Scan


def make_user(username, role="customer"):
    user = User.objects.create_user(username=username, password="Str0ngPass!234")
    profile = sv.get_profile(user)
    profile.role = role
    profile.save()
    return user


class RuleTests(TestCase):
    def test_fake_payment_message_is_high_risk(self):
        result = rules.analyse_text(
            "I have paid, check your momo and send the difference now.")
        self.assertEqual(result["risk"], "high")
        codes = {f["code"] for f in result["flags"]}
        self.assertIn("payment_claim", codes)
        self.assertIn("overpayment", codes)
        self.assertTrue(result["advice"])

    def test_normal_marketplace_message_is_not_high(self):
        result = rules.analyse_text(
            "Is the blue bag still available? I can pay on delivery.")
        self.assertNotIn(result["risk"], ("medium", "high"))
        self.assertFalse(any(f["severity"] == "high" for f in result["flags"]))

    def test_twi_payment_language_is_detected(self):
        result = rules.analyse_text(
            "Mepa wo kyew, tua sika no ntem na ma me baako.")
        codes = {f["code"] for f in result["flags"]}
        self.assertIn("advance_fee", codes)
        self.assertGreaterEqual(result["score"], 35)

    def test_shortened_link_and_phishing_are_flagged(self):
        result = rules.analyse_text(
            "You have won a prize. Click this link to verify your account: "
            "http://bit.ly/abc")
        codes = {f["code"] for f in result["flags"]}
        self.assertIn("phishing", codes)
        self.assertIn("link", codes)
        self.assertEqual(result["risk"], "high")

    def test_clean_message_has_no_flags(self):
        result = rules.analyse_text("Thank you, I will pick it up tomorrow morning.")
        self.assertEqual(result["risk"], "safe")
        self.assertEqual(result["flags"], [])


@override_settings(SCAM_DOCTOR_AI_API_KEY="")
class CheckerTests(TestCase):
    def setUp(self):
        self.customer = make_user("sd_customer", "customer")
        self.other = make_user("sd_other", "seller")

    def test_login_required(self):
        response = self.client.get(reverse("scamdoctor:checker"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_text_scan_is_saved_and_shown(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("scamdoctor:checker"), {
            "text": "I have paid, check your momo and send the difference.",
            "consent": "on",
        })
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.get(user=self.customer)
        self.assertEqual(scan.kind, "text")
        self.assertEqual(scan.risk, "high")
        self.assertTrue(scan.flags)
        self.assertTrue(scan.summary)
        page = self.client.get(reverse("scamdoctor:scan_detail", args=[scan.id]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "likely scam")

    def test_consent_is_required(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("scamdoctor:checker"), {
            "text": "I have paid.",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Scan.objects.filter(user=self.customer).exists())

    def test_scan_detail_is_private(self):
        scan = Scan.objects.create(user=self.customer, risk="low", score=10,
                                   summary="Test")
        self.client.force_login(self.other)
        response = self.client.get(reverse("scamdoctor:scan_detail", args=[scan.id]))
        self.assertEqual(response.status_code, 404)

    def test_ai_is_off_without_a_key_and_says_so(self):
        self.assertFalse(ai.available())
        result = engine.analyse_text("Hello there", use_ai=True)
        self.assertIn("AI text analysis is off", result["ai_note"])


UPLOAD_MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(SCAM_DOCTOR_AI_API_KEY="", MEDIA_ROOT=UPLOAD_MEDIA_ROOT)
class UploadTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(UPLOAD_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.customer = make_user("sd_uploader", "customer")

    def test_screenshot_upload_is_stored_and_checked(self):
        buf = io.BytesIO()
        Image.new("RGB", (800, 1200), (255, 255, 255)).save(buf, "PNG")
        upload = SimpleUploadedFile("payment.png", buf.getvalue(),
                                    content_type="image/png")
        self.client.force_login(self.customer)
        response = self.client.post(reverse("scamdoctor:checker"), {
            "image": upload, "consent": "on",
        })
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.get(user=self.customer)
        self.assertEqual(scan.kind, "image")
        self.assertTrue(scan.file)
        self.assertTrue(any(f["code"] == "screenshot" for f in scan.flags))
        # A medium finding must raise the stored score and risk, not stay "safe".
        self.assertGreaterEqual(scan.score, 10)
        self.assertIn(scan.risk, ("low", "medium", "high"))
        self.assertIn("image", scan.summary.lower())

    def test_video_upload_is_stored_for_review(self):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
        upload = SimpleUploadedFile("clip.mp4", data, content_type="video/mp4")
        self.client.force_login(self.customer)
        response = self.client.post(reverse("scamdoctor:checker"), {
            "video": upload, "consent": "on",
        })
        self.assertEqual(response.status_code, 302)
        scan = Scan.objects.get(user=self.customer)
        self.assertEqual(scan.kind, "video")
        self.assertTrue(scan.file)
        self.assertIn("human review", scan.ai_note)


@override_settings(SCAM_DOCTOR_AI_API_KEY="", SCAM_DOCTOR_AUTO_WARN_SCORE=35)
class ChatCheckTests(TestCase):
    def setUp(self):
        self.customer = make_user("sd_chat_customer", "customer")
        self.seller = make_user("sd_chat_seller", "seller")
        self.outsider = make_user("sd_chat_outsider", "customer")
        self.race = Race.objects.create(customer=self.customer, product="Bag",
                                        budget="100.00", status="open")
        self.room = ChatRoom.objects.create(race=self.race, customer=self.customer,
                                            seller=self.seller)
        self.circle = Circle.objects.create(customer=self.customer)
        CircleMember.objects.create(circle=self.circle, seller=self.seller,
                                    status="accepted")

    def test_private_chat_message_can_be_checked(self):
        saved = sv.save_message(self.seller, self.room.id, "Is it available?")
        message_id = saved["message"]["id"]
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse("scamdoctor:check_message"),
            data='{"source":"private_chat","message_id":%d}' % message_id,
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        scan = Scan.objects.get(pk=data["scan_id"])
        self.assertEqual(scan.user, self.customer)
        self.assertEqual(scan.other_user, self.seller)
        self.assertEqual(scan.source, "private_chat")
        self.assertEqual(scan.message_id, message_id)

    def test_outsider_cannot_check_a_private_message(self):
        saved = sv.save_message(self.seller, self.room.id, "Is it available?")
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("scamdoctor:check_message"),
            data='{"source":"private_chat","message_id":%d}' % saved["message"]["id"],
            content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_circle_member_can_check_a_message(self):
        saved = sv.save_circle_message(self.seller, self.circle.id, "Hello there", "")
        message_id = saved["message"]["id"]
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse("scamdoctor:check_message"),
            data='{"source":"circle_chat","message_id":%d}' % message_id,
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        scan = Scan.objects.get(pk=response.json()["scan_id"])
        self.assertEqual(scan.source, "circle_chat")
        self.assertEqual(scan.circle, self.circle)

    def test_outsider_cannot_check_a_circle_message(self):
        saved = sv.save_circle_message(self.seller, self.circle.id, "Hello there", "")
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("scamdoctor:check_message"),
            data='{"source":"circle_chat","message_id":%d}' % saved["message"]["id"],
            content_type="application/json")
        self.assertEqual(response.status_code, 403)


@override_settings(SCAM_DOCTOR_AI_API_KEY="", SCAM_DOCTOR_AUTO_WARN_SCORE=35)
class AutoWarningTests(TestCase):
    def setUp(self):
        self.customer = make_user("sd_auto_customer", "customer")
        self.seller = make_user("sd_auto_seller", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Phone",
                                        budget="500.00", status="open")
        self.room = ChatRoom.objects.create(race=self.race, customer=self.customer,
                                            seller=self.seller)
        self.circle = Circle.objects.create(customer=self.customer)
        CircleMember.objects.create(circle=self.circle, seller=self.seller,
                                    status="accepted")

    def test_private_scam_message_warns_the_recipient_only(self):
        saved = sv.save_message(
            self.seller, self.room.id,
            "I have paid, check your momo and send the difference now.")
        message_id = saved["message"]["id"]
        scan = Scan.objects.get(source="private_chat", message_id=message_id)
        self.assertEqual(scan.user, self.customer)
        self.assertEqual(scan.other_user, self.seller)
        self.assertTrue(scan.warning_sent)
        self.assertFalse(Scan.objects.filter(user=self.seller, message_id=message_id).exists())

    def test_safe_private_message_does_not_create_a_scan(self):
        saved = sv.save_message(self.seller, self.room.id,
                                "Thank you, I will pick it up tomorrow.")
        self.assertFalse(Scan.objects.filter(
            source="private_chat", message_id=saved["message"]["id"]).exists())

    def test_auto_check_never_calls_the_ai(self):
        with patch("scamdoctor.ai.analyse_text",
                   side_effect=AssertionError("AI must not run automatically")):
            saved = sv.save_message(
                self.seller, self.room.id,
                "I have paid, check your momo and send the difference now.")
        self.assertTrue(Scan.objects.filter(
            source="private_chat", message_id=saved["message"]["id"]).exists())

    def test_circle_scam_message_warns_other_members(self):
        saved = sv.save_circle_message(
            self.seller, self.circle.id,
            "I have paid, check your momo and send the difference now.", "")
        message_id = saved["message"]["id"]
        scan = Scan.objects.get(source="circle_chat", message_id=message_id,
                                user=self.customer)
        self.assertEqual(scan.other_user, self.seller)
        self.assertFalse(Scan.objects.filter(
            source="circle_chat", message_id=message_id, user=self.seller).exists())

    def test_risk_helpers(self):
        scan = Scan.objects.create(user=self.customer, risk="medium", score=40)
        self.assertEqual(scan.risk_rank, 2)
        self.assertTrue(scan.is_flagged)
