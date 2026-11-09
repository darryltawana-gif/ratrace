from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import services as sv
from .models import (AccountGroup, AccountIdentity, Circle, CircleMember,
                     CircleMessage, Participant, Post, Profile, QualitySession,
                     Race, Report, Review)


def make_user(username, role="customer", email="", phone=""):
    user = User.objects.create_user(username=username, password="Str0ngPass!234")
    profile = sv.get_profile(user)
    profile.role = role
    profile.email = email
    profile.phone = phone
    profile.save()
    return user


class HomePageTests(TestCase):
    def setUp(self):
        self.customer = make_user("customer1", "customer")
        self.seller = make_user("seller1", "seller")

    def test_guest_home_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hall of Fame")
        self.assertContains(response, "My Circle")
        self.assertNotContains(response, "My Posts")
        self.assertContains(response, "#f2c200")
        self.assertNotContains(response, "#8ff0c4")

    def test_customer_home_renders(self):
        self.client.force_login(self.customer)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start a Race")
        self.assertNotContains(response, "Recent activities")

    def test_seller_home_shows_profile_then_posts(self):
        self.client.force_login(self.seller)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("seller1", body)
        self.assertIn("My Posts", body)
        self.assertIn("Recent activities", body)
        self.assertLess(body.index("seller-hero"), body.index(">🛍 My Posts</h2>"))
        self.assertLess(body.index(">🛍 My Posts</h2>"), body.index(">🕒 Recent activities</h2>"))
        self.assertLess(body.index(">🕒 Recent activities</h2>"), body.index(">🤝 My Circle</h3>"))


class NavigationTests(TestCase):
    def test_guest_nav_lists_public_links(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('class="nav-links"', body)
        self.assertIn('class="nav-actions"', body)
        for label in ["Comments", "Scam Doctor", "Login", "Sign up"]:
            self.assertIn(label, body)

    def test_seller_nav_lists_every_link_without_sideways_scroll(self):
        seller = make_user("navseller", "seller")
        self.client.force_login(seller)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('class="nav-links"', body)
        self.assertIn('class="nav-actions"', body)
        for label in ["My Circle", "My Posts", "Comments", "Scam Doctor", "Chats",
                      "My photo", "Add account", "Logout"]:
            self.assertIn(label, body)
        # The header must wrap, not hide buttons behind a sideways swipe.
        self.assertNotIn("nav{flex-wrap:nowrap", body)
        self.assertNotIn("nav>*{flex:0 0 auto}", body)


class AccountGroupTests(TestCase):
    def test_same_email_links_two_accounts(self):
        first = make_user("first", "customer", email="same@example.com")
        second = make_user("second", "seller", email="same@example.com")
        sv.link_account(first.profile, email="Same@Example.com")
        sv.link_account(second.profile, email="same@example.com")
        first.profile.refresh_from_db()
        second.profile.refresh_from_db()
        self.assertIsNotNone(first.profile.account_group_id)
        self.assertEqual(first.profile.account_group_id, second.profile.account_group_id)
        self.assertIn(second.profile, first.profile.other_accounts)
        self.assertEqual(AccountIdentity.objects.get(kind="email", value="same@example.com").group_id,
                         first.profile.account_group_id)

    def test_same_device_links_two_accounts(self):
        first = make_user("dev1")
        second = make_user("dev2")
        sv.link_account(first.profile, device="device-abc")
        sv.link_account(second.profile, device="device-abc")
        first.profile.refresh_from_db()
        second.profile.refresh_from_db()
        self.assertEqual(first.profile.account_group_id, second.profile.account_group_id)

    def test_different_people_are_not_grouped(self):
        first = make_user("solo1", email="one@example.com")
        second = make_user("solo2", email="two@example.com")
        sv.link_account(first.profile, email="one@example.com", device="device-1")
        sv.link_account(second.profile, email="two@example.com", device="device-2")
        first.profile.refresh_from_db()
        second.profile.refresh_from_db()
        self.assertNotEqual(first.profile.account_group_id, second.profile.account_group_id)

    def test_signup_with_shared_email_creates_group(self):
        for username in ("signup1", "signup2"):
            response = self.client.post("/signup/", {
                "username": username, "password": "Str0ngPass!234",
                "email": "family@example.com", "phone": "",
            })
            self.assertEqual(response.status_code, 302)
            self.client.logout()
        groups = AccountGroup.objects.filter(
            profiles__email="family@example.com").distinct()
        self.assertEqual(groups.count(), 1)
        self.assertEqual(groups.first().profiles.count(), 2)

    def test_common_password_is_rejected(self):
        response = self.client.post("/signup/", {
            "username": "weakuser", "password": "password123",
            "email": "weak@example.com", "phone": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too common")
        self.assertFalse(User.objects.filter(username="weakuser").exists())


class ReviewTests(TestCase):
    def setUp(self):
        self.customer = make_user("buyer", "customer")
        self.winner = make_user("winner", "seller")
        self.other = make_user("other", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Phone", budget=100)
        self.win_p = Participant.objects.create(
            race=self.race, seller=self.winner, slot=1, display_name="Winner",
            product_name="Phone", price=90, details="", image="products/x.jpg")
        self.other_p = Participant.objects.create(
            race=self.race, seller=self.other, slot=2, display_name="Other",
            product_name="Phone", price=95, details="", image="products/y.jpg")

    def test_only_winner_can_be_reviewed(self):
        self.client.force_login(self.customer)
        response = self.client.get(f"/race/{self.race.id}/review/{self.other.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)

    def test_review_after_winning(self):
        self.race.status = "won"
        self.race.winner = self.win_p
        self.race.save()
        self.client.force_login(self.customer)
        response = self.client.get(f"/race/{self.race.id}/review/{self.winner.id}/")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/race/{self.race.id}/review/{self.winner.id}/", {
            "good_quality": "on", "comfortable": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.filter(race=self.race, seller=self.winner).count(), 1)


class PrivacyTests(TestCase):
    def setUp(self):
        self.customer = make_user("locbuyer", "customer")
        self.seller = make_user("locseller", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Bag", budget=50,
                                        location="12 Private Street")

    def test_lobby_hides_location(self):
        payload = sv.lobby_race(self.race)
        self.assertNotIn("location", payload)

    def test_race_access(self):
        self.assertFalse(sv.race_access(self.seller, self.race.id))
        Participant.objects.create(race=self.race, seller=self.seller, slot=1,
                                   display_name="S", product_name="Bag", price=40,
                                   details="", image="products/z.jpg")
        self.assertTrue(sv.race_access(self.seller, self.race.id))
        self.assertTrue(sv.race_access(self.customer, self.race.id))
        stranger = make_user("stranger")
        self.assertFalse(sv.race_access(stranger, self.race.id))

    def test_report_screenshot_staff_only(self):
        staff = User.objects.create_user("staff", password="Str0ngPass!234", is_staff=True)
        report = Report.objects.create(seller=self.seller, reporter=self.customer, problem="bad")
        report.screenshot.save("proof.jpg", SimpleUploadedFile("proof.jpg", b"fake", content_type="image/jpeg"))
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(f"/reports/{report.id}/screenshot/").status_code, 403)
        self.client.force_login(staff)
        with override_settings(MEDIA_ROOT=report.screenshot.storage.location):
            response = self.client.get(f"/reports/{report.id}/screenshot/")
        self.assertEqual(response.status_code, 200)


class QualitySessionTests(TestCase):
    def setUp(self):
        self.customer = make_user("qbuyer", "customer")
        self.seller = make_user("qseller", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Shoes", budget=80)
        Participant.objects.create(race=self.race, seller=self.seller, slot=1,
                                   display_name="S", product_name="Shoes", price=70,
                                   details="", image="products/q.jpg")

    def test_one_window_per_seller_per_race(self):
        session = sv.start_quality_session(self.seller, self.race.id)
        self.assertIsNotNone(session)
        self.assertIsNotNone(sv.start_quality_session(self.seller, self.race.id))
        # Stopping the video pauses the proof; the seller may start again
        # inside the same 48-minute window.
        sv.end_quality_session(session.id)
        resumed = sv.start_quality_session(self.seller, self.race.id)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.id, session.id)
        self.assertIsNone(resumed.ended_at)
        self.assertEqual(QualitySession.objects.filter(race=self.race).count(), 1)

    def test_window_expires_on_server_clock(self):
        session = sv.start_quality_session(self.seller, self.race.id)
        QualitySession.objects.filter(pk=session.id).update(
            started_at=timezone.now() - timedelta(seconds=sv.QUALITY_SECONDS + 1))
        self.assertIsNone(sv.start_quality_session(self.seller, self.race.id))
        self.assertIsNone(sv.quality_for_race(self.race.id))

    def test_paused_proof_is_not_offered_to_customers(self):
        session = sv.start_quality_session(self.seller, self.race.id)
        self.assertTrue(sv.quality_payload(self.race.id)["active"])
        sv.end_quality_session(session.id)
        self.assertFalse(sv.quality_payload(self.race.id)["active"])
        # ...but the seller can bring it back inside the window.
        sv.start_quality_session(self.seller, self.race.id)
        payload = sv.quality_payload(self.race.id)
        self.assertTrue(payload["active"])
        self.assertEqual(payload["seller_id"], self.seller.id)

    def test_stale_connection_cannot_pause_a_resumed_proof(self):
        session = sv.start_quality_session(
            self.seller, self.race.id, connection_id="old")
        self.assertIsNotNone(session)
        resumed = sv.start_quality_session(
            self.seller, self.race.id, connection_id="new")
        self.assertEqual(resumed.id, session.id)
        # A late disconnect from the replaced socket must be ignored.
        self.assertEqual(sv.release_quality_session(session.id, "old"), 0)
        self.assertTrue(sv.quality_payload(self.race.id)["active"])
        # The socket that still owns the proof can pause it.
        self.assertEqual(sv.release_quality_session(session.id, "new"), 1)
        self.assertFalse(sv.quality_payload(self.race.id)["active"])
        self.assertEqual(
            QualitySession.objects.get(pk=session.id).connection_id, "")

    def test_quality_window_is_48_minutes(self):
        self.assertEqual(sv.QUALITY_SECONDS, 48 * 60)
        session = sv.start_quality_session(self.seller, self.race.id)
        self.assertEqual(int((session.ends_at - session.started_at).total_seconds()), 48 * 60)

    def test_only_participant_seller_can_stream(self):
        self.assertTrue(sv.can_stream_quality(self.seller, self.race.id))
        self.assertTrue(sv.can_stream_quality(self.customer, self.race.id))
        stranger = make_user("qstranger")
        self.assertFalse(sv.can_stream_quality(stranger, self.race.id))


class CircleTests(TestCase):
    def setUp(self):
        self.customer = make_user("circlebuyer", "customer")
        self.seller = make_user("circleseller", "seller")
        self.other = make_user("circleother", "seller")
        self.circle = sv.ensure_circle(self.customer)
        self.member = CircleMember.objects.create(
            circle=self.circle, seller=self.seller, status="accepted")

    def test_customer_can_remove_a_member(self):
        self.client.force_login(self.customer)
        response = self.client.post(f"/circle/remove/{self.member.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CircleMember.objects.filter(pk=self.member.id).exists())

    def test_seller_cannot_remove_a_member(self):
        self.client.force_login(self.seller)
        response = self.client.post(f"/circle/remove/{self.member.id}/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CircleMember.objects.filter(pk=self.member.id).exists())

    def test_customer_topic_persists_and_seller_cannot_change_it(self):
        first = sv.save_circle_message(self.customer, self.circle.id, "Hello all", "jokes")
        self.assertEqual(first["message"]["topic"], "jokes")
        self.circle.refresh_from_db()
        self.assertEqual(self.circle.current_topic, "jokes")

        # A seller trying to choose a topic is ignored.
        reply = sv.save_circle_message(self.seller, self.circle.id, "A joke for you", "products")
        self.circle.refresh_from_db()
        self.assertEqual(self.circle.current_topic, "jokes")
        self.assertEqual(reply["message"]["topic"], "jokes")
        self.assertIsNone(sv.set_circle_topic(self.seller, self.circle.id, "products"))

        # The customer can change the topic mid-conversation.
        changed = sv.save_circle_message(self.customer, self.circle.id, "New topic", "products")
        self.circle.refresh_from_db()
        self.assertEqual(self.circle.current_topic, "products")
        self.assertEqual(changed["topic_changed"], "products")

        later = sv.save_circle_message(self.seller, self.circle.id, "About my products", "")
        self.assertEqual(later["message"]["topic"], "products")

    def test_customer_and_seller_can_react_with_emoji(self):
        msg = CircleMessage.objects.create(
            circle=self.circle, sender=self.customer, text="Hello", topic="other")
        res = sv.react_to_circle_message(self.seller, self.circle.id, msg.id, "👍")
        self.assertEqual(res["message_id"], msg.id)
        self.assertEqual(res["reactions"], [
            {"emoji": "👍", "count": 1, "users": [self.seller.id]}])

        # Tapping the same emoji again removes the reaction.
        res = sv.react_to_circle_message(self.seller, self.circle.id, msg.id, "👍")
        self.assertEqual(res["reactions"], [])

        # The customer can react too, and one person may use several emojis.
        sv.react_to_circle_message(self.customer, self.circle.id, msg.id, "❤️")
        res = sv.react_to_circle_message(self.customer, self.circle.id, msg.id, "😂")
        self.assertEqual({r["emoji"] for r in res["reactions"]}, {"❤️", "😂"})
        self.assertTrue(all(r["count"] == 1 for r in res["reactions"]))

    def test_reactions_require_membership_and_known_emoji(self):
        msg = CircleMessage.objects.create(
            circle=self.circle, sender=self.customer, text="Hello", topic="other")
        outsider = make_user("circlestranger")
        self.assertIsNone(
            sv.react_to_circle_message(outsider, self.circle.id, msg.id, "👍"))
        self.assertIsNone(
            sv.react_to_circle_message(self.seller, self.circle.id, msg.id, "💩"))
        self.assertIsNone(
            sv.react_to_circle_message(self.seller, self.circle.id, 999999, "👍"))

        # A message from another Circle cannot be reached through this one.
        other_circle = sv.ensure_circle(outsider)
        other_msg = CircleMessage.objects.create(
            circle=other_circle, sender=outsider, text="Other", topic="other")
        self.assertIsNone(
            sv.react_to_circle_message(self.seller, self.circle.id, other_msg.id, "👍"))

    def test_history_includes_reactions(self):
        msg = CircleMessage.objects.create(
            circle=self.circle, sender=self.customer, text="Hello", topic="other")
        sv.react_to_circle_message(self.seller, self.circle.id, msg.id, "🙏")
        history = sv.circle_history(self.circle.id)
        self.assertEqual(history[-1]["reactions"][0]["emoji"], "🙏")

    def test_reply_quotes_the_original_message(self):
        original = sv.save_circle_message(
            self.customer, self.circle.id, "Which bag is best?")["message"]
        reply = sv.save_circle_message(
            self.seller, self.circle.id, "The leather one.",
            reply_to_id=original["id"])["message"]
        self.assertEqual(reply["reply_to"]["id"], original["id"])
        self.assertEqual(reply["reply_to"]["sender"], "circlebuyer")
        self.assertEqual(reply["reply_to"]["text"], "Which bag is best?")
        history = sv.circle_history(self.circle.id)
        self.assertEqual(history[-1]["reply_to"]["id"], original["id"])
        self.assertIsNone(history[0]["reply_to"])

    def test_reply_to_message_from_another_circle_is_ignored(self):
        other_circle = sv.ensure_circle(self.other)
        other_msg = CircleMessage.objects.create(
            circle=other_circle, sender=self.other, text="Not yours", topic="other")
        reply = sv.save_circle_message(
            self.seller, self.circle.id, "Sneaky quote",
            reply_to_id=other_msg.id)["message"]
        self.assertIsNone(reply["reply_to"])
        self.assertIsNone(CircleMessage.objects.get(pk=reply["id"]).reply_to_id)


class PostTests(TestCase):
    def setUp(self):
        self.seller = make_user("postseller", "seller")
        self.customer = make_user("postbuyer", "customer")

    def test_seller_can_post_without_media(self):
        self.client.force_login(self.seller)
        response = self.client.post("/posts/new/", {
            "title": "Handmade bag", "description": "New stock", "price": "25.00"})
        self.assertEqual(response.status_code, 302)
        post = Post.objects.get(title="Handmade bag")
        self.assertEqual(post.seller, self.seller)
        self.assertTrue(post.active)

    def test_seller_can_remove_own_post(self):
        post = Post.objects.create(seller=self.seller, title="Old stock")
        self.client.force_login(self.seller)
        response = self.client.post(f"/posts/{post.id}/delete/")
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertFalse(post.active)

    def test_seller_cannot_remove_someone_elses_post(self):
        post = Post.objects.create(seller=self.seller, title="Mine")
        self.client.force_login(self.customer)
        response = self.client.post(f"/posts/{post.id}/delete/")
        self.assertEqual(response.status_code, 404)
        post.refresh_from_db()
        self.assertTrue(post.active)
