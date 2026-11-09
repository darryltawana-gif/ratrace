import asyncio
import uuid
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import services


class Base(AsyncJsonWebsocketConsumer):
    async def relay(self, event):
        await self.send_json(event["payload"])


class LobbyConsumer(Base):
    """Every page connects here: site-wide alerts + private alerts for the signed-in user."""
    async def connect(self):
        await self.channel_layer.group_add("lobby", self.channel_name)
        user = self.scope["user"]
        self.user_group = f"user_{user.id}" if user.is_authenticated else None
        if self.user_group:
            await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard("lobby", self.channel_name)
        if getattr(self, "user_group", None):
            await self.channel_layer.group_discard(self.user_group, self.channel_name)


class RaceConsumer(Base):
    async def connect(self):
        self.race_id = self.scope["url_route"]["kwargs"]["race_id"]
        user = self.scope["user"]
        allowed = await database_sync_to_async(services.race_access)(user, self.race_id)
        if not allowed:
            await self.close()
            return
        self.group = f"race_{self.race_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        state = await database_sync_to_async(services.race_state)(self.race_id)
        await self.send_json({"type": "state", "state": state})
        # Tell the page straight away whether a proof is already live, so a
        # customer who joins late (or refreshes) still sees the lit button.
        quality = await database_sync_to_async(services.quality_payload)(self.race_id)
        await self.send_json({"type": "quality_state", **quality})
        self.timer = asyncio.create_task(self.expiry())

    async def disconnect(self, code):
        if hasattr(self, "timer"):
            self.timer.cancel()
        await self.channel_layer.group_discard(self.group, self.channel_name)

    async def expiry(self):
        wait = await database_sync_to_async(services.seconds_left)(self.race_id)
        await asyncio.sleep(max(wait, 0) + 1)
        await database_sync_to_async(services.expire_races)()

    async def receive_json(self, content):
        user = self.scope["user"]
        if not user.is_authenticated:
            return
        action = content.get("action")
        ok = False
        if action == "adjust":
            try:
                direction = int(content.get("dir", 0))
            except (TypeError, ValueError):
                return
            ok = await database_sync_to_async(services.adjust)(
                user, self.race_id, content.get("field"), direction)
        elif action == "promo_item":
            ok = await database_sync_to_async(services.set_promo_item)(
                user, self.race_id, str(content.get("text", "")))
        elif action == "quality_request":
            # Seller asks the customer to watch a live Quality Proof video.
            # The server owns the clock and allows one 48-minute window per
            # seller per race; stopping and starting again inside that window
            # is allowed.
            session = await database_sync_to_async(services.start_quality_session)(user, self.race_id)
            if session:
                payload = await database_sync_to_async(services.quality_session_payload)(session)
                await self.channel_layer.group_send(self.group, {
                    "type": "relay", "payload": {"type": "quality_state", **payload}})
            else:
                await self.send_json({
                    "type": "quality_denied",
                    "message": "Your Quality Proof window for this race has ended. "
                               "It lasts up to 48 minutes from the first time you start."})
        if ok:
            state = await database_sync_to_async(services.race_state)(self.race_id)
            await self.channel_layer.group_send(
                self.group, {"type": "relay", "payload": {"type": "state", "state": state}})


class QualityConsumer(Base):
    """WebRTC signalling for one seller's Quality Proof video.

    The timer is based on the server-side QualitySession started by the
    seller. Reconnecting never gives a fresh window. Inside that window the
    seller may stop and start again. Each seller gets their own signalling
    group, so two sellers in the same race can never mix up their streams.
    """
    async def connect(self):
        self.race_id = self.scope["url_route"]["kwargs"]["race_id"]
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close()
            return
        allowed = await database_sync_to_async(services.can_stream_quality)(user, self.race_id)
        if not allowed:
            await self.close()
            return
        self.role = await database_sync_to_async(services.race_role)(user, self.race_id)
        params = parse_qs((self.scope.get("query_string") or b"").decode())
        requested = params.get("seller", [""])[0]
        if self.role == "seller":
            # A fresh token marks this socket as the one that owns the proof.
            self.connection_id = uuid.uuid4().hex
            session = await database_sync_to_async(services.start_quality_session)(
                user, self.race_id, connection_id=self.connection_id)
            self.seller_id = user.id
        else:
            try:
                seller_id = int(requested) if requested else None
            except (TypeError, ValueError):
                seller_id = None
            session = await database_sync_to_async(services.quality_for_race)(
                self.race_id, seller_id)
            if not session and seller_id is None:
                # Older clients did not name a seller: pick the newest proof.
                session = await database_sync_to_async(services.quality_for_race)(self.race_id)
            self.seller_id = session.seller_id if session else seller_id
        if not session:
            await self.accept()
            if self.role == "seller":
                message = ("Quality Proof isn't available right now. Start it from the "
                           "race page while the race is live — or your 48-minute window "
                           "has already ended.")
            else:
                message = ("The seller hasn't started their Quality Proof yet. "
                           "The View Quality button will light up when they go live.")
            await self.send_json({"type": "denied", "message": message})
            await self.close()
            return
        self.session_id = session.id
        self.seller_id = session.seller_id
        self.seller_name = await database_sync_to_async(services.quality_seller_name)(
            self.session_id)
        self.seconds_left = services.quality_seconds_left(session)
        self.group = f"qp_{self.race_id}_{self.seller_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({"type": "ready", "role": self.role,
                              "seller_id": self.seller_id,
                              "seller": self.seller_name,
                              "seconds": self.seconds_left,
                              "session_id": self.session_id})
        await self.channel_layer.group_send(
            self.group, {"type": "relay", "payload": {"type": "peer_join", "role": self.role}})
        if self.role == "seller":
            # Light up the customer's View Quality button as soon as the
            # camera is actually connected (not merely when it was clicked).
            payload = await database_sync_to_async(services.quality_session_payload)(session)
            await self.channel_layer.group_send(
                f"race_{self.race_id}",
                {"type": "relay", "payload": {"type": "quality_state", **payload}})
        # Both sides get the same countdown, measured from the seller's start.
        if self.seconds_left > 0:
            await self.start_timer(self.seconds_left)

    async def start_timer(self, seconds):
        async def countdown():
            await asyncio.sleep(seconds)
            await database_sync_to_async(services.end_quality_session)(self.session_id)
            await self.channel_layer.group_send(
                self.group, {"type": "relay", "payload": {"type": "time_up"}})
            await self.broadcast_quality_state()
        if getattr(self, "timer", None):
            self.timer.cancel()
        self.timer = asyncio.create_task(countdown())

    async def broadcast_quality_state(self):
        """Refresh the customer's button from the database.

        With several sellers in one race, this keeps the button lit for
        whoever is still live instead of blindly switching it off.
        """
        payload = await database_sync_to_async(services.quality_payload)(self.race_id)
        await self.channel_layer.group_send(
            f"race_{self.race_id}",
            {"type": "relay", "payload": {"type": "quality_state", **payload}})

    async def disconnect(self, code):
        if getattr(self, "timer", None):
            self.timer.cancel()
        if not hasattr(self, "group"):
            return
        if getattr(self, "role", None) == "seller":
            # Only the socket that still owns the proof may pause it. A late
            # disconnect from a replaced socket must not touch the live one.
            released = await database_sync_to_async(services.release_quality_session)(
                self.session_id, self.connection_id)
            if released:
                await self.channel_layer.group_send(
                    self.group,
                    {"type": "relay", "payload": {"type": "peer_left", "role": self.role}})
                await self.broadcast_quality_state()
        else:
            await self.channel_layer.group_send(
                self.group,
                {"type": "relay", "payload": {"type": "peer_left", "role": self.role}})
        await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content):
        kind = content.get("kind")   # hello | offer | answer | ice | hangup
        if kind not in ("hello", "offer", "answer", "ice", "hangup"):
            return
        if kind == "hangup":
            if self.role == "seller":
                # The seller finished showing the product: pause the stream.
                released = await database_sync_to_async(services.release_quality_session)(
                    self.session_id, self.connection_id)
                if released:
                    await self.channel_layer.group_send(
                        self.group, {"type": "relay", "payload": {"type": "stopped"}})
                    await self.broadcast_quality_state()
            else:
                # The customer closed their viewer. The seller keeps the
                # session open and can show the product again when they return.
                await self.channel_layer.group_send(
                    self.group,
                    {"type": "relay", "payload": {"type": "peer_left", "role": self.role}})
            return
        await self.channel_layer.group_send(self.group, {
            "type": "relay",
            "payload": {"type": "signal", "kind": kind, "from": self.role,
                        "data": content.get("data")}})


class CircleConsumer(Base):
    """Group chat for a customer's My Circle."""
    async def connect(self):
        self.circle_id = int(self.scope["url_route"]["kwargs"]["circle_id"])
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close()
            return
        circle = await database_sync_to_async(services.get_circle)(self.circle_id)
        if not circle or not await database_sync_to_async(services.is_circle_member)(circle, user):
            await self.close()
            return
        self.group = f"circle_{self.circle_id}"
        self.user = user
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        history = await database_sync_to_async(services.circle_history)(self.circle_id)
        await self.send_json({"type": "history", "messages": history,
                              "topic": circle.current_topic,
                              "topic_label": circle.current_topic_label,
                              "is_owner": circle.customer_id == user.id})

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content):
        # The customer chooses the topic; it then applies to every message
        # until the customer changes it again.
        if content.get("action") == "topic":
            topic = str(content.get("topic", ""))
            res = await database_sync_to_async(services.set_circle_topic)(
                self.user, self.circle_id, topic)
            if res:
                await self.channel_layer.group_send(
                    self.group, {"type": "relay", "payload": {"type": "topic", **res}})
            return
        if content.get("action") == "react":
            try:
                message_id = int(content.get("message_id"))
            except (TypeError, ValueError):
                return
            emoji = str(content.get("emoji", ""))
            res = await database_sync_to_async(services.react_to_circle_message)(
                self.user, self.circle_id, message_id, emoji)
            if res:
                await self.channel_layer.group_send(
                    self.group,
                    {"type": "relay", "payload": {"type": "reaction", **res}})
            return
        text = str(content.get("text", "")).strip()[:1000]
        topic = str(content.get("topic", ""))
        try:
            reply_to_id = int(content.get("reply_to")) if content.get("reply_to") else None
        except (TypeError, ValueError):
            reply_to_id = None
        if not text:
            return
        res = await database_sync_to_async(services.save_circle_message)(
            self.user, self.circle_id, text, topic, reply_to_id)
        if not res:
            return
        await self.channel_layer.group_send(
            self.group, {"type": "relay", "payload": {"type": "message", "message": res["message"]}})
        if res.get("topic_changed"):
            await self.channel_layer.group_send(
                self.group, {"type": "relay", "payload": {
                    "type": "topic", "topic": res["topic_changed"],
                    "label": dict(services.CIRCLE_TOPICS).get(res["topic_changed"], "Any other talks"),
                    "by": self.user.username}})
        # Wake up accepted members who are elsewhere on the site.
        recipients = await database_sync_to_async(services.circle_recipient_ids)(
            self.circle_id, exclude=self.user.id)
        for uid in recipients:
            await self.channel_layer.group_send(
                f"user_{uid}",
                {"type": "relay", "payload": {
                    "type": "chat_message", "room_id": self.circle_id,
                    "message": f"👥 {self.user.username}: {text[:80]}",
                    "url": f"/circle/chat/{self.circle_id}/"}})


class ChatConsumer(Base):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        user = self.scope["user"]
        if not user.is_authenticated:
            await self.close()
            return
        allowed = await database_sync_to_async(services.can_chat)(user, self.room_id)
        if not allowed:
            await self.close()
            return
        self.group = f"chat_{self.room_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        history = await database_sync_to_async(services.chat_history)(self.room_id)
        await self.send_json({"type": "history", "messages": history})

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content):
        user = self.scope["user"]
        text = str(content.get("text", "")).strip()[:1000]
        if not text:
            return
        res = await database_sync_to_async(services.save_message)(user, self.room_id, text)
        if not res:
            return
        await self.channel_layer.group_send(
            self.group, {"type": "relay", "payload": {"type": "message", "message": res["message"]}})
        # Alert the other person wherever they are on the site
        await self.channel_layer.group_send(
            f"user_{res['to_user_id']}",
            {"type": "relay", "payload": {
                "type": "chat_message", "room_id": int(self.room_id),
                "message": f"💬 {res['from_label']}: {text[:80]}", "url": res["room_url"]}})
        # ...and on their phone even if the site is closed
        await database_sync_to_async(services.push_to_users)(
            [res["to_user_id"]],
            {"title": f"💬 {res['from_label']}", "body": text[:120], "url": res["room_url"],
             "tag": f"chat-{self.room_id}"}, 3600)
