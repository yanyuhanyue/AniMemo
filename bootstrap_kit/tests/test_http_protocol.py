"""Closed transport selectors; no network or release authority."""
import unittest

from bootstrap_kit.http_protocol import HttpFailure, HttpSelection, asset_redirect


class ProtocolTests(unittest.TestCase):
    def test_current_metadata_proof_and_tuf_paths_are_closed(self):
        for path in ('/releases?per_page=100&page=1', '/releases/tags/v2.0.0-rc.3',
                '/git/ref/tags/v2.0.0-rc.3', '/git/tags/' + 'a' * 40,
                '/attestations/sha1:' + 'b' * 40 + '?per_page=100&predicate_type=release',
                '/attestations/sha256:' + 'c' * 64):
            self.assertEqual(HttpSelection.github_json('/repos/yanyuhanyue/AniMemo' + path).target()[0], 'api.github.com')
        for track in ('github', 'sigstore'):
            self.assertEqual(HttpSelection.tuf_bootstrap(track).target()[0], 'raw.githubusercontent.com')
            for name in ('timestamp.json', '4.root.json', '2.snapshot.json', '1.targets.json',
                         'targets/' + 'a' * 64 + '.trusted_root.json'):
                HttpSelection.tuf(track, name).target()
        for bad in ('https://attacker.invalid/', '/repos/other/repo/releases',
                    '/repos/yanyuhanyue/AniMemo/releases/latest',
                    '/repos/yanyuhanyue/AniMemo/releases?per_page=100&page=1&page=2',
                    '/repos/yanyuhanyue/AniMemo/releases?token=not-a-secret'):
            with self.subTest(bad=bad), self.assertRaises(HttpFailure):
                HttpSelection.github_json(bad)

    def test_exact_asset_ids_namespace_digest_and_redirect(self):
        fields = {'version': 'v2.0.0-rc.3', 'release_id': 392113678, 'asset_id': 574986486,
            'name': 'installer-materials.tar', 'size': 2, 'sha256': 'a' * 64}
        selection = HttpSelection.github_asset(**fields)
        self.assertEqual(selection.target()[1], '/repos/yanyuhanyue/AniMemo/releases/assets/574986486')
        self.assertNotEqual(selection.identity, HttpSelection.github_asset(**(fields | {'release_id': 2})).identity)
        for key, value in [('release_id', True), ('asset_id', 0), ('size', 2**30),
                           ('name', '../installer-materials.tar'), ('sha256', 'x' * 64), ('kind', 'LATEST')]:
            with self.subTest(key=key), self.assertRaises(HttpFailure):
                HttpSelection.github_asset(**(fields | {key: value}))
        url = 'https://release-assets.githubusercontent.com/github-production-release-asset/1327429673/6b04b10a-2b03-4ef6-9f41-9c1dae586843?sig=NONSECRET'
        self.assertEqual(asset_redirect(url)[0], 'release-assets.githubusercontent.com')
        for bad in (url.replace('https:', 'http:'), url.replace('1327429673', '42'),
                    url.replace('release-assets.', 'attacker.'), url + '#fragment',
                    url.replace('https://', 'https://user@')):
            with self.assertRaises(HttpFailure):
                asset_redirect(bad)

    def test_actions_signed_query_cannot_change_fixed_host_or_path(self):
        selection = HttpSelection.actions_bundle('/attestations/1327429673/2026/09/20/123.json.sn', 'sig=NONSECRET')
        self.assertEqual(selection.target()[0], 'tmaproduction.blob.core.windows.net')
        for path, query in [('/attestations/42/2026/09/20/123.json.sn', 'sig=x'),
                            ('/attestations/1327429673/2026/09/20/123.json.sn', 'x\nAuthorization: value')]:
            with self.assertRaises(HttpFailure):
                HttpSelection.actions_bundle(path, query)
