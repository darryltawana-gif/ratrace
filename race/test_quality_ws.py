from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from django.utils import timezone
from datetime import timedelta

from . import routing, services as sv
from .models import Participant, QualitySession, Race
from .tests import make_user


class QualityWebSocketTests(TransactionTestCase):
    """The end-to-end signalling flow used by the race page."""

    def setUp(self):
        self.customer = make_user("wsbuyer", "customer")
        self.seller = make_user("wsseller", "seller")
        self.race = Race.objects.create(customer=self.customer, product="Shoes", budget=80)
        Participant.objects.create(race=self.race, seller=self.seller, slot=1,
                                   display_name="S", product_name="Shoes", price=70,
                                   details="", image="products/q.jpg")

    async def _recv(self, communicator, wanted, tries=8):
        for _ in range(tries):
            message = await communicator.receive_json_from(timeout=3)
            if message.get("type") == wanted:
                return message
        raise AssertionError(f"Never received {wanted!r}")

    async def _recv_where(self, communicator, predicate, tries=10):
        for _ in range(tries):
            message = await communicator.receive_json_from(timeout=3)
            if predicate(message):
                return message
        raise AssertionError("Never received the expected message")

    async def _open(self, app, user, seller_id=None):
        query = f"?seller={seller_id}" if seller_id else ""
        communicator = WebsocketCommunicator(
            app, f"/ws/quality/{self.race.id}/{query}")
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    def test_seller_can_resume_after_stopping_and_customer_can_watch(self):
        async_to_sync(self._flow)()

    async def _flow(self):
        app = URLRouter(routing.websocket_urlpatterns)

        seller = await self._open(app, self.seller, self.seller.id)
        ready = await seller.receive_json_from(timeout=3)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["role"], "seller")
        self.assertEqual(ready["seller_id"], self.seller.id)
        # The seller's own join announcement is echoed back to them.
        own_join = await seller.receive_json_from(timeout=3)
        self.assertEqual(own_join["type"], "peer_join")
        self.assertEqual(own_join["role"], "seller")

        customer = await self._open(app, self.customer, self.seller.id)
        ready = await customer.receive_json_from(timeout=3)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["role"], "customer")

        joined = await self._recv(seller, "peer_join")
        self.assertEqual(joined["role"], "customer")

        # WebRTC offer from the seller reaches the customer.
        await seller.send_json_to({"kind": "offer", "data": {"sdp": "v=0"}})
        signal = await self._recv(customer, "signal")
        self.assertEqual(signal["kind"], "offer")
        self.assertEqual(signal["from"], "seller")

        # The seller stops: the customer is told, and the stream is paused.
        await seller.send_json_to({"kind": "hangup"})
        stopped = await self._recv(customer, "stopped")
        self.assertEqual(stopped["type"], "stopped")

        await seller.disconnect()
        await customer.disconnect()

        # Starting again inside the window must NOT be denied.
        seller2 = await self._open(app, self.seller, self.seller.id)
        ready2 = await seller2.receive_json_from(timeout=3)
        self.assertEqual(ready2["type"], "ready")
        self.assertEqual(ready2["role"], "seller")
        await seller2.disconnect()

    def test_customer_is_told_when_no_proof_is_live(self):
        async_to_sync(self._customer_denied)()

    async def _customer_denied(self):
        app = URLRouter(routing.websocket_urlpatterns)
        customer = await self._open(app, self.customer, self.seller.id)
        message = await customer.receive_json_from(timeout=3)
        self.assertEqual(message["type"], "denied")
        self.assertIn("hasn't started", message["message"])
        await customer.disconnect()

    def test_expired_window_is_explained_to_the_seller(self):
        session = sv.start_quality_session(self.seller, self.race.id)
        QualitySession.objects.filter(pk=session.id).update(
            started_at=timezone.now() - timedelta(seconds=sv.QUALITY_SECONDS + 1))
        async_to_sync(self._seller_expired)()

    async def _seller_expired(self):
        app = URLRouter(routing.websocket_urlpatterns)
        seller = await self._open(app, self.seller, self.seller.id)
        message = await seller.receive_json_from(timeout=3)
        self.assertEqual(message["type"], "denied")
        self.assertIn("window", message["message"])
        await seller.disconnect()

    def test_late_disconnect_from_replaced_socket_does_not_pause(self):
        async_to_sync(self._replaced_socket)()

    async def _replaced_socket(self):
        app = URLRouter(routing.websocket_urlpatterns)

        # Two sockets for the same seller: the second takes over the proof.
        seller1 = await self._open(app, self.seller, self.seller.id)
        await seller1.receive_json_from(timeout=3)   # ready
        await seller1.receive_json_from(timeout=3)   # own peer_join
        seller2 = await self._open(app, self.seller, self.seller.id)
        await seller2.receive_json_from(timeout=3)   # ready
        await seller2.receive_json_from(timeout=3)   # own peer_join

        # The old socket drops late. It must not pause the proof seller2 owns.
        await seller1.disconnect()

        customer = await self._open(app, self.customer, self.seller.id)
        message = await customer.receive_json_from(timeout=3)
        self.assertEqual(message["type"], "ready")
        self.assertEqual(message["role"], "customer")

        await customer.disconnect()
        await seller2.disconnect()

    def test_customer_race_page_is_told_when_seller_goes_live(self):
        async_to_sync(self._race_page_quality_state)()

    async def _race_page_quality_state(self):
        app = URLRouter(routing.websocket_urlpatterns)

        race_ws = WebsocketCommunicator(app, f"/ws/race/{self.race.id}/")
        race_ws.scope["user"] = self.customer
        connected, _ = await race_ws.connect()
        self.assertTrue(connected)
        initial = await self._recv_where(
            race_ws, lambda m: m.get("type") == "quality_state")
        self.assertFalse(initial.get("active"))

        # The seller going live lights the customer's View Quality button.
        seller = await self._open(app, self.seller, self.seller.id)
        await seller.receive_json_from(timeout=3)   # ready
        await seller.receive_json_from(timeout=3)   # own peer_join
        live = await self._recv_where(
            race_ws,
            lambda m: m.get("type") == "quality_state" and m.get("active"))
        self.assertEqual(live["seller_id"], self.seller.id)
        self.assertEqual(live["seller"], self.seller.username)

        # Stopping pauses it and turns the button back off.
        await seller.send_json_to({"kind": "hangup"})
        await self._recv(seller, "stopped")
        paused = await self._recv_where(
            race_ws,
            lambda m: m.get("type") == "quality_state" and not m.get("active"))
        self.assertFalse(paused.get("active"))

        await seller.disconnect()
        await race_ws.disconnect()
