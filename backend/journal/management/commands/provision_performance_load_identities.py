"""Provision isolated virtual-user journeys and short-lived access tokens."""

from __future__ import annotations

import json
import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from performance.seed import (
    ADMIN_USERNAME,
    provision_load_user_journeys,
    seed_backend_performance_data,
)
from rest_framework_simplejwt.tokens import AccessToken

from journal.staff_services import get_security_profile


class Command(BaseCommand):
    help = "Provision disposable performance users and emit their isolated access identities as JSON."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", choices=("small", "medium", "large"), default="large")
        parser.add_argument("--count", type=int, default=20)
        parser.add_argument("--token-minutes", type=int, default=40)
        parser.add_argument("--staff-password-env", default="")
        parser.add_argument("--confirm-isolated", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_isolated"]:
            raise CommandError("--confirm-isolated is required before provisioning performance identities")
        count = int(options["count"])
        token_minutes = int(options["token_minutes"])
        if count <= 0 or token_minutes <= 0:
            raise CommandError("identity count and token lifetime must be positive")

        seed_result = seed_backend_performance_data(options["dataset"], reset=True)
        journeys = provision_load_user_journeys(count)
        user_model = get_user_model()
        users = {
            user.username: user
            for user in user_model.objects.filter(username__in=[item.username for item in journeys])
        }

        staff_password_environment = str(options["staff_password_env"] or "").strip()
        if staff_password_environment:
            staff_password = os.environ.get(staff_password_environment)
            if not staff_password:
                raise CommandError(
                    f"staff password environment variable is missing: {staff_password_environment}"
                )
            admin = user_model.objects.get(username=ADMIN_USERNAME)
            admin.set_password(staff_password)
            admin.save(update_fields=["password"])

        identities = []
        for journey in journeys:
            user = users[journey.username]
            token = AccessToken.for_user(user)
            token["sv"] = get_security_profile(user).session_version
            token.set_exp(lifetime=timedelta(minutes=token_minutes))
            identities.append(
                {
                    "username": journey.username,
                    "entry_id": journey.entry_id,
                    "access_token": str(token),
                }
            )

        self.stdout.write(
            json.dumps(
                {
                    "dataset": seed_result.dataset,
                    "journal_entries": seed_result.journal_entries,
                    "identities": identities,
                },
                sort_keys=True,
            )
        )
