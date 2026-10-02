"""Negative and consumer-boundary DEV tests; no successful crypto is mocked."""
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import bootstrap
from installer import tokenless_stage0 as stage0


class TestOnlyStage0Tests(unittest.TestCase):
    def broken_test_capability(self):
        # Negative malformed state, never a verified record or successful proof.
        result = object.__new__(stage0.TestOnlyStage0Release)
        result._record, result._used = b'{}', False
        return result

    def test_constructor_cannot_issue_from_plain_dict_or_foreign_issuer(self):
        with self.assertRaises(stage0.TokenlessStage0Error):
            stage0.TestOnlyStage0Release(object(), {"verified": True})
        self.assertFalse(issubclass(stage0.TestOnlyStage0Release, stage0.VerifiedStage0Release))

    def test_real_factory_rejects_invalid_inputs_before_process_and_commit(self):
        with mock.patch("installer.apt_diagnostics.subprocess.Popen") as launch, \
                mock.patch.object(bootstrap, "commit_bootstrap_authorization") as commit:
            with self.assertRaises(stage0.TokenlessStage0Error):
                stage0.verify_for_test_only(material=None, inputs=None,
                    archive=Path("unused"), expected_commit="a"*40)
            launch.assert_not_called()
            commit.assert_not_called()

    def test_dev_outer_entry_performs_real_local_file_read_and_real_factory_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory)/"installer-materials.tar"
            archive.write_bytes(b"unverified fixture bytes")
            with mock.patch("installer.apt_diagnostics.subprocess.Popen") as launch, \
                    mock.patch.object(bootstrap, "commit_bootstrap_authorization") as commit, \
                    mock.patch.object(bootstrap, "_safe_root", side_effect=AssertionError("system root used")), \
                    self.assertRaises(stage0.TokenlessStage0Error):
                bootstrap.authorize_online_stage0_test_only(tag="v2.0.0-rc.3", release_commit="a"*40,
                    verified_at="2026-09-20T00:00:00Z", archive=archive, material=None, inputs=None,
                    scratch_parent=Path(directory))
            launch.assert_not_called()
            commit.assert_not_called()

    def test_production_consumer_rejects_dev_type_before_consuming_or_committing(self):
        capability = self.broken_test_capability()
        with mock.patch.object(bootstrap, "commit_bootstrap_authorization") as commit:
            with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, "CAPABILITY_REQUIRED"):
                bootstrap._consume_stage0_binding(before=b"x", after=b"x", verified=capability,
                    expected_type=stage0.VerifiedStage0Release, tag="v2.0.0-rc.3", release_commit="a"*40)
            commit.assert_not_called()

    def test_failed_consumption_is_exhausted_and_non_serializable(self):
        capability = self.broken_test_capability()
        fields = {"version": "v2.0.0-rc.3", "release_commit": "a"*40,
                  "archive_digest": "sha256:"+"b"*64, "archive_size": 1}
        with self.assertRaisesRegex(stage0.TokenlessStage0Error, "BINDING_MISMATCH"):
            capability.consume(**fields)
        with self.assertRaisesRegex(stage0.TokenlessStage0Error, "CAPABILITY_CONSUMED"):
            capability.consume(**fields)
        with self.assertRaises(TypeError):
            pickle.dumps(capability)

    def test_material_race_revokes_before_failure_for_both_disjoint_types(self):
        for capability_type in (stage0.TestOnlyStage0Release, stage0.VerifiedStage0Release):
            capability = object.__new__(capability_type)
            capability._record, capability._used = b'{}', False
            with self.subTest(capability=capability_type.__name__):
                with self.assertRaisesRegex(bootstrap.BootstrapAuthorityError, "MATERIAL_RACE"):
                    bootstrap._consume_stage0_binding(before=b"x", after=b"y", verified=capability,
                        expected_type=capability_type, tag="v2.0.0-rc.3", release_commit="a"*40)
                self.assertTrue(capability._used)
                with self.assertRaisesRegex(stage0.TokenlessStage0Error, "CAPABILITY_CONSUMED"):
                    capability.consume(version="v2.0.0-rc.3", release_commit="a"*40,
                        archive_digest="sha256:"+"b"*64, archive_size=1)

    def test_plain_test_only_commit_record_is_rejected_by_production_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test-only.json"
            path.write_bytes(b'{"schema":"animemo.test-only-bootstrap-commit/v1","state":"TEST_ONLY","production_authority_granted":false}')
            with self.assertRaises(bootstrap.BootstrapAuthorityError):
                bootstrap._load_record(path)


if __name__ == "__main__":
    unittest.main()
