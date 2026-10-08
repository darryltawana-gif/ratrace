from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve
from race.forms import LoginForm

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(authentication_form=LoginForm), name="login"),
    path("", include("django.contrib.auth.urls")),   # logout etc.
    path("", include("race.urls")),
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]