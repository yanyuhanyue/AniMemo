from django.test import SimpleTestCase, override_settings


class ReleaseIdentityHealthTests(SimpleTestCase):
    @override_settings(
        ANIMEMO_VERSION="v1.0.0-rc.3",
        ANIMEMO_COMMIT="a" * 40,
        ANIMEMO_RELEASE_CHANNEL="rc",
        ANIMEMO_ARTIFACT_VERSION="v1.0.0-rc.2",
        ANIMEMO_ARTIFACT_COMMIT="a" * 40,
        ANIMEMO_ARTIFACT_CHANNEL="rc",
        ANIMEMO_DATABASE_CONTRACT="animemo-db-v1",
        ANIMEMO_CONFIGURATION_CONTRACT="animemo-config-v1",
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
        self.assertEqual(
            response.json()["artifact"],
            {
                "version": "v1.0.0-rc.2",
                "commit": "a" * 40,
                "channel": "rc",
            },
        )
        self.assertEqual(
            response.json()["contracts"],
            {"database": "animemo-db-v1", "configuration": "animemo-config-v1"},
        )
