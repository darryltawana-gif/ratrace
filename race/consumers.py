import asyncio
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from . import services


class Base(AsyncJsonWebsocketConsumer):
    async def relay(self, event):
        await self.send_json(event["payload"])


class LobbyConsumer(Base):
    async def connect(self):
        await self.channel_layer.group_add("lobby", self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard("lobby", self.channel_name)


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
        if content.get("action") == "adjust" and user.is_authenticated:
            ok = await database_sync_to_async(services.adjust)(
                user, self.race_id, content.get("field"), int(content.get("dir", 0)))
            if ok:
                state = await database_sync_to_async(services.race_state)(self.race_id)
                await self.channel_layer.group_send(
                    self.group, {"type": "relay", "payload": {"type": "state", "state": state}})