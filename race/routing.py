from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path("ws/lobby/", consumers.LobbyConsumer.as_asgi()),
    path("ws/race/<int:race_id>/", consumers.RaceConsumer.as_asgi()),
    path("ws/chat/<int:room_id>/", consumers.ChatConsumer.as_asgi()),
]