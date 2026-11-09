from django.urls import path

from . import views

app_name = "scamdoctor"

urlpatterns = [
    path("scam-doctor/", views.checker, name="checker"),
    path("scam-doctor/scan/<int:scan_id>/", views.scan_detail, name="scan_detail"),
    path("scam-doctor/scan/<int:scan_id>/file/", views.scan_file, name="scan_file"),
    path("scam-doctor/check/", views.check_message, name="check_message"),
]
