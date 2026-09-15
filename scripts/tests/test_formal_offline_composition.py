"""Local integration using qualified media; external process/host facts are synthetic.

Use formal_test_materials.selected_materials() in a trusted local test launcher
with independently selected roots and canonical member identities. Optional
ANIMEMO_FORMAL_TEST_* selectors must match that selection. This never
starts a VM, accesses Docker IPC, executes a plan, or emits a Formal receipt.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from installer import bootstrap, formal_bootstrap, production
from installer.platform_bootstrap import PLATFORM_PACKAGE_POLICY
from installer.runtime import InstallTransportSource, InstallerError, explicit_transport_policy
from installer.tests.test_platform_bootstrap import qualified_existing_facts
from release.candidate import canonical_json_bytes
from release.materials import INITIAL_TRUST_KIT_PREFIX
from release.trust_bootstrap import validate_initial_trust_kit
from scripts.formal_profile_runner import _install_request
from scripts.tests.trust_kit_fixture import authority_test_namespace, simulated_test_root_ownership
from scripts.tests.formal_test_materials import snapshot_selected_materials
from updater import __version__
from updater.offline import SigstoreGoEvidenceVerifier
from updater.tests.test_offline import _actions_claim, _release_claim


class SyntheticVerifierProcess:
    """Only the external executable boundary is simulated; claims stay checked."""
    def __init__(self):
        self.calls=[]
        self.wrong_subject=False

    def __call__(self, argv, **kwargs):
        assert tuple(argv[1::2]) == ('--bundle','--trusted-root','--request')
        assert kwargs['capture_output'] and kwargs['check'] is False
        request=json.loads(Path(argv[-1]).read_bytes())
        self.calls.append(request)
        if request['mode']=='github-release':
            claim=_release_claim()
            claim.update(tag=request['tag'],tagCommit=request['tagCommit'],tagObject=request['tagObject'],
                prerelease=True,signedAt='2026-09-15T03:18:03Z')
            claim['assets']=[item for item in request['expectedSubjects'] if not item['name'].endswith('-portable.tar')]
            claim['transportAssets']=[{**item,'role':'PORTABLE_RELEASE_BUNDLE','authorityRole':'TRANSPORT_ONLY'}
                for item in request['expectedSubjects'] if item['name'].endswith('-portable.tar')]
        elif request['mode']=='actions-provenance':
            claim=_actions_claim()
            claim.update(subject={k:v for k,v in request['subject'].items() if k!='size'},workflow=request['workflow'],
                source={'commit':request['sourceCommit'],'ref':'refs/heads/main'},signerDigest=request['sourceCommit'])
            claim['certificate']['identity']='https://github.com/yanyuhanyue/AniMemo/'+request['workflow']+'@refs/heads/main'
            if self.wrong_subject:
                claim['subject']['sha256']='sha256:'+'0'*64
        else:
            raise AssertionError('Unexpected synthetic verifier command')
        return subprocess.CompletedProcess(argv,0,canonical_json_bytes(claim),b'')


class FormalOfflineCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='animemo-formal-local-plan-')
        self.root=Path(self.temporary.name).resolve()
        self.assertEqual(self.root.parent,Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.stack=self.enterContext(ExitStack())
        self.authority_root=self.root/'authority'
        materials=snapshot_selected_materials(self.authority_root)
        if materials is None:
            self.skipTest('No qualified media selected for the optional local composition gate')
        manifest=materials.manifest
        self.version=manifest['release']['version']
        self.payload=materials.payload
        self.sidecar=materials.sidecar
        self.installer_archive=materials.installer_archive
        self.external=SyntheticVerifierProcess()
        self.stack.enter_context(mock.patch.dict(SigstoreGoEvidenceVerifier.__init__.__kwdefaults__,runner=self.external))
        self.stack.enter_context(simulated_test_root_ownership())
        self.stack.enter_context(mock.patch('release.formal_windows_pretrust.assert_windows_private_acl'))
        self.profile=validate_initial_trust_kit(self.authority_root/INITIAL_TRUST_KIT_PREFIX).identity
        self.authority=SimpleNamespace(rc_tag=self.version,source_sha=manifest['release']['commit'],
            repository='yanyuhanyue/AniMemo',offline_release_trust_profile_identity=self.profile,
            installer_materials_identity=manifest['deployment']['installerMaterials']['sha256'])

    def capability(self):
        return production.issue_formal_candidate_bound_offline_verifier(self.authority_root,
            expected_profile_identity=self.profile)

    def composition(self):
        value=production.build_formal_production_composition(offline_verifier_capability=self.capability(),
            local_bundle_payload=self.payload,local_bundle_release_attestation=self.sidecar,
            transport_policy=explicit_transport_policy(InstallTransportSource.LOCAL_BUNDLE))
        self.addCleanup(value.close_formal_authority)
        if value.releases._temporary is not None:
            self.addCleanup(value.releases._temporary.cleanup)
        return value

    def test_stage0_real_composition_and_platform_plan_share_exact_offline_inputs(self):
        verifier,temporary=self.capability()._consume()
        try:
            verified=verifier.verify(payload=self.payload,sidecar=self.sidecar,
                destination=self.root/'expected-release',updater_version=__version__)
            self.authority.publication_identity=verified.publication_identity
            self.assertEqual(len(verified.images.images),4)
        finally:
            temporary.cleanup()
        protected=self.root/'bootstrap';protected.mkdir(mode=0o700)
        archive=protected/bootstrap._MATERIALS_FILE
        shutil.copyfile(self.installer_archive,archive);archive.chmod(0o400)
        self.addCleanup(lambda: archive.chmod(0o600) if archive.exists() else None)
        with authority_test_namespace(protected),mock.patch.object(formal_bootstrap,'BOOTSTRAP_AUTHORITY_ROOT',protected),\
                mock.patch.object(formal_bootstrap,'os',SimpleNamespace(name='posix',geteuid=lambda:0,fstat=os.fstat)):
            archive.chmod(0o400)
            # The root/ownership namespace is simulated; byte and proof
            # orchestration, Stage-0 record and consumption are actual code.
            formal_bootstrap.prepare_offline_stage0(self.authority_root,self.authority)
            composition=self.composition()
            request=_install_request(self.authority,self.authority_root,'FORMAL_OFFLINE')
            calls=[]
            def protected_module_observation(**kwargs):
                self.assertEqual(kwargs,{'version':self.version,'release_commit':self.authority.source_sha})
                authorization=bootstrap.BootstrapPrivilegeGate().consume(**kwargs)
                self.assertEqual(authorization.materials_path,archive)
                self.assertEqual(authorization.materials_sha256,self.authority.installer_materials_identity)
                self.assertEqual(authorization.version,self.version)
                self.assertEqual(authorization.release_commit,self.authority.source_sha)
                calls.append(kwargs)
                return authorization
            facts=qualified_existing_facts(postgres_major=PLATFORM_PACKAGE_POLICY.required_postgres_major,
                installed=tuple(PLATFORM_PACKAGE_POLICY.body()['packageNames']))
            with mock.patch.object(composition.bootstrap_privilege_gate,'verify_runtime_source',side_effect=protected_module_observation),\
                    mock.patch('installer.platform_bootstrap.collect_bootstrap_host_facts',return_value=facts),\
                    mock.patch('installer.bootstrap.authorize_online_stage0',side_effect=AssertionError('No online fallback')):
                session=composition.plan_platform(request,'2026-09-15T08:00:00Z')
            self.assertEqual(session.release.version,self.version)
            self.assertEqual(session.release.commit,self.authority.source_sha)
            self.assertEqual(session.plan.network_policy,'DENY_ALL')
            self.assertEqual(len(calls),1)
            self.assertEqual(composition.releases.latest_materials().installer_archive_sha256,self.authority.installer_materials_identity)
            self.assertGreaterEqual(len(self.external.calls),18)

    def test_missing_local_payload_is_rejected_by_actual_composition(self):
        self.payload.unlink()
        with self.assertRaisesRegex(InstallerError,'INSTALL_LOCAL_BUNDLE_VERIFICATION_FAILED'):
            self.composition()
        self.assertEqual(self.external.calls,[])

    def test_wrong_proof_subject_is_rejected_before_local_plan(self):
        self.external.wrong_subject=True
        with self.assertRaisesRegex(InstallerError,'INSTALL_LOCAL_BUNDLE_VERIFICATION_FAILED'):
            self.composition()
        self.assertTrue(self.external.calls)


if __name__=='__main__':
    unittest.main()
