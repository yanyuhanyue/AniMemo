import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ExternalIdentityBinding, IntegrationPairingCode


PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class PairingError(ValueError):
    code = "pairing_failed"


class PairingCodeInvalid(PairingError):
    code = "pairing_code_invalid"


class IdentityAlreadyBound(PairingError):
    code = "identity_already_bound"


def normalize_pairing_code(code):
    return "".join(character for character in str(code or "").upper() if character.isalnum())


def pairing_code_lookup(code):
    return signing.salted_hmac(
        "integrations.pairing.lookup.v1",
        normalize_pairing_code(code),
    ).hexdigest()


def _new_pairing_code():
    compact = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
    return f"{compact[:4]}-{compact[4:]}"


def create_pairing_code(connection, user):
    ttl = int(getattr(settings, "INTEGRATION_PAIRING_CODE_TTL_SECONDS", 600))
    now = timezone.now()
    with transaction.atomic():
        IntegrationPairingCode.objects.select_for_update().filter(
            connection=connection,
            user=user,
            consumed_at__isnull=True,
        ).update(consumed_at=now)
        for _attempt in range(10):
            plaintext = _new_pairing_code()
            try:
                row = IntegrationPairingCode.objects.create(
                    connection=connection,
                    user=user,
                    code_lookup=pairing_code_lookup(plaintext),
                    code_hash=make_password(normalize_pairing_code(plaintext)),
                    expires_at=now + timedelta(seconds=ttl),
                )
                return row, plaintext
            except IntegrityError:
                continue
    raise PairingError("无法生成唯一配对码，请重试。")


def consume_pairing_code(connection, code, platform, external_user_id, display_name=""):
    normalized = normalize_pairing_code(code)
    if len(normalized) != 8:
        raise PairingCodeInvalid("配对码无效或已失效。")
    now = timezone.now()
    conflict = False
    binding = None
    with transaction.atomic():
        row = IntegrationPairingCode.objects.select_for_update().filter(
            connection=connection,
            code_lookup=pairing_code_lookup(normalized),
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).select_related("user").first()
        if row is None or not check_password(normalized, row.code_hash):
            raise PairingCodeInvalid("配对码无效或已失效。")

        row.consumed_at = now
        row.save(update_fields=["consumed_at"])
        if ExternalIdentityBinding.objects.filter(
            connection=connection,
            platform=platform,
            external_user_id=external_user_id,
        ).exists():
            conflict = True
        else:
            try:
                with transaction.atomic():
                    binding = ExternalIdentityBinding.objects.create(
                        connection=connection,
                        user=row.user,
                        platform=platform,
                        external_user_id=external_user_id,
                        display_name=display_name,
                        verified_at=now,
                    )
            except IntegrityError:
                conflict = True
    if conflict:
        raise IdentityAlreadyBound("该外部身份已绑定。")
    return binding
