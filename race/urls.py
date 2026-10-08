from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("signup/", views.signup, name="signup"),
    path("role/<str:role>/", views.set_role, name="set_role"),
    path("start/", views.customer_start, name="start"),
    path("race/<int:race_id>/", views.race_room, name="race_room"),
    path("race/<int:race_id>/exit/", views.customer_exit, name="customer_exit"),
    path("race/<int:race_id>/reopen/", views.customer_reopen, name="customer_reopen"),
    path("race/<int:race_id>/winner/<int:pid>/", views.pick_winner, name="pick_winner"),
    path("race/<int:race_id>/leave/", views.seller_leave, name="seller_leave"),
    path("race/<int:race_id>/chat/<int:pid>/", views.start_chat, name="start_chat"),
    path("chats/", views.chat_list, name="chat_list"),
    path("chat/<int:room_id>/", views.chat_room, name="chat_room"),
    path("profile/", views.seller_profile, name="profile"),
    path("comments/", views.comments, name="comments"),
    path("api/live-races/", views.api_live, name="api_live"),
    path("sw.js", views.sw_js),
    path("manifest.json", views.manifest),
]