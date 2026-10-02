import copy
import unittest
from dataclasses import replace
from datetime import timedelta

from bootstrap_kit import manifest
from bootstrap_kit.tests.fixtures import NOW, minimal_manifest, selection


class ManifestTests(unittest.TestCase):
    def test_static_initial_pins_equal_original_policy_and_cannot_be_reselected_by_manifest(self):
        from release.trust_bootstrap import _TRACKS
        self.assertEqual(manifest.INITIAL_ROOT_PINS,
                         {domain: track['bootstrapSha256'] for domain, track in _TRACKS.items()})
        value = minimal_manifest()
        value['trustRoots']['github']['initialSha256'], value['trustRoots']['sigstore']['initialSha256'] = (
            value['trustRoots']['sigstore']['initialSha256'], value['trustRoots']['github']['initialSha256'])
        with self.assertRaises(ValueError):
            manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)

    def test_current_roots_require_exact_members_and_all_six_fixed_trust_roles(self):
        for mutation in ('swapped-current', 'missing-verifier', 'wrong-role', 'empty-verifier'):
            value = minimal_manifest()
            if mutation == 'swapped-current':
                value['trustRoots']['github']['currentSha256'], value['trustRoots']['sigstore']['currentSha256'] = (
                    value['trustRoots']['sigstore']['currentSha256'], value['trustRoots']['github']['currentSha256'])
            else:
                verifier = next(item for item in value['members'] if item['path'] == 'trust/offline-release-verifier')
                if mutation == 'missing-verifier':
                    value['members'].remove(verifier)
                elif mutation == 'wrong-role':
                    verifier['role'] = 'LIBRARY'
                else:
                    verifier['size'] = 0
            # A recalculated external hash still cannot weaken static policy.
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)

    def test_external_pin_and_explicit_lifecycle_permit_only_selected_dev_manifest(self):
        value = minimal_manifest()
        self.assertEqual(manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW), value)

    def test_hash_is_checked_before_untrusted_json_parse(self):
        value = minimal_manifest()
        with self.assertRaisesRegex(manifest.ManifestError, "PIN_MISMATCH"):
            manifest.validate_manifest(b'not json', selection(value), now=NOW)

    def test_lower_external_version_sequence_and_other_source_are_rejected(self):
        value = minimal_manifest()
        for changed in (replace(selection(value), minimum_sequence=4),
                        replace(selection(value), minimum_version="1.1.0"),
                        replace(selection(value), expected_source_commit="c"*40),
                        replace(selection(value), expected_source_tree="c"*40),
                        replace(selection(value), expected_platform="linux-amd64-cp312"),
                        replace(selection(value), expected_entry_size=101)):
            with self.subTest(selection=changed), self.assertRaises(manifest.ManifestError):
                manifest.validate_manifest(manifest.canonical(value), changed, now=NOW)

    def test_closed_fields_disabled_expiry_platform_and_root_mixing_rejected(self):
        mutations = [lambda v:v.update(extra=True), lambda v:v.pop("policies"),
            lambda v:v.update(kind="PRODUCT_RELEASE"), lambda v:v.update(disabled=True),
            lambda v:v.update(sequence=True), lambda v:v.update(issuedAt="2026-09-21T00:00:00Z"),
            lambda v:v.update(expiresAt="2026-09-20T00:00:00Z"),
            lambda v:v.update(expiresAt="2027-01-01T00:00:00Z"),
            lambda v:v.update(pythonAbi="cp313"), lambda v:v.update(classification="TEST_ONLY"),
            lambda v:v["trustRoots"].update(sigstore=copy.deepcopy(v["trustRoots"]["github"])),
            lambda v:v["trustRoots"]["github"].update(expiresAt="2026-09-25T00:00:00Z"),
            lambda v:v["distribution"].update(archiveUrl="https://unknown.invalid/package"),
            lambda v:v["source"].update(locator="https://unknown.invalid/source")]
        for index, mutate in enumerate(mutations):
            value = minimal_manifest()
            mutate(value)
            with self.subTest(mutation=index), self.assertRaises(ValueError):
                manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)

    def test_member_names_links_budget_duplicates_and_missing_entry_rejected(self):
        for name in ("../escape", "/absolute", "a\\b", "a//b", "C:/file", "a/./b", "CON.py", "a/b."):
            value = minimal_manifest()
            value["members"][0]["path"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)
        for change in ({"size": manifest.MAX_FILE+1}, {"size": -1}, {"size": True},
                       {"type": "symlink"}, {"role": "PRODUCT"}, {"sha256": "sha1:bad"}):
            value = minimal_manifest()
            value["members"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)
        value = minimal_manifest()
        value["members"] *= 2
        with self.assertRaises(ValueError):
            manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)

    def test_duplicate_keys_depth_nonfinite_and_noncanonical_are_rejected_after_pin(self):
        value = minimal_manifest()
        raw_cases = [b'{"x":1,"x":2}', b'{"x":NaN}', b'['*1001+b']'*1001,
                     manifest.canonical(value)+b' ', b'\xff']
        for raw in raw_cases:
            selected = replace(selection(value), expected_manifest_sha256=manifest.digest(raw))
            with self.assertRaises(ValueError):
                manifest.validate_manifest(raw, selected, now=NOW)

    def test_production_schema_validates_external_selection_without_issuing_capability(self):
        value = minimal_manifest()
        with self.assertRaises(ValueError):
            manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW, production=True)
        value["classification"] = "PRODUCTION"
        prefix = "https://github.com/yanyuhanyue/AniMemo/releases/download/bootstrap-trust-kit-v1.0.0/bootstrap-trust-kit-windows-amd64"
        value["distribution"] = {"status": "PUBLISHED_IMMUTABLE", "releaseId": 123,
                                 "manifestUrl": prefix+".manifest.json", "archiveUrl": prefix+".tar"}
        verified = manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW, production=True)
        self.assertIs(type(verified), dict)
        self.assertNotIn("authority", verified)
        with self.assertRaises(ValueError):
            manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW)
        with self.assertRaises(ValueError):
            manifest.validate_manifest(manifest.canonical(value), selection(value), now=NOW+timedelta(days=20), production=True)


if __name__ == "__main__":
    unittest.main()
