import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_license_docs import (
    ANIMEMO_AVATAR_PATH,
    ANIMEMO_FALLBACK_POSTER_PATH,
    DEMO_SUBJECT_IDS,
    POLYFORM_BLOB,
    POLYFORM_LINES,
    POLYFORM_PATH,
    POLYFORM_SHA256,
    POLYFORM_SIZE,
    PRODUCT_IDENTITY,
    README_ASSET_WARNING,
    ROOT_LICENSE_PATH,
    REMOVED_BUNDLED_POSTERS,
    ValidationError,
    _git_blob,
    _lf_bytes,
    _logical_lines,
    validate_all,
    validate_documents,
    validate_legacy_identity_boundaries,
    validate_media_policy,
    validate_polyform,
)


class LicenseDocumentationTests(unittest.TestCase):
    def test_repository_license_documentation_is_current(self):
        validate_all()

    def test_polyform_official_fingerprints(self):
        payload = _lf_bytes((ROOT / POLYFORM_PATH).read_bytes())
        self.assertEqual(_git_blob(payload), POLYFORM_BLOB)
        self.assertEqual(__import__("hashlib").sha256(payload).hexdigest(), POLYFORM_SHA256)
        self.assertEqual(len(payload), POLYFORM_SIZE)
        self.assertEqual(_logical_lines(payload), POLYFORM_LINES)
        validate_polyform()

    def test_readme_keeps_final_product_identity_and_asset_warning(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(PRODUCT_IDENTITY, readme)
        self.assertIn(README_ASSET_WARNING, readme)
        validate_documents()

    def test_root_license_is_byte_identical_to_named_polyform_copy(self):
        self.assertEqual(
            _lf_bytes((ROOT / ROOT_LICENSE_PATH).read_bytes()),
            _lf_bytes((ROOT / POLYFORM_PATH).read_bytes()),
        )

    def test_media_policy_keeps_only_original_brand_and_fallback_assets(self):
        self.assertGreater((ROOT / ANIMEMO_AVATAR_PATH).stat().st_size, 0)
        self.assertGreater((ROOT / ANIMEMO_FALLBACK_POSTER_PATH).stat().st_size, 0)
        self.assertTrue(all(not (ROOT / path).exists() for path in REMOVED_BUNDLED_POSTERS))
        self.assertEqual(len(DEMO_SUBJECT_IDS), 16)
        self.assertEqual(len(set(DEMO_SUBJECT_IDS)), 16)
        validate_media_policy()

    def test_release_documents_have_no_legacy_provenance_markers(self):
        validate_documents()

    def test_legacy_identity_literals_stay_inside_migration_or_compatibility_boundaries(self):
        validate_legacy_identity_boundaries()

    def test_polyform_validator_rejects_changed_bytes(self):
        payload = _lf_bytes((ROOT / POLYFORM_PATH).read_bytes())
        self.assertNotEqual(_git_blob(payload + b"\n"), POLYFORM_BLOB)
        self.assertNotEqual(_logical_lines(payload + b"\n"), POLYFORM_LINES)

    def test_validation_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(ValidationError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
