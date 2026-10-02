"""Local Stage-0 evidence tests; opt-in real original Release crypto fixtures.

ANIMEMO_TOKENLESS_REAL_FIXTURE names a task-owned JSON input manifest. The real
group executes the fixed verifier, never a fake successful verifier. Its DEV
trust profile binds the supplied Windows binary/root; this does not prove
first trust provisioning, TUF refresh, Linux execution, or clean installation.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from installer import tokenless_stage0 as stage0
from installer.anonymous_release_transport import UntrustedReleaseMaterials
from updater.offline import PretrustedTrustMaterial, TrustProfile

REAL_FIXTURE = 'ANIMEMO_TOKENLESS_REAL_FIXTURE'
CANARY = 'NON_SECRET_PARENT_CANARY_MUST_NOT_PROPAGATE'


def digest(path):
    with Path(path).open('rb') as stream:
        return 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()


class TokenlessStage0BoundaryTests(unittest.TestCase):
    def test_lab_dict_cannot_issue_production_capability(self):
        with self.assertRaises(stage0.TokenlessStage0Error):
            stage0.VerifiedStage0Release(object(), {'authority': 'NON_AUTHORITATIVE_LOCAL_EVIDENCE'})

    def test_production_does_not_accept_lab_trust_or_inputs_arguments(self):
        with self.assertRaises(TypeError):
            stage0.verify_for_production(version='v2.0.0-rc.3', release_commit='a' * 40,
                archive=Path('unused'), material=object(), inputs={})

    @unittest.skipUnless(os.name == 'nt', 'Windows production root rejection')
    def test_windows_production_rejects_before_trust_read_or_fetch(self):
        with mock.patch.object(PretrustedTrustMaterial, 'load') as load, \
                mock.patch('installer.anonymous_release_transport.AnonymousReleaseReader.fetch') as fetch:
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'PRODUCTION_ROOT_REQUIRED'):
                stage0.verify_for_production(version='v2.0.0-rc.3', release_commit='a' * 40,
                    archive=Path('unused'))
            load.assert_not_called()
            fetch.assert_not_called()

    def test_json_duplicate_key_nonfinite_and_budget_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff',
                    b' ' * (stage0._MAX_JSON + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(stage0.TokenlessStage0Error):
                stage0._json(raw)

    def test_held_file_rejects_hardlink_and_size_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original'
            original.write_bytes(b'test bytes')
            with self.assertRaises(stage0.TokenlessStage0Error):
                with stage0._held_file(original, 2):
                    self.fail('oversized file yielded')
            linked = root / 'linked'
            os.link(original, linked)
            with self.assertRaises(stage0.TokenlessStage0Error):
                with stage0._held_file(linked, 100):
                    self.fail('hard link yielded')

    def test_held_file_detects_same_size_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned-file'
            path.write_bytes(b'original')
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'FILE_CHANGED'):
                with stage0._held_file(path, 100):
                    path.write_bytes(b'modified')

    def test_held_file_rejects_symlink_if_host_permits_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.write_bytes(b'owned')
            link = root / 'link'
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest('Host does not permit unprivileged test symlink: ' + type(error).__name__)
            with self.assertRaises(stage0.TokenlessStage0Error):
                with stage0._held_file(link, 100):
                    self.fail('symlink yielded')

    def test_first_fstat_failure_keeps_primary_and_records_close_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file'
            path.write_bytes(b'owned')
            actual_close = os.close
            def fail_close(descriptor):
                actual_close(descriptor)
                raise OSError('close detail must not replace primary')
            with mock.patch.object(stage0.os, 'fstat', side_effect=OSError('primary')), \
                    mock.patch.object(stage0.os, 'close', side_effect=fail_close):
                with self.assertRaisesRegex(OSError, '^primary$') as caught:
                    with stage0._held_file(path, 100):
                        self.fail('opened after failed fstat')
            self.assertEqual(caught.exception.secondary_errors,
                             ('BOOTSTRAP_TOKENLESS_HANDLE_CLOSE_FAILED',))

    def test_growing_file_read_stops_at_original_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file'
            path.write_bytes(b'owned')
            with path.open('rb') as stream:
                actual_read = stream.read
                count = 0
                def grow(size):
                    nonlocal count
                    count += 1
                    result = actual_read(size)
                    if count == 1:
                        with path.open('ab') as output:
                            output.write(b'extra bytes')
                    return result
                with mock.patch.object(stream, 'read', side_effect=grow), \
                        self.assertRaisesRegex(stage0.TokenlessStage0Error, 'FILE_CHANGED'):
                    stage0._file_digest(stream)
                self.assertEqual(count, 2)

    def test_json_depth_cycle_unknown_types_and_observation_fields_fail_closed(self):
        cycle = []
        cycle.append(cycle)
        deep = value = []
        for _ in range(66):
            nested = []
            value.append(nested)
            value = nested
        for value in (cycle, deep, object(), {1: 'value'}):
            with self.assertRaises(stage0.TokenlessStage0Error):
                stage0._canonical(value)
        for value in ([{}], ({'signed_url': CANARY},), tuple({} for _ in range(27))):
            with self.assertRaises(stage0.TokenlessStage0Error):
                stage0._closed_observations(value)

    def test_control_file_readback_uses_fixed_length_and_rejects_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.json'
            expected = b'{"fixed":true}'
            path.write_bytes(expected)
            stage0._readback_equal(path, expected)
            path.write_bytes(expected + b' ')
            with self.assertRaises(stage0.TokenlessStage0Error):
                stage0._readback_equal(path, expected)


@unittest.skipUnless(os.environ.get(REAL_FIXTURE), 'Explicit real public fixture manifest required')
class TokenlessStage0RealCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(Path(os.environ[REAL_FIXTURE]).read_bytes())
        if cls.fixture['authority'] != 'NON_AUTHORITATIVE_LOCAL_COMPONENT_FIXTURE':
            raise ValueError('Explicit DEV fixture marker required')
        f = cls.fixture
        original_profile = TrustProfile.from_bootstrap_record(json.loads(Path(f['profile_path']).read_bytes()))
        # Component trust only: independently recorded original Q tool bytes.
        profile = replace(original_profile, verifier_identity=digest(f['verifier_path']),
            github_trusted_root_sha256=digest(f['github_root_path']))
        cls.material = PretrustedTrustMaterial(root=Path(f['profile_path']).parent, profile=profile,
            verifier_path=Path(f['verifier_path']), github_trusted_root_path=Path(f['github_root_path']),
            sigstore_trusted_root_path=Path(f['sigstore_root_path']),
            github_tuf_root_path=Path(f['github_tuf_root_path']),
            sigstore_tuf_root_path=Path(f['sigstore_tuf_root_path']))
        if f.get('live_inputs_path'):
            live = json.loads(Path(f['live_inputs_path']).read_bytes())
            cls.inputs = UntrustedReleaseMaterials(version=live['version'],
                release_metadata=live['release_metadata'], tag_object=live['tag_object'],
                tag_commit=live['tag_commit'], release_bundle=base64.b64decode(live['release_bundle'], validate=True),
                observations=tuple(live['observations']))
        else:
            cls.inputs = UntrustedReleaseMaterials(version=f['version'],
                release_metadata=json.loads(Path(f['release_metadata_path']).read_bytes()),
                tag_object=f['tag_object'], tag_commit=f['tag_commit'],
                release_bundle=Path(f['release_bundle_path']).read_bytes(),
                observations=tuple(f.get('observations', [])))
        cls.archive = Path(f['archive_path'])
        cls.scratch = Path(f['scratch_parent'])

    def verify(self, **changes):
        options = dict(material=self.material, inputs=self.inputs, archive=self.archive,
            expected_commit=self.inputs.tag_commit, scratch_parent=self.scratch)
        options.update(changes)
        return stage0.verify_release_locally(**options)

    def mutated_bundle(self, mutate):
        value = json.loads(self.inputs.release_bundle)
        mutate(value)
        return replace(self.inputs, release_bundle=stage0._canonical(value))

    def mutated_statement(self, mutate):
        def change(bundle):
            envelope = bundle['dsseEnvelope']
            value = json.loads(base64.b64decode(envelope['payload']))
            mutate(value)
            envelope['payload'] = base64.b64encode(stage0._canonical(value)).decode('ascii')
        return self.mutated_bundle(change)

    def test_real_original_release_has_crypto_verified_non_authoritative_result(self):
        observed = self.verify()
        self.assertEqual(observed['authority'], 'NON_AUTHORITATIVE_LOCAL_EVIDENCE')
        self.assertEqual(observed['tag'], self.inputs.version)
        self.assertEqual(observed['tag_object'], self.inputs.tag_object)
        self.assertEqual(observed['observed_commit'], self.inputs.tag_commit)
        self.assertEqual(observed['installer_sha256'], digest(self.archive))
        with self.assertRaises(stage0.TokenlessStage0Error):
            stage0.VerifiedStage0Release(object(), observed)
        self.assertNotIsInstance(observed, stage0.VerifiedStage0Release)

    def test_real_verifier_receives_no_parent_credential_canary(self):
        actual = stage0.capture_process
        observed_environments = []
        def run(argv, **kwargs):
            environment = kwargs['environment']
            observed_environments.append(dict(environment))
            self.assertNotIn(CANARY, repr(environment))
            for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'HTTP_PROXY',
                        'HTTPS_PROXY', 'ALL_PROXY', 'COOKIE', 'AUTHORIZATION'):
                self.assertNotIn(key, environment)
            return actual(argv, **kwargs)
        synthetic_environment = {key: CANARY for key in ('GH_TOKEN', 'GITHUB_TOKEN',
            'GH_ENTERPRISE_TOKEN', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'GH_CONFIG_DIR')}
        if os.name == 'nt':
            synthetic_environment['SystemRoot'] = 'C:\\Windows'
        with mock.patch.object(os, 'environ', synthetic_environment), \
                mock.patch.object(stage0, 'capture_process', side_effect=run):
            self.verify()
        self.assertEqual(len(observed_environments), 1)

    def test_real_verifier_rejects_corrupted_signature(self):
        def corrupt(value):
            signature = value['dsseEnvelope']['signatures'][0]
            raw = bytearray(base64.b64decode(signature['sig']))
            raw[-1] ^= 1
            signature['sig'] = base64.b64encode(raw).decode('ascii')
        with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED'):
            self.verify(inputs=self.mutated_bundle(corrupt))

    def test_real_verifier_rejects_modified_signed_identity_fields(self):
        # These alter real signed payloads without re-signing: prove tamper
        # rejection, not isolated policy rejection under a new valid signature.
        cases = {
            'predicate': lambda s: s.update(predicateType='https://slsa.dev/provenance/v1'),
            'repository': lambda s: s['predicate'].update(repository='other/repository'),
            'owner': lambda s: s['predicate'].update(ownerId='1'),
            'tag': lambda s: s['predicate'].update(tag='v9.9.9-rc.1'),
            'databaseId': lambda s: s['predicate'].update(databaseId='1'),
            'subject': lambda s: s['subject'][0]['digest'].update(sha1='0' * 40),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case), self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED'):
                self.verify(inputs=self.mutated_statement(mutate))

    def test_real_verifier_rejects_corrupted_certificate_san(self):
        def corrupt(value):
            certificate = value['verificationMaterial']['certificate']
            raw = base64.b64decode(certificate['rawBytes'])
            self.assertIn(b'dotcom.releases.github.com', raw)
            raw = raw.replace(b'dotcom.releases.github.com', b'dotcom.invalids.github.com')
            certificate['rawBytes'] = base64.b64encode(raw).decode('ascii')
        # SAN mutation also invalidates certificate signature; isolated SAN
        # policy correctness remains covered by the verifier's signed fixtures.
        with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED'):
            self.verify(inputs=self.mutated_bundle(corrupt))

    def test_real_verifier_rejects_missing_timestamp_and_duplicate_signatures(self):
        cases = (
            lambda b: b['verificationMaterial'].pop('timestampVerificationData'),
            lambda b: b['dsseEnvelope']['signatures'].append(copy.deepcopy(b['dsseEnvelope']['signatures'][0])),
        )
        for mutate in cases:
            with self.subTest(mutation=cases.index(mutate)), self.assertRaisesRegex(
                    stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED'):
                self.verify(inputs=self.mutated_bundle(mutate))

    def test_real_valid_signature_rejects_wrong_tag_object_and_tag(self):
        metadata = copy.deepcopy(self.inputs.release_metadata)
        metadata['tag_name'] = 'v9.9.9-rc.1'
        cases = (
            replace(self.inputs, tag_object='0' * 40),
            replace(self.inputs, version='v9.9.9-rc.1', release_metadata=metadata),
        )
        for value in cases:
            with self.subTest(tag=value.version), self.assertRaises(stage0.TokenlessStage0Error):
                self.verify(inputs=value)

    def test_real_valid_signature_rejects_wrong_observed_database_id(self):
        metadata = copy.deepcopy(self.inputs.release_metadata)
        metadata['id'] += 1
        with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'IDENTITY_MISMATCH'):
            self.verify(inputs=replace(self.inputs, release_metadata=metadata))

    def test_expected_commit_and_metadata_asset_identity_fail_closed(self):
        with self.assertRaises(stage0.TokenlessStage0Error):
            self.verify(expected_commit='0' * 40)
        for field, replacement in (('digest', 'sha256:' + '0' * 64), ('size', 1), ('name', 'other.tar')):
            metadata = copy.deepcopy(self.inputs.release_metadata)
            asset = next(v for v in metadata['assets'] if v['name'] == 'installer-materials.tar')
            asset[field] = replacement
            with self.subTest(field=field), self.assertRaises(stage0.TokenlessStage0Error):
                self.verify(inputs=replace(self.inputs, release_metadata=metadata))

    def test_real_verifier_rejects_unknown_root_even_with_matching_dev_hash(self):
        with tempfile.TemporaryDirectory(dir=self.scratch) as directory:
            root = Path(directory) / 'unknown-root.json'
            # Existing real Sigstore-public root is the wrong GitHub domain.
            root.write_bytes(self.material.sigstore_trusted_root_path.read_bytes())
            material = replace(self.material, github_trusted_root_path=root,
                profile=replace(self.material.profile, github_trusted_root_sha256=digest(root)))
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED'):
                self.verify(material=material)

    def test_bound_verifier_or_root_hash_mismatch_fails_before_process(self):
        for field in ('verifier_identity', 'github_trusted_root_sha256'):
            material = replace(self.material, profile=replace(self.material.profile, **{field: 'sha256:' + '0' * 64}))
            with self.subTest(field=field), mock.patch('installer.apt_diagnostics.subprocess.Popen') as launch:
                with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'TRUST_CHANGED'):
                    self.verify(material=material)
                launch.assert_not_called()

    def test_real_profile_policy_rejection_is_fixed_before_process(self):
        material = replace(self.material, profile=replace(self.material.profile,
            github_release_certificate_identity='https://' + CANARY + '.invalid'))
        with mock.patch('installer.apt_diagnostics.subprocess.Popen') as launch:
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'TRUST_REQUIRED') as caught:
                self.verify(material=material)
            self.assertNotIn(CANARY, str(caught.exception))
            self.assertEqual(caught.exception.secondary_errors, ())
            launch.assert_not_called()

    def test_real_verification_detects_archive_change_before_use(self):
        with tempfile.TemporaryDirectory(dir=self.scratch) as directory:
            archive = Path(directory) / 'installer-materials.tar'
            shutil.copyfile(self.archive, archive)
            actual = stage0.capture_process
            def change_after_process(argv, **kwargs):
                result = actual(argv, **kwargs)
                with archive.open('r+b') as output:
                    output.write(b'X')
                return result
            with mock.patch.object(stage0, 'capture_process', side_effect=change_after_process), \
                    self.assertRaisesRegex(stage0.TokenlessStage0Error, 'FILE_CHANGED'):
                self.verify(archive=archive)

    def test_same_asset_bytes_with_wrong_local_filename_rejected(self):
        with tempfile.TemporaryDirectory(dir=self.scratch) as directory:
            archive = Path(directory) / 'portable-role-confusion.tar'
            shutil.copyfile(self.archive, archive)
            with self.assertRaises(stage0.TokenlessStage0Error):
                self.verify(archive=archive)

    def test_oversized_bundle_rejected_before_verifier_process(self):
        inputs = replace(self.inputs, release_bundle=b' ' * (stage0._MAX_JSON + 1))
        with mock.patch('installer.apt_diagnostics.subprocess.Popen') as launch:
            with self.assertRaises(stage0.TokenlessStage0Error):
                self.verify(inputs=inputs)
            launch.assert_not_called()

    def test_launch_timeout_cancel_have_fixed_codes_without_private_text(self):
        cases = ((OSError(CANARY), 'PROCESS_FAILED'),
                 (subprocess.TimeoutExpired(CANARY, 60), 'TIMEOUT'),
                 (KeyboardInterrupt(), 'CANCELLED'))
        for failure, expected in cases:
            with self.subTest(expected=expected), mock.patch('installer.apt_diagnostics.subprocess.Popen',
                    side_effect=failure):
                with self.assertRaisesRegex(stage0.TokenlessStage0Error, expected) as raised:
                    self.verify()
                self.assertNotIn(CANARY, str(raised.exception))

    def test_real_child_oversized_or_invalid_output_is_bounded_and_rejected(self):
        actual_launch = subprocess.Popen
        children = []
        for program in ("import sys;sys.stdout.buffer.write(b'x' * (1024 * 1024))",
                        "import sys;sys.stdout.buffer.write(b'{}\\n');sys.stderr.write('private')"):
            def fixture_process(argv, **kwargs):
                # capture_process may explicitly bind a long Windows verifier
                # path; this injected child must bind its Python executable too.
                kwargs['executable'] = sys.executable
                child = actual_launch([sys.executable, '-I', '-B', '-c', program], **kwargs)
                children.append(child)
                return child
            # Replace only OS execution with a real failing-output child. It
            # cannot claim successful verification or issue an authority.
            with self.subTest(program=program), \
                    mock.patch('installer.apt_diagnostics.subprocess.Popen', side_effect=fixture_process):
                with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'OUTPUT_INVALID'):
                    self.verify()
            self.assertIsNotNone(children[-1].poll())
            self.assertTrue(children[-1].stdout.closed)
            self.assertTrue(children[-1].stderr.closed)

    def test_cleanup_failure_is_secondary_to_original_error(self):
        created = None
        cleanup_target = ('bootstrap_kit.safe_files.remove_owned_directory' if os.name == 'nt'
                          else 'installer.tokenless_stage0.shutil.rmtree')
        try:
            with mock.patch(cleanup_target, side_effect=OSError(CANARY)) as cleanup:
                with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'SIGNATURE_REJECTED') as caught:
                    with stage0._scratch(self.scratch) as created:
                        raise stage0.TokenlessStage0Error('BOOTSTRAP_TOKENLESS_SIGNATURE_REJECTED')
            self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(caught.exception.secondary_errors, ('BOOTSTRAP_TOKENLESS_CLEANUP_FAILED',))
            self.assertNotIn(CANARY, str(caught.exception))
        finally:
            if created is not None:
                self.assertEqual(created.parent, self.scratch)
                if os.name == 'nt':
                    from bootstrap_kit.safe_files import directory_identity, remove_owned_directory
                    remove_owned_directory(created, directory_identity(created))
                else:
                    shutil.rmtree(created)

    def test_timeout_with_real_process_cleanup_error_retains_both_codes(self):
        actual_launch = subprocess.Popen
        children = []
        def launch(_argv, **kwargs):
            kwargs['executable'] = sys.executable
            child = actual_launch([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(30)'], **kwargs)
            children.append(child)
            actual_wait, actual_kill = child.wait, child.kill
            attempts = 0
            child.wait = lambda timeout=None: actual_wait(timeout=0.02)
            def kill():
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError(CANARY)
                return actual_kill()
            child.kill = kill
            return child
        with mock.patch('installer.apt_diagnostics.subprocess.Popen', side_effect=launch):
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'VERIFIER_TIMEOUT') as caught:
                self.verify()
        self.assertIn('BOOTSTRAP_TOKENLESS_PROCESS_CLEANUP_FAILED', caught.exception.secondary_errors)
        self.assertIsNotNone(children[0].poll())

    def test_real_crypto_to_bootstrap_with_explicit_test_os_and_http_boundaries(self):
        from installer import anonymous_release_transport as transport
        from installer import bootstrap
        # Fixed public bytes replay at the supervised HTTP client result seam. The reader,
        # production verifier, one-use consume, and bootstrap ordering are real.
        values = [self.inputs.release_metadata,
            {'ref': 'refs/tags/' + self.inputs.version, 'object': {'type': 'tag',
                'sha': self.inputs.tag_object, 'url': f'https://api.github.com{transport.BASE}/git/tags/{self.inputs.tag_object}'}},
            {'sha': self.inputs.tag_object, 'tag': self.inputs.version, 'object': {'type': 'commit',
                'sha': self.inputs.tag_commit, 'url': f'https://api.github.com{transport.BASE}/git/commits/{self.inputs.tag_commit}'}},
            {'attestations': [{'repository_id': transport.REPOSITORY_ID, 'initiator': 'github',
                              'bundle': json.loads(self.inputs.release_bundle)}]}]
        from installer.tests.test_anonymous_release_transport import Network, Response
        client = Network([Response(body=transport._canonical(value)) for value in values])
        with tempfile.TemporaryDirectory(dir=self.scratch) as directory:
            trust = Path(directory)
            profile = self.material.profile
            (trust / 'trust-profile.json').write_bytes(json.dumps(profile.as_bootstrap_record(),
                ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode())
            for name, source in (
                ('offline-release-verifier', self.material.verifier_path),
                ('github-trusted-root.jsonl', self.material.github_trusted_root_path),
                ('sigstore-trusted-root.jsonl', self.material.sigstore_trusted_root_path),
                ('github-tuf-root.json', self.material.github_tuf_root_path),
                ('sigstore-tuf-root.json', self.material.sigstore_tuf_root_path),
            ):
                shutil.copyfile(source, trust / name)
            actual_chain, actual_hold = stage0._safe_chain, stage0._held_file
            @contextmanager
            def user_owned_file(path, maximum, *, production=False):
                # Only the OS owner/root rule is adapted on Windows.
                with actual_hold(path, maximum, production=False) as stream:
                    yield stream
            captures = []
            def test_commit(value):
                captures.append(value)
                return {'schema': 'animemo.test-only-stage0/v1', 'state': 'TEST_ONLY',
                        'production_authority_granted': False}
            actual_consume = stage0.VerifiedStage0Release.consume
            consumed = []
            def consume(capability, **kwargs):
                result = actual_consume(capability, **kwargs)
                consumed.append(capability)
                return result
            with mock.patch.object(stage0, '_production_host_allowed', return_value=True), \
                    mock.patch.object(stage0, 'OPERATOR_TRUST_ROOT', trust), \
                    mock.patch.object(stage0, '_safe_chain', side_effect=lambda path, **kw: actual_chain(path)), \
                    mock.patch.object(stage0, '_held_file', side_effect=user_owned_file), \
                    mock.patch.object(stage0.tempfile, 'gettempdir', return_value=str(self.scratch)), \
                    mock.patch('bootstrap_kit.http_supervisor.SupervisedAnonymousHttp', return_value=client), \
                    mock.patch.object(bootstrap, 'BOOTSTRAP_AUTHORITY_ROOT', self.archive.parent), \
                    mock.patch.object(bootstrap, 'commit_bootstrap_authorization', side_effect=test_commit), \
                    mock.patch.object(bootstrap, '_protected_material_bytes', wraps=bootstrap._protected_material_bytes) as read, \
                    mock.patch.object(stage0.VerifiedStage0Release, 'consume', new=consume):
                result = bootstrap.authorize_online_stage0(tag=self.inputs.version,
                    release_commit=self.inputs.tag_commit, verified_at='2026-09-20T02:00:00Z')
            self.assertEqual(result['state'], 'TEST_ONLY')
            self.assertFalse(result['production_authority_granted'])
            self.assertEqual(read.call_count, 2)
            self.assertEqual(len(consumed), 1)
            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0]['installerMaterials']['sha256'], digest(self.archive))
            self.assertEqual(captures[0]['stage0']['carrier'], stage0.CARRIER)
            self.assertEqual(len(client.requests), 4)
            self.assertFalse(client.responses)
            self.assertTrue(all(result.closed for result in client.connections))
            rejected = trust / 'test-only-result.json'
            rejected.write_bytes(stage0._canonical(result))
            with self.assertRaises(bootstrap.BootstrapAuthorityError):
                bootstrap._load_record(rejected)
            with self.assertRaisesRegex(stage0.TokenlessStage0Error, 'CAPABILITY_CONSUMED'):
                consumed[0].consume(version=self.inputs.version, release_commit=self.inputs.tag_commit,
                    archive_digest=digest(self.archive), archive_size=self.archive.stat().st_size)


if __name__ == '__main__':
    unittest.main()
