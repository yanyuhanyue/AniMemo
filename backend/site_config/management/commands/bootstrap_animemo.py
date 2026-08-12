from django.core.management import BaseCommand, call_command

from site_config.models import SiteSettings


class Command(BaseCommand):
    help = "Apply idempotent application defaults and provision the first-run setup state."

    def handle(self, *args, **options):
        call_command("sync_official_plugins", stdout=self.stdout, stderr=self.stderr)
        SiteSettings.load()
        call_command("provision_first_run_setup", stdout=self.stdout, stderr=self.stderr)
