"""Runtime Turnstile configuration resolved from the SiteSettings singleton."""

from dataclasses import dataclass, field

from config.credentials import CredentialCipherError

from .models import SiteSettings


@dataclass(frozen=True)
class EffectiveTurnstileConfiguration:
    enabled: bool
    site_key: str
    secret: str = field(repr=False, default="")
    secret_configured: bool = False
    secret_ready: bool = False
    decrypt_failed: bool = False
    ready: bool = False

    def public_data(self):
        """Return only non-secret Turnstile metadata."""
        return {
            "turnstile_enabled": self.enabled,
            "turnstile_site_key": self.site_key,
            "turnstile_secret_configured": self.secret_configured,
            "turnstile_secret_ready": self.secret_ready,
            "turnstile_ready": self.ready,
            "turnstile": {
                "enabled": self.enabled,
                "site_key": self.site_key,
            },
        }


def get_effective_turnstile_configuration(site_settings=None):
    """Resolve DB-backed Turnstile configuration and fail closed on bad data."""
    site_settings = site_settings or SiteSettings.load()
    encrypted_secret = str(site_settings.turnstile_secret_encrypted or "")
    decrypt_failed = False
    if encrypted_secret:
        try:
            secret = site_settings.get_turnstile_secret()
        except CredentialCipherError:
            secret = ""
            decrypt_failed = True
    else:
        secret = ""

    secret_configured = bool(encrypted_secret)
    secret_ready = bool(secret) and not decrypt_failed
    site_key = str(site_settings.turnstile_site_key or "").strip()
    enabled = bool(site_settings.turnstile_enabled)
    ready = bool(enabled and site_key and secret_ready)
    return EffectiveTurnstileConfiguration(
        enabled=enabled,
        site_key=site_key,
        secret=secret,
        secret_configured=secret_configured,
        secret_ready=secret_ready,
        decrypt_failed=decrypt_failed,
        ready=ready,
    )


def resolve_turnstile_config(site_settings=None):
    """Short resolver alias used by request-time adapters."""
    return get_effective_turnstile_configuration(site_settings)


def is_turnstile_ready(site_settings=None):
    return get_effective_turnstile_configuration(site_settings).ready
