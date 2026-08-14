from django.conf import settings
from django.test import SimpleTestCase, override_settings

from site_config.models import default_trusted_poster_hosts


class PublicOriginSettingsTests(SimpleTestCase):
    def test_bangumi_callback_and_user_agent_derive_from_public_origin(self):
        self.assertEqual(
            settings.BANGUMI_OAUTH_REDIRECT_URI,
            f"{settings.ANIMEMO_PUBLIC_ORIGIN}/api/v1/external-accounts/bangumi/callback/",
        )
        self.assertEqual(
            settings.BANGUMI_USER_AGENT,
            f"AniMemo/1.0 (+{settings.ANIMEMO_PUBLIC_ORIGIN})",
        )
        self.assertEqual(settings.FRONTEND_URL, settings.ANIMEMO_PUBLIC_ORIGIN)

    @override_settings(
        ANIMEMO_PUBLIC_ORIGIN="https://animemo.example",
        ANIMEMO_MEDIA_PUBLIC_ORIGIN="https://media.animemo.example",
    )
    def test_trusted_poster_hosts_derive_from_public_origins(self):
        self.assertEqual(
            default_trusted_poster_hosts(),
            ["lain.bgm.tv", "media.animemo.example"],
        )

    @override_settings(ANIMEMO_MEDIA_PUBLIC_ORIGIN="http://localhost:8000")
    def test_loopback_media_origin_is_not_a_remote_poster_host(self):
        self.assertEqual(default_trusted_poster_hosts(), ["lain.bgm.tv"])
