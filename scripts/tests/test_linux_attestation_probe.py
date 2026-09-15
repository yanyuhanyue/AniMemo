import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from installer import formal_bootstrap
from release.materials import INITIAL_TRUST_KIT_PREFIX, MaterialContractError, MaterialFileIdentity, VerifiedMaterialSet
from scripts import development_linux_probe as host, linux_attestation_probe as probe


class LinuxProductProbeTests(unittest.TestCase):
    def setup_inputs(self, root):
        original=root/'qualified'; original.mkdir()
        (original/'updater').mkdir()
        data=b'published exact module\n'
        (original/'updater/source.py').write_bytes(data)
        item=MaterialFileIdentity(path='updater/source.py',size=len(data),mode=0o644,
            sha256='sha256:'+hashlib.sha256(data).hexdigest())
        verified=VerifiedMaterialSet(root=original,archive_sha256='sha256:'+'1'*64,files=(item,))
        (original/'release-manifest.json').write_bytes(b'{}\n')
        draft=root/'draft'; draft.mkdir()
        (draft/'updater').mkdir();(draft/'updater/source.py').write_bytes(b'different tool module\n')
        kit=draft/INITIAL_TRUST_KIT_PREFIX;kit.mkdir(parents=True)
        for name in ('github-trusted-root.jsonl','sigstore-trusted-root.jsonl'):
            (kit/name).write_text('{\n  "mediaType": "synthetic-root"\n}\n',encoding='utf-8')
        gh=root/'synthetic-gh';gh.write_bytes(b'\x7fELF synthetic tool')
        sidecar=root/'sidecar.json';sidecar.write_text(json.dumps({'tag':'v2.0.0-rc.2','commit':'a'*40}),encoding='utf-8')
        loaded=SimpleNamespace(root=original,candidate_input={'candidate_version':'v2.0.0-rc.2','source_sha':'a'*40},
            materials=SimpleNamespace(verified=verified,material=verified.material))
        return loaded,draft,gh,sidecar,data

    def test_published_mode_copies_qualified_bytes_and_compacts_only_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded,draft,gh,sidecar,data=self.setup_inputs(Path(directory))
            with mock.patch.object(formal_bootstrap,'GH_EXE_SHA256',hashlib.sha256(gh.read_bytes()).hexdigest()),mock.patch('updater.offline._extract_sigstore_bundle',return_value={'synthetic':'bundle'}):
                additions=probe.prepare_probe_inputs(loaded=loaded,source_sha='b'*40,execution_root=draft,
                    linux_gh=gh,sidecar=sidecar,published_subject=True)
            context=json.loads((draft/'development-probe/context.json').read_bytes())
            self.assertEqual(context['purpose'],'PUBLISHED_PRODUCT_PREFLIGHT_ONLY')
            self.assertEqual(context['parser_sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual((draft/'development-probe/published-product/updater/source.py').read_bytes(),data)
            self.assertIn('development-probe/published-product/updater/source.py',additions)
            self.assertEqual(len((draft/'development-probe/trusted-roots.jsonl').read_bytes().splitlines()),2)
            self.assertEqual(len((draft/INITIAL_TRUST_KIT_PREFIX/'github-trusted-root.jsonl').read_bytes().splitlines()),3)

    def test_qualified_member_change_fails_before_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded,draft,gh,sidecar,_data=self.setup_inputs(Path(directory))
            (loaded.root/'updater/source.py').write_bytes(b'changed')
            with self.assertRaisesRegex(MaterialContractError,'Verified installer material has changed'):
                probe.prepare_probe_inputs(loaded=loaded,source_sha='b'*40,execution_root=draft,
                    linux_gh=gh,sidecar=sidecar,published_subject=True)

    def test_old_scopes_cannot_start_published_probe(self):
        for scope in ('ANIMEMO_V2_RC1_FORMAL_SINGLE_CAPTURE_V1',host.DEVELOPMENT_AUTHORIZATION):
            with self.subTest(scope=scope),mock.patch.object(host.h,'ClosedVmwareProvider') as provider:
                with self.assertRaisesRegex(host.h.CandidateHarnessError,'DEVELOPMENT_LINUX_PROBE_SCOPE_INVALID'):
                    host.run(SimpleNamespace(authorization_id=scope,published_subject=True,output=Path('unused')))
                provider.assert_not_called()


if __name__=='__main__':
    unittest.main()
