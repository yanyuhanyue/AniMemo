from django.core.management import BaseCommand

from site_config.models import SiteSettings


class Command(BaseCommand):
    help = "Disable the database-backed Turnstile challenge without deleting its configuration."

    def handle(self, *args, **options):
        site_settings = SiteSettings.load()
        if site_settings.turnstile_enabled:
            site_settings.turnstile_enabled = False
            site_settings.save(update_fields=["turnstile_enabled", "updated_at"])
            self.stdout.write(self.style.SUCCESS("Turnstile disabled."))
            return
        self.stdout.write("Turnstile is already disabled.")
