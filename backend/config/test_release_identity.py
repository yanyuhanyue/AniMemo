from django.test import SimpleTestCase, override_settings


class ReleaseIdentityHealthTests(SimpleTestCase):
    @override_settings(
        ANIME_JOURNAL_VERSION="v1.0.0-rc.3",
        ANIME_JOURNAL_COMMIT="a" * 40,
        ANIME_JOURNAL_RELEASE_CHANNEL="rc",
    )
    def test_health_reports_immutable_release_identity(self):
        response = self.client.get("/health/", secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["release"],
            {
                "version": "v1.0.0-rc.3",
                "commit": "a" * 40,
                "channel": "rc",
            },
        )
