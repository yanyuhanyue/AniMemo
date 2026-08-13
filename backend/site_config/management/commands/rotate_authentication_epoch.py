from django.core.management.base import BaseCommand, CommandError

from site_config.first_run import rotate_authentication_epoch
from site_config.models import InstallationState


class Command(BaseCommand):
    help = (
        "Invalidate every AniMemo JWT and Django login session after a database "
        "restore or installation identity change. The new epoch is never printed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-restore",
            action="store_true",
            help="Required acknowledgement that all existing login sessions will end.",
        )

    def handle(self, *args, **options):
        if not options["confirm_restore"]:
            raise CommandError(
                "--confirm-restore is required because this invalidates every login session."
            )
        try:
            rotate_authentication_epoch()
        except InstallationState.DoesNotExist as error:
            raise CommandError(
                "InstallationState is missing; restore and migrate the database first."
            ) from error
        self.stdout.write(
            self.style.SUCCESS(
                "Authentication epoch rotated; all previously issued JWTs are invalid."
                " All Django login sessions were deleted."
            )
        )
