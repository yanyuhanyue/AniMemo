import secrets

from django.core.management.base import BaseCommand, CommandError

from site_config.first_run import provision_first_run_setup


def _command_failure():
    return CommandError(
        "first_run_provision_failed "
        f"correlation_id={secrets.token_hex(16)}"
    )


class Command(BaseCommand):
    help = "Provision or reuse the private one-time code for the browser first-run setup flow."

    def handle(self, *args, **options):
        try:
            return self._handle(*args, **options)
        except Exception:
            raise _command_failure() from None

    def _handle(self, *args, **options):
        provisioned = provision_first_run_setup()
        if provisioned is None:
            self.stdout.write("Installation is already initialized; no setup code was issued.")
            return
        action = "Reused" if provisioned.reused else "Provisioned"
        self.stdout.write(f"{action} private first-run setup code at {provisioned.path}.")
        self.stdout.write(f"The code expires at {provisioned.expires_at.isoformat()}.")
