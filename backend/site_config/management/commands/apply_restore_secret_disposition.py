from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from journal.models import ExternalProviderConfiguration

from site_config.models import SiteSettings

_ALLOWED = {
    "BANGUMI_OAUTH_CLIENT_SECRET",
    "RESEND_API_KEY",
    "TURNSTILE_SECRET",
}


class Command(BaseCommand):
    help = (
        "Apply authenticated Secret Envelope RECONFIGURE dispositions after "
        "database restore. Secret values are never accepted by this command."
    )

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="append", default=[])

    def handle(self, *args, **options):
        del args
        requested = tuple(sorted(set(options["clear"])))
        if any(name not in _ALLOWED for name in requested):
            raise CommandError("RESTORE_SECRET_DISPOSITION_INVALID")
        with transaction.atomic():
            if "BANGUMI_OAUTH_CLIENT_SECRET" in requested:
                ExternalProviderConfiguration.objects.update(
                    encrypted_client_secret="",
                    credential_key_version="",
                )
            site = SiteSettings.load()
            fields = []
            if "RESEND_API_KEY" in requested:
                site.resend_api_key_encrypted = ""
                fields.append("resend_api_key_encrypted")
            if "TURNSTILE_SECRET" in requested:
                site.turnstile_secret_encrypted = ""
                site.turnstile_enabled = False
                fields.extend(("turnstile_secret_encrypted", "turnstile_enabled"))
            if fields:
                site.save(update_fields=(*fields, "updated_at"))
        self.stdout.write("RESTORE_SECRET_DISPOSITION_APPLIED")
