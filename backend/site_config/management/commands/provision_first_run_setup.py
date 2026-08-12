from django.core.management.base import BaseCommand, CommandError

from site_config.first_run import FirstRunBootstrapError, provision_first_run_setup
from site_config.models import InstallationState


class Command(BaseCommand):
    help = "Provision or reuse the private one-time code for the browser first-run setup flow."

    def handle(self, *args, **options):
        try:
            provisioned = provision_first_run_setup()
        except (FirstRunBootstrapError, InstallationState.DoesNotExist) as error:
            raise CommandError(str(error)) from error
        if provisioned is None:
            self.stdout.write("Installation is already initialized; no setup code was issued.")
            return
        action = "Reused" if provisioned.reused else "Provisioned"
        self.stdout.write(f"{action} private first-run setup code at {provisioned.path}.")
        self.stdout.write(f"The code expires at {provisioned.expires_at.isoformat()}.")
