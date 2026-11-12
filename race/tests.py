from datetime import timedelta
from io import BytesIO

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from . import services as sv
from .consumers import RaceConsumer
from .models import (AccountGroup, AccountIdentity, ChatRoom, Circle, CircleMember,
                     CircleMessage, Participant, Post, Profile, QualitySession,
                     Race, RaceItem, Report, Review)


def make_user(username, role="customer", email="", phone=""):
    user = User.objects.create_user(username=username, password="Str0ngPass!234")
    profile = sv.get_profile(user)
    profile.role = role
    profile.email = email
    profile.phone = phone
    profile.save()
    return user


def jpeg_upload(name="photo.jpg"):
    buf = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


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

    def test_customer_home_links_back_to_active_race(self):
        race = Race.objects.create(customer=self.customer, product="Sneakers", budget=100)
        self.client.force_login(self.customer)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back to Race")
        self.assertContains(response, f'href="/race/{race.id}/"')
        self.assertNotContains(response, "Start a Race")

    def test_customer_home_links_back_to_reopenable_race(self):
        race = Race.objects.create(customer=self.customer, product="Sneakers", budget=100, status="closed")
        self.client.force_login(self.customer)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back to Race")
        self.assertContains(response, f'action="/race/{race.id}/reopen/"')
        self.assertNotContains(response, "Start a Race")

    def test_customer_home_offers_new_race_when_last_one_expired(self):
        Race.objects.create(
            customer=self.customer, product="Sneakers", budget=100, status="closed",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.client.force_login(self.customer)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start a Race")
        self.assertNotContains(response, "Back to Race")

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


class ReviewDisplayTests(TestCase):
    """Review counts appear as icon chips everywhere sellers are shown."""

    def setUp(self):
        self.customer = make_user("displaybuyer", "customer")
        self.seller = make_user("displayseller", "seller")
        self.profile = sv.get_profile(self.seller)
        for i in range(3):
            race = Race.objects.create(customer=self.customer, product=f"Item {i}", budget=100)
            Review.objects.create(race=race, customer=self.customer, seller=self.seller,
                                  good_quality=True, comfortable=i < 2)
        race = Race.objects.create(customer=self.customer, product="Last", budget=100)
        Review.objects.create(race=race, customer=self.customer, seller=self.seller,
                              poor_quality=True, unfriendly=True)
        self.race = race
        self.participant = Participant.objects.create(
            race=race, seller=self.seller, slot=1, display_name="Display Seller",
            product_name="Last", price=90, details="", image="products/x.jpg")

    def test_badges_are_sorted_positive_first_then_by_count(self):
        badges = self.profile.review_badges
        self.assertEqual([b["key"] for b in badges],
                         ["good", "comfortable", "poor", "unfriendly"])
        self.assertEqual(badges[0]["count"], 3)
        self.assertEqual(badges[1]["count"], 2)

    def test_home_seller_card_shows_counts_with_icons(self):
        response = self.client.get("/")
        self.assertContains(response, "displayseller")
        self.assertContains(response, "Good Quality")
        self.assertContains(response, "×3")
        self.assertContains(response, "review-badge")

    def test_seller_profile_shows_all_counts_and_per_review_icons(self):
        response = self.client.get(f"/seller/{self.seller.id}/")
        self.assertContains(response, "What customers say")
        self.assertContains(response, "×3")
        self.assertContains(response, "×2")
        self.assertContains(response, "review-badge pos")
        self.assertContains(response, "review-badge neg")

    def test_race_state_embeds_badges_for_seller_lane(self):
        state = sv.race_state(self.race.id)
        participant = state["participants"][0]
        self.assertEqual(participant["review_badges"][0]["key"], "good")
        self.assertIn("Good Quality", participant["review_badges_html"])
        self.assertIn("×3", participant["review_badges_html"])

    def test_chat_screens_show_seller_badges(self):
        room = ChatRoom.objects.create(race=self.race, customer=self.customer, seller=self.seller)
        self.client.force_login(self.customer)
        response = self.client.get("/chats/")
        self.assertContains(response, "Good Quality")
        response = self.client.get(f"/chat/{room.id}/")
        self.assertContains(response, "Good Quality")


class PresenceTests(TestCase):
    """Sellers can tell whether the customer saw or is viewing their offer."""

    def setUp(self):
        self.customer = make_user("presencebuyer", "customer")
        self.seller = make_user("presenceseller", "seller")
        self.other = make_user("otherpresence", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Phone", budget=100)
        self.mine = Participant.objects.create(
            race=self.race, seller=self.seller, slot=1, display_name="Presence Seller",
            product_name="Phone", price=90, details="", image="products/x.jpg")
        self.theirs = Participant.objects.create(
            race=self.race, seller=self.other, slot=2, display_name="Other Seller",
            product_name="Phone", price=95, details="", image="products/y.jpg")

    def test_customer_presence_marks_viewed_and_viewing(self):
        self.assertTrue(sv.update_customer_presence(
            self.customer, self.race.id, [self.mine.id]))
        self.mine.refresh_from_db()
        self.assertIsNotNone(self.mine.customer_viewed_at)
        self.assertIsNotNone(self.mine.customer_viewing_at)

        payload = sv.presence_payload(self.race.id)
        self.assertIn(self.mine.id, payload["viewing"])
        self.assertIn(self.mine.id, payload["viewed"])
        self.assertNotIn(self.theirs.id, payload["viewing"])

        state = sv.race_state(self.race.id)
        row = next(x for x in state["participants"] if x["id"] == self.mine.id)
        self.assertTrue(row["customer_viewed"])
        self.assertTrue(row["customer_viewing"])

    def test_scrolling_away_clears_viewing_but_keeps_viewed(self):
        sv.update_customer_presence(self.customer, self.race.id, [self.mine.id])
        sv.update_customer_presence(self.customer, self.race.id, [])
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.customer_viewing_at)
        self.assertIsNotNone(self.mine.customer_viewed_at)
        payload = sv.presence_payload(self.race.id)
        self.assertNotIn(self.mine.id, payload["viewing"])
        self.assertIn(self.mine.id, payload["viewed"])

    def test_viewing_expires_after_ttl(self):
        sv.update_customer_presence(self.customer, self.race.id, [self.mine.id])
        Participant.objects.filter(pk=self.mine.pk).update(
            customer_viewing_at=timezone.now() - timedelta(seconds=sv.PRESENCE_TTL + 1))
        payload = sv.presence_payload(self.race.id)
        self.assertNotIn(self.mine.id, payload["viewing"])
        self.assertIn(self.mine.id, payload["viewed"])

    def test_only_the_race_customer_can_report_presence(self):
        self.assertFalse(sv.update_customer_presence(
            self.seller, self.race.id, [self.mine.id]))
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.customer_viewing_at)

    def test_disconnect_clears_viewing(self):
        sv.update_customer_presence(self.customer, self.race.id, [self.mine.id])
        self.assertTrue(sv.clear_customer_presence(self.customer, self.race.id))
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.customer_viewing_at)
        self.assertIsNotNone(self.mine.customer_viewed_at)

    def test_other_seller_profile_is_reachable(self):
        self.client.force_login(self.seller)
        response = self.client.get(f"/seller/{self.other.id}/")
        self.assertEqual(response.status_code, 200)


class PresenceSocketTests(TransactionTestCase):
    """The customer's race socket records presence and broadcasts it."""

    def setUp(self):
        self.customer = make_user("socketbuyer", "customer")
        self.seller = make_user("socketseller", "seller")
        self.race = Race.objects.create(
            customer=self.customer, product="Phone", budget=100,
            expires_at=timezone.now() + timedelta(minutes=20))
        self.mine = Participant.objects.create(
            race=self.race, seller=self.seller, slot=1, display_name="Socket Seller",
            product_name="Phone", price=90, details="", image="products/x.jpg")

    def test_viewing_action_updates_presence_and_broadcasts(self):
        async def scenario():
            comm = WebsocketCommunicator(
                RaceConsumer.as_asgi(), f"/ws/race/{self.race.id}/")
            comm.scope["user"] = self.customer
            comm.scope["url_route"] = {"kwargs": {"race_id": str(self.race.id)}}
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            state_msg = await comm.receive_json_from()
            quality_msg = await comm.receive_json_from()
            self.assertEqual(state_msg["type"], "state")
            self.assertEqual(quality_msg["type"], "quality_state")
            await comm.send_json_to({"action": "viewing", "ids": [self.mine.id]})
            presence_msg = await comm.receive_json_from()
            self.assertEqual(presence_msg["type"], "presence")
            self.assertIn(self.mine.id, presence_msg["viewing"])
            self.assertIn(self.mine.id, presence_msg["viewed"])
            await comm.disconnect()

        async_to_sync(scenario)()
        self.mine.refresh_from_db()
        self.assertIsNotNone(self.mine.customer_viewed_at)
        self.assertIsNone(self.mine.customer_viewing_at)


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


class JoinRaceTests(TestCase):
    """A seller must enter the race immediately without a profile-photo detour."""

    def setUp(self):
        self.customer = make_user("joincustomer", "customer")
        self.seller = make_user("joinseller", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Bag", budget=100)

    def test_seller_can_join_without_profile_photo(self):
        self.client.force_login(self.seller)
        response = self.client.post(f"/race/{self.race.id}/", {
            "price": "50.00",
            "details": "Handmade leather bag",
            "image": jpeg_upload("bag.jpg"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Participant.objects.filter(race=self.race, seller=self.seller, active=True).exists()
        )

    def test_seller_can_still_add_profile_photo_when_joining(self):
        self.client.force_login(self.seller)
        response = self.client.post(f"/race/{self.race.id}/", {
            "price": "50.00",
            "details": "Handmade leather bag",
            "image": jpeg_upload("bag.jpg"),
            "profile_photo": jpeg_upload("seller.jpg"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(sv.get_profile(self.seller).photo)

    def test_join_form_does_not_require_profile_photo(self):
        self.client.force_login(self.seller)
        response = self.client.get(f"/race/{self.race.id}/")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertFalse(form.fields["profile_photo"].required)


class MultiProductRaceTests(TestCase):
    """A customer posts several products in one race; sellers answer each one."""

    def setUp(self):
        self.customer = make_user("multibuyer", "customer")
        self.seller = make_user("multiseller", "seller")

    def test_customer_can_start_one_race_with_many_products(self):
        self.client.force_login(self.customer)
        response = self.client.post("/start/", {
            "location": "Avondale, Harare",
            "latitude": "-17.800000",
            "longitude": "31.040000",
            "items-TOTAL_FORMS": "2",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "1",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-product": "Leather handbag",
            "items-0-budget": "120.00",
            "items-0-quantity": "1",
            "items-0-details": "Black, medium",
            "items-1-product": "Running shoes",
            "items-1-budget": "80.00",
            "items-1-quantity": "2",
            "items-1-details": "Size 42",
        })
        self.assertEqual(response.status_code, 302)
        race = Race.objects.get(customer=self.customer)
        self.assertEqual(race.items.count(), 2)
        self.assertEqual(race.product, "Leather handbag")
        self.assertEqual(str(race.budget), "120.00")
        self.assertEqual(race.customer_lat, -17.8)
        self.assertEqual(race.items.last().product, "Running shoes")

    def test_customer_start_requires_at_least_one_product(self):
        self.client.force_login(self.customer)
        response = self.client.post("/start/", {
            "location": "Avondale, Harare",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "1",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-product": "",
            "items-0-budget": "",
            "items-0-quantity": "1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least one product or service you want.")
        self.assertFalse(Race.objects.filter(customer=self.customer).exists())

    def test_seller_can_enter_the_same_race_for_two_products(self):
        race = Race.objects.create(customer=self.customer, product="Handbag", budget=100)
        bag = RaceItem.objects.create(race=race, position=1, product="Handbag", budget=100)
        shoes = RaceItem.objects.create(race=race, position=2, product="Shoes", budget=60)
        self.client.force_login(self.seller)
        first = self.client.post(f"/race/{race.id}/", {
            "item": str(bag.id), "price": "80.00", "details": "Bag offer",
            "image": jpeg_upload("bag.jpg"),
        })
        self.assertEqual(first.status_code, 302)
        second = self.client.post(f"/race/{race.id}/", {
            "item": str(shoes.id), "price": "45.00", "details": "Shoe offer",
            "image": jpeg_upload("shoes.jpg"),
        })
        self.assertEqual(second.status_code, 302)
        offers = Participant.objects.filter(race=race, seller=self.seller, active=True)
        self.assertEqual(offers.count(), 2)
        self.assertEqual(set(offers.values_list("item_id", flat=True)), {bag.id, shoes.id})
        # Both offers are clearly labelled with the product they answer.
        state = sv.race_state(race.id)
        labels = {p["item_product"] for p in state["participants"]}
        self.assertEqual(labels, {"Handbag", "Shoes"})


class LiveLocationTests(TestCase):
    """Coordinates, distance and shop details travel with the race state."""

    def setUp(self):
        self.customer = make_user("mapbuyer", "customer")
        self.seller = make_user("mapseller", "seller")
        profile = sv.get_profile(self.seller)
        profile.shop_name = "Maple Shop"
        profile.shop_location = "24 Samora Machel Ave, Harare"
        profile.shop_lat = -17.8292
        profile.shop_lng = 31.0522
        profile.save()
        self.race = Race.objects.create(customer=self.customer, product="Lamp", budget=40)
        self.item = RaceItem.objects.create(race=self.race, position=1, product="Lamp", budget=40)
        self.offer = Participant.objects.create(
            race=self.race, seller=self.seller, item=self.item, slot=1,
            display_name="Map Seller", product_name="Lamp", price=35,
            details="", image="products/lamp.jpg")

    def test_customer_location_is_stored_and_broadcast(self):
        payload = sv.update_location(self.customer, self.race.id, -17.8252, 31.0335)
        self.race.refresh_from_db()
        self.assertAlmostEqual(self.race.customer_lat, -17.8252)
        self.assertIsNotNone(payload)
        self.assertAlmostEqual(payload["customer"]["lat"], -17.8252)
        # The seller's shop pin becomes the fallback position on the map.
        seller = payload["sellers"][0]
        self.assertAlmostEqual(seller["lat"], -17.8292)
        self.assertEqual(seller["shop_name"], "Maple Shop")
        self.assertIsNotNone(seller["distance_m"])

    def test_seller_live_location_follows_them_between_races(self):
        sv.update_location(self.seller, self.race.id, -17.8100, 31.0600)
        profile = sv.get_profile(self.seller)
        self.assertAlmostEqual(profile.latitude, -17.81)
        state = sv.race_state(self.race.id)
        lane = state["participants"][0]
        self.assertEqual(lane["location_source"], "live")
        self.assertAlmostEqual(lane["lat"], -17.81)

    def test_only_race_members_may_report_a_location(self):
        stranger = make_user("mapstranger", "seller")
        self.assertIsNone(sv.update_location(stranger, self.race.id, -17.8, 31.0))
        self.assertIsNone(sv.update_location(self.seller, self.race.id, "not-a-number", "x"))

    def test_race_room_shows_map_items_distance_and_shop(self):
        sv.update_location(self.customer, self.race.id, -17.8252, 31.0335)
        self.client.force_login(self.customer)
        response = self.client.get(f"/race/{self.race.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Live map", body)
        self.assertIn("Lamp", body)
        self.assertIn("Directions", body)
        self.assertIn("away", body)
        self.assertIn("Car", body)
        self.assertIn("Weather here", body)
        # The shop details are sent in the live state payload.
        state = sv.race_state(self.race.id)
        self.assertEqual(state["participants"][0]["shop_name"], "Maple Shop")
        self.assertIsNotNone(state["participants"][0]["distance_m"])


class ShopProfileTests(TestCase):
    def setUp(self):
        self.seller = make_user("shopseller", "seller", email="shop@example.com")

    def test_seller_must_add_a_shop_location(self):
        self.client.force_login(self.seller)
        response = self.client.post("/profile/", {
            "username": "shopseller", "email": "shop@example.com", "phone": "",
            "category": "gadgets", "shop_name": "Optional Name",
            "shop_location": "", "shop_lat": "", "shop_lng": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add your shop or business location")
        self.assertFalse(sv.get_profile(self.seller).shop_location)

    def test_seller_can_save_optional_shop_name_and_required_location(self):
        self.client.force_login(self.seller)
        response = self.client.post("/profile/", {
            "username": "shopseller", "email": "shop@example.com", "phone": "",
            "category": "gadgets", "shop_name": "Maple Shop",
            "shop_location": "24 Samora Machel Ave, Harare",
            "shop_lat": "-17.8292", "shop_lng": "31.0522",
        })
        self.assertEqual(response.status_code, 302)
        profile = sv.get_profile(self.seller)
        self.assertEqual(profile.shop_name, "Maple Shop")
        self.assertAlmostEqual(profile.shop_lat, -17.8292)

    def test_public_seller_page_shows_shop_and_directions(self):
        profile = sv.get_profile(self.seller)
        profile.shop_name = "Maple Shop"
        profile.shop_location = "24 Samora Machel Ave, Harare"
        profile.shop_lat = -17.8292
        profile.shop_lng = 31.0522
        profile.save()
        response = self.client.get(f"/seller/{self.seller.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maple Shop")
        self.assertContains(response, "Directions")


class SellerMultipleRacesTests(TestCase):
    def test_seller_home_lists_every_live_race(self):
        seller = make_user("busyseller", "seller")
        first_customer = make_user("busybuyer1", "customer")
        second_customer = make_user("busybuyer2", "customer")
        first = Race.objects.create(customer=first_customer, product="Radio", budget=30)
        second = Race.objects.create(customer=second_customer, product="Kettle", budget=25)
        Participant.objects.create(race=first, seller=seller, slot=1, display_name="Busy",
                                   product_name="Radio", price=20, details="",
                                   image="products/r.jpg")
        Participant.objects.create(race=second, seller=seller, slot=1, display_name="Busy",
                                   product_name="Kettle", price=18, details="",
                                   image="products/k.jpg")
        self.client.force_login(seller)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Your live races", body)
        self.assertIn("Radio", body)
        self.assertIn("Kettle", body)


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
