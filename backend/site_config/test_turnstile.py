from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import TestCase, override_settings

from config.credentials import CredentialCipher

from .models import SiteSettings
from .turnstile import get_effective_turnstile_configuration


TEST_KEY = Fernet.generate_key().decode("ascii")


class TurnstileConfigurationTests(TestCase):
    def test_site_settings_defaults_to_disabled_without_a_secret(self):
        site_settings = SiteSettings.load()

        self.assertFalse(site_settings.turnstile_enabled)
        self.assertEqual(site_settings.turnstile_site_key, "")
        self.assertFalse(site_settings.turnstile_secret_configured)
        self.assertFalse(site_settings.turnstile_secret_ready)
        self.assertFalse(site_settings.turnstile_ready)

    @override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_KEY)
    def test_secret_is_encrypted_and_ready_only_with_complete_database_config(self):
        site_settings = SiteSettings.load()
        site_settings.turnstile_enabled = True
        site_settings.turnstile_site_key = "0x4AAAA-site-key"
        site_settings.set_turnstile_secret("turnstile-private-secret")
        site_settings.save()

        self.assertNotEqual(
            site_settings.turnstile_secret_encrypted,
            "turnstile-private-secret",
        )
        self.assertTrue(site_settings.turnstile_secret_configured)
        self.assertTrue(site_settings.turnstile_secret_ready)
        self.assertTrue(site_settings.turnstile_ready)
        self.assertEqual(site_settings.get_turnstile_secret(), "turnstile-private-secret")

        configuration = get_effective_turnstile_configuration(site_settings)
        self.assertTrue(configuration.ready)
        self.assertNotIn("turnstile-private-secret", str(configuration.public_data()))
        self.assertNotIn("turnstile_secret_encrypted", configuration.public_data())

    @override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_KEY, TURNSTILE_SECRET="environment-secret")
    def test_database_secret_decryption_failure_fails_closed_without_environment_fallback(self):
        site_settings = SiteSettings.load()
        site_settings.turnstile_enabled = True
        site_settings.turnstile_site_key = "0x4AAAA-site-key"
        site_settings.set_turnstile_secret("turnstile-private-secret")
        site_settings.save()

        with override_settings(CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii")):
            configuration = get_effective_turnstile_configuration(site_settings)

        self.assertTrue(configuration.secret_configured)
        self.assertFalse(configuration.secret_ready)
        self.assertTrue(configuration.decrypt_failed)
        self.assertFalse(configuration.ready)
        self.assertEqual(configuration.secret, "")

    @override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_KEY, TURNSTILE_SECRET="environment-secret")
    def test_empty_database_secret_does_not_fallback_to_environment(self):
        site_settings = SiteSettings.load()
        site_settings.turnstile_enabled = True
        site_settings.turnstile_site_key = "0x4AAAA-site-key"
        site_settings.save()

        configuration = get_effective_turnstile_configuration(site_settings)

        self.assertFalse(configuration.secret_configured)
        self.assertFalse(configuration.secret_ready)
        self.assertFalse(configuration.ready)
        self.assertEqual(configuration.secret, "")

    @override_settings(CREDENTIAL_ENCRYPTION_KEY=TEST_KEY)
    def test_disable_command_turns_off_challenge_but_keeps_secret(self):
        site_settings = SiteSettings.load()
        site_settings.turnstile_enabled = True
        site_settings.turnstile_site_key = "0x4AAAA-site-key"
        site_settings.set_turnstile_secret("turnstile-private-secret")
        site_settings.save()
        ciphertext = site_settings.turnstile_secret_encrypted

        output = StringIO()
        call_command("disable_turnstile", stdout=output)

        site_settings.refresh_from_db()
        self.assertFalse(site_settings.turnstile_enabled)
        self.assertEqual(site_settings.turnstile_secret_encrypted, ciphertext)
        self.assertIn("disabled", output.getvalue().lower())

    def test_secret_cipher_is_the_existing_credential_cipher(self):
        self.assertEqual(CredentialCipher.version, "v1")
