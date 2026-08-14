from dataclasses import dataclass
from urllib.parse import urlsplit

from config.credentials import CredentialCipher, CredentialCipherError, CredentialDecryptionError
from django.conf import settings
from django.db import transaction

from journal.models import ExternalProviderConfiguration


SOURCE_DATABASE = "database"
SOURCE_ENVIRONMENT = "environment"
SOURCE_NOT_CONFIGURED = "not_configured"
SUPPORTED_PROVIDERS = {"bangumi"}


@dataclass(frozen=True)
class EffectiveProviderConfiguration:
    provider: str
    display_name: str
    enabled: bool
    enabled_source: str
    client_id: str
    client_id_source: str
    client_secret: str
    client_secret_configured: bool
    client_secret_source: str
    oauth_callback: str
    oauth_available: bool

    def public_data(self):
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "enabled_source": self.enabled_source,
            "client_id": self.client_id,
            "client_id_source": self.client_id_source,
            "client_secret_configured": self.client_secret_configured,
            "client_secret_source": self.client_secret_source,
            "oauth_callback": self.oauth_callback,
            "oauth_available": self.oauth_available,
        }


def _normalize_provider(provider):
    provider = str(provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("不支持此外部服务配置。")
    return provider


def _environment(provider):
    if provider == "bangumi":
        return {
            "display_name": "Bangumi",
            "enabled": bool(getattr(settings, "BANGUMI_ACCOUNT_INTEGRATION_ENABLED", True)),
            "client_id": str(getattr(settings, "BANGUMI_OAUTH_CLIENT_ID", "") or "").strip(),
            "client_secret": str(getattr(settings, "BANGUMI_OAUTH_CLIENT_SECRET", "") or "").strip(),
            "oauth_callback": str(getattr(settings, "BANGUMI_OAUTH_REDIRECT_URI", "") or "").strip(),
        }
    raise ValueError("不支持此外部服务配置。")


def _configured_value(database_value, environment_value):
    if database_value:
        return database_value, SOURCE_DATABASE
    if environment_value:
        return environment_value, SOURCE_ENVIRONMENT
    return "", SOURCE_NOT_CONFIGURED


def _valid_oauth_callback(callback):
    try:
        parsed = urlsplit(callback)
        port = parsed.port
    except ValueError:
        return False
    origin_valid = bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and (port is None or 1 <= port <= 65535)
    )
    return bool(
        origin_valid
        and parsed.path == "/api/v1/external-accounts/bangumi/callback/"
        and not parsed.query
        and not parsed.fragment
    )


def get_effective_provider_configuration(provider):
    provider = _normalize_provider(provider)
    environment = _environment(provider)
    stored = ExternalProviderConfiguration.objects.filter(provider=provider).first()

    if stored is not None and stored.enabled is not None:
        enabled = stored.enabled
        enabled_source = SOURCE_DATABASE
    else:
        enabled = environment["enabled"]
        enabled_source = SOURCE_ENVIRONMENT

    database_client_id = stored.client_id if stored is not None else ""
    client_id, client_id_source = _configured_value(database_client_id, environment["client_id"])

    database_secret_configured = bool(stored and stored.encrypted_client_secret)
    database_secret = ""
    if database_secret_configured:
        try:
            database_secret = CredentialCipher.decrypt(stored.encrypted_client_secret)
        except (CredentialCipherError, CredentialDecryptionError):
            # Keep the database source visible to administrators while failing OAuth closed.
            database_secret = ""
    if database_secret_configured:
        client_secret = database_secret
        client_secret_source = SOURCE_DATABASE
    else:
        client_secret, client_secret_source = _configured_value("", environment["client_secret"])

    callback = environment["oauth_callback"]
    oauth_available = bool(
        enabled
        and client_id
        and client_secret
        and _valid_oauth_callback(callback)
    )
    return EffectiveProviderConfiguration(
        provider=provider,
        display_name=environment["display_name"],
        enabled=enabled,
        enabled_source=enabled_source,
        client_id=client_id,
        client_id_source=client_id_source,
        client_secret=client_secret,
        client_secret_configured=bool(client_secret) or database_secret_configured,
        client_secret_source=client_secret_source,
        oauth_callback=callback,
        oauth_available=oauth_available,
    )


@transaction.atomic
def update_provider_configuration(provider, *, enabled=None, client_id=None, client_secret=None, fields=()):
    provider = _normalize_provider(provider)
    stored, _ = ExternalProviderConfiguration.objects.select_for_update().get_or_create(provider=provider)
    update_fields = {"updated_at"}
    if "enabled" in fields:
        stored.enabled = enabled
        update_fields.add("enabled")
    if "client_id" in fields:
        stored.client_id = client_id
        update_fields.add("client_id")
    if "client_secret" in fields:
        stored.encrypted_client_secret = CredentialCipher.encrypt(client_secret)
        stored.credential_key_version = CredentialCipher.version
        update_fields.update({"encrypted_client_secret", "credential_key_version"})
    stored.save(update_fields=sorted(update_fields))
    return get_effective_provider_configuration(provider)


@transaction.atomic
def clear_provider_client_secret(provider):
    provider = _normalize_provider(provider)
    stored = ExternalProviderConfiguration.objects.select_for_update().filter(provider=provider).first()
    if stored is not None and (stored.encrypted_client_secret or stored.credential_key_version):
        stored.encrypted_client_secret = ""
        stored.credential_key_version = ""
        stored.save(update_fields=["encrypted_client_secret", "credential_key_version", "updated_at"])
    return get_effective_provider_configuration(provider)
