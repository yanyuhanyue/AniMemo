from __future__ import annotations

import unittest
from unittest import mock

from scripts.smoke_distribution_images import smoke_images, web_resources


class DistributionImageSmokeTests(unittest.TestCase):
    def test_web_resource_references_include_hashed_assets_and_local_icons(self):
        self.assertEqual(
            web_resources('<script src="/assets/app-123.js"></script><link href="/assets/app.css"><link href="/icon.svg"><img src="https://example.invalid/image.png">'),
            ["/usr/share/nginx/html/assets/app-123.js", "/usr/share/nginx/html/assets/app.css", "/usr/share/nginx/html/icon.svg"],
        )

    def test_web_missing_entrypoint_and_parent_paths_fail(self):
        for value in ("<div></div>", '<script src="/../app.js"></script>'):
            with self.subTest(html=value), self.assertRaises(ValueError):
                web_resources(value)

    def test_image_probes_use_exact_local_ids_without_network_or_source_mounts(self):
        api = "sha256:" + "a" * 64
        web = "sha256:" + "b" * 64
        with mock.patch(
            "scripts.smoke_distribution_images._docker",
            side_effect=[api, web, '{"templates":3}', '<script src="/assets/app.js"></script>', ""],
        ) as docker:
            result = smoke_images("animemo-api:test", "animemo-web:test")
        self.assertEqual(result["api_image"], api)
        self.assertEqual(result["web_image"], web)
        for call in docker.call_args_list[2:]:
            arguments = call.args
            self.assertIn("--read-only", arguments)
            self.assertEqual(arguments[arguments.index("--network") + 1], "none")
            self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
            self.assertNotIn("--volume", arguments)
            self.assertNotIn("--mount", arguments)
            self.assertTrue(api in arguments or web in arguments)


if __name__ == "__main__":
    unittest.main()
