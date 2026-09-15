from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from installer.runtime import InstallTransportSource, InstallerMode, InstallerError
from release.materials import VerifiedMaterialSet
from scripts.formal_profile_runner import _install_request
from updater.authority import VerifiedReleaseMaterials
from updater.errors import RequestRejected


class FormalProductionSeamsTests(unittest.TestCase):
    def test_real_offline_request_constructor_receives_both_material_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            authority = SimpleNamespace(rc_tag='v2.0.0-rc.2')
            request = _install_request(authority, root, 'FORMAL_OFFLINE')
            self.assertIs(request.mode, InstallerMode.FRESH)
            self.assertIs(request.transport_source, InstallTransportSource.LOCAL_BUNDLE)
            self.assertEqual(request.local_bundle_payload, root/'animemo-v2.0.0-rc.2-portable.tar')
            self.assertEqual(request.local_bundle_release_attestation, root/'release-attestation.sigstore.json')
            self.assertEqual(request.selector.version, authority.rc_tag)

    def test_both_online_profiles_use_fresh_github_without_offline_paths(self):
        for profile in ('FORMAL_FRESH', 'FORMAL_DOCKER'):
            request = _install_request(SimpleNamespace(rc_tag='v2.0.0-rc.2'), Path('/unused'), profile)
            self.assertIs(request.mode, InstallerMode.FRESH)
            self.assertIs(request.transport_source, InstallTransportSource.GITHUB)
            self.assertIsNone(request.local_bundle_payload)
            self.assertIsNone(request.local_bundle_release_attestation)

    def test_relative_offline_path_is_rejected_by_real_constructor(self):
        with self.assertRaises(InstallerError):
            _install_request(SimpleNamespace(rc_tag='v2.0.0-rc.2'), Path('relative'), 'FORMAL_OFFLINE')

    def test_archive_identity_is_not_collection_identity(self):
        archive = 'sha256:'+'a'*64
        collection = 'sha256:'+'b'*64
        materials = VerifiedReleaseMaterials(
            manifest={'deployment':{'installerMaterials':{'sha256':archive}}},
            deployment_contract={'archive':{'sha256':archive}},
            verified=VerifiedMaterialSet(root=Path('/unused'),archive_sha256=archive,files=()),
            identity_digest=collection)
        self.assertEqual(materials.installer_archive_sha256, archive)
        self.assertNotEqual(materials.installer_archive_sha256, materials.identity_digest)
        materials.deployment_contract['archive']['sha256'] = collection
        with self.assertRaises(RequestRejected):
            _ = materials.installer_archive_sha256


if __name__ == '__main__':
    unittest.main()
