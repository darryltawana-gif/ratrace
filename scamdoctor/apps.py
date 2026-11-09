from django.apps import AppConfig


class ScamDoctorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scamdoctor"
    verbose_name = "Scam Doctor"

    def ready(self):
        # Connect the automatic chat checks. Importing here avoids touching
        # models before Django has finished loading the app registry.
        from . import signals  # noqa: F401
