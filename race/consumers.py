import asyncio
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
        self.group = f"race_{self.race_id}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        state = await database_sync_to_async(services.race_state)(self.race_id)
        await self.send_json({"type": "state", "state": state})
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
        if ok:
            state = await database_sync_to_async(services.race_state)(self.race_id)
            await self.channel_layer.group_send(
                self.group, {"type": "relay", "payload": {"type": "state", "state": state}})


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