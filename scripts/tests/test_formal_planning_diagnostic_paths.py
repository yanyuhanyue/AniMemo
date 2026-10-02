"""Current planning diagnostics without pretending tokenless crypto succeeded.

The immutable Release metadata negative retains the complete Formal caller path. The
three adjacent negatives exercise Stage0 refusal, read-only source checking,
and platform fact collection directly; they do not prove those stages can be
reached after real production cryptographic verification. All four use real
framing and the host reader. No privileged capability or crypto PASS is forged.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
OPERATION = 'sha256:' + 'd' * 64
SENTINEL = 'PRIVATE_PLANNING_SENTINEL_NOT_PUBLIC'


def _child(case: str, directory: str) -> int:
    """Run in a new interpreter so protected imports have their actual paths."""
    from unittest import mock

    scratch = Path(directory).resolve()
    protected = scratch / 'bootstrap'
    runtime = protected / 'materials'
    runtime.mkdir(parents=True, mode=0o700)
    # Real source bytes at actual protected paths, with a real matching archive.
    # This avoids mocking the source gate or constructing an opaque capability.
    with tarfile.open(protected / 'installer-materials.tar', 'w:') as archive:
        for package in ('durability', 'installer', 'release', 'updater', 'bootstrap_kit'):
            for original in sorted((ROOT / package).rglob('*')):
                if not original.is_file() or original.suffix not in {'.py', '.json'}:
                    continue
                relative = original.relative_to(ROOT)
                data = original.read_bytes()
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(data)
                target.chmod(0o600)
                if not data:
                    continue  # Empty package markers are not runtime module members.
                member = tarfile.TarInfo(relative.as_posix())
                member.size = len(data)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(data))
    if case == 'bootstrap-source':
        (runtime / 'installer' / 'cli.py').write_bytes(
            b'# ' + SENTINEL.encode() + b'\n')
    sys.path.insert(0, str(runtime))

    from scripts import formal_profile_runner as formal
    from scripts.candidate_diagnostics import DiagnosticWriter, FD_ENV, OP_ENV, best_effort_fault
    from scripts.tests.test_formal_profile_runner import FormalProfileRunnerTests
    from updater.tests.test_source import FakePublicRest, stable_manifest

    authority_root = scratch / 'authority'
    authority_root.mkdir()
    fixture = FormalProfileRunnerTests()
    request = fixture.authority()  # Request data only, never Verified authority.
    _, identity = fixture.stage(authority_root, request)
    manifest = stable_manifest()
    manifest['release']['version'] = request.rc_tag
    manifest['release']['channel'] = 'rc'
    manifest['release']['promotedFrom'] = None
    manifest['releaseNotes']['tag'] = request.rc_tag
    manifest['provenance']['workflow'] = '.github/workflows/release.yml'
    rest = FakePublicRest(manifest)
    # Deliberate anonymous authority metadata failure, before any asset or
    # verifier execution. Other identity fields satisfy the new DTO shape.
    rest.exact_release.update(id=392113678, immutable=case != 'release-materials')
    for index, asset in enumerate(rest.exact_release['assets'], 1):
        asset.update(id=index, digest='sha256:' + '1' * 64)
    observed = []

    def gh_output(_runner, argv, **kwargs):
        observed.append(['release-gh', list(argv[:3])])
        raise AssertionError('Anonymous planning negatives must never fall back to gh')

    def rest_output(_rest, path, *, label):
        observed.append(['release-http', path])
        return rest.get_json(path, label=label)

    def platform_process(argv, **kwargs):
        observed.append(['platform-process', list(argv)])
        if case != 'platform-facts' or argv != ['/usr/bin/dpkg', '--print-architecture']:
            raise AssertionError('unexpected platform process')
        # The genuine command adapter/collector/plan stack receives this error.
        raise OSError(SENTINEL + ' ' + directory)

    writer = DiagnosticWriter(1, OPERATION)
    with (
        mock.patch.dict(os.environ, {
            FD_ENV: '1', OP_ENV: OPERATION,
            formal.CONTEXT_ENV: fixture.context('FORMAL_FRESH', identity),
        }, clear=True),
        mock.patch('installer.bootstrap.BOOTSTRAP_AUTHORITY_ROOT', protected),
        mock.patch('updater.commands.CommandRunner.run', new=gh_output),
        mock.patch('updater.source.AnonymousGitHubRest.get_json', new=rest_output),
        mock.patch('installer.tokenless_stage0.OPERATOR_TRUST_ROOT', scratch / 'absent-operator-trust'),
        mock.patch('installer.apt_diagnostics.subprocess.Popen', side_effect=platform_process),
    ):
        writer.stage('ROOT_STARTED')
        writer.stage('RUNTIME_READY')
        writer.stage('RUNNER_STARTED')
        if case == 'release-materials':
            code = formal.main(['--authority-root', str(authority_root),
                                '--profile', 'FORMAL_FRESH', '--execute'])
        else:
            writer.stage('PLATFORM_PREPARING')
            try:
                if case == 'online-stage0':
                    from installer.bootstrap import authorize_online_stage0
                    observed.append(['direct-stage0-refusal'])
                    authorize_online_stage0(tag=request.rc_tag, release_commit=request.source_sha,
                                           verified_at='2026-09-20T00:00:00Z')
                elif case == 'bootstrap-source':
                    from installer.bootstrap import _validate_protected_runtime_sources, _REQUIRED_RUNTIME_MODULES
                    observed.append(['direct-source-negative'])
                    # A read-only helper receives only a fixture archive path,
                    # not an issued AuthorizedBootstrap or verified capability.
                    _validate_protected_runtime_sources(SimpleNamespace(materials_path=protected/'installer-materials.tar'),
                        module_files={name: runtime/(name.replace('.', '/')+'.py') for name in _REQUIRED_RUNTIME_MODULES})
                elif case == 'platform-facts':
                    from installer.platform_bootstrap import ProductionPlatformBootstrap
                    from installer.runtime import InstallTransportSource
                    observed.append(['direct-platform-negative'])
                    ProductionPlatformBootstrap().plan(transport_source=InstallTransportSource.GITHUB)
                else:
                    raise AssertionError('Unknown test case')
            except Exception as error:  # noqa: BLE001 - exercise the runner's exception-to-fault bridge for each negative case.
                observed.append(['fixed-error-code', getattr(error, 'code', type(error).__name__)])
                best_effort_fault(error)
                code = 2
            else:
                raise AssertionError('Negative probe unexpectedly completed')
        writer.exited('RUNTIME_RUNNER', code)
        writer.error('ROOT_EXECUTION_FAILED')
        writer.exited('ROOT', code)
        writer.exited('SUDO', code)
    (scratch / 'observed.json').write_text(json.dumps(observed), encoding='utf-8')
    return code


@unittest.skipIf(os.name == 'posix' and os.geteuid() != 0,
                 'Real protected source checks require root ownership on POSIX')
class FormalPlanningDiagnosticPathsTests(unittest.TestCase):
    def _failure(self, case):
        from scripts import candidate_guest_session as session

        with tempfile.TemporaryDirectory() as directory:
            program = ('import sys;sys.path.insert(0,' + repr(str(ROOT)) + ');'
                       'from scripts.tests.test_formal_planning_diagnostic_paths import _child;'
                       'raise SystemExit(_child(' + repr(case) + ',' + repr(directory) + '))')
            process = subprocess.Popen([sys.executable, '-B', '-c', program], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            provider = SimpleNamespace(_candidate_diagnostics={})
            try:
                with self.assertRaises(session.WorkloadFailure) as caught:
                    session._read_receipt(process, operation=OPERATION, provider=provider,
                        profile=SimpleNamespace(profile='FRESH_BASE'), timeout=45)
                self.assertTrue(caught.exception.revoke_batch)
                self.assertEqual(process.wait(timeout=10), 2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
            public = provider._candidate_diagnostics['FRESH_BASE']
            record = {'result': 'ERROR', 'workload_diagnostic': public}
            self.assertEqual(public['failure_diagnostic']['status'], 'COMPLETE', public)
            self.assertEqual(public['last_stage'], 'PLATFORM_PREPARING')
            self.assertIsNone(public['exit_codes']['INSTALLER'])
            self.assertFalse(public['profile_draft_received'])
            serialized = json.dumps(record)
            for private in (directory, str(ROOT), SENTINEL, 'locals', 'Traceback'):
                self.assertNotIn(private, serialized)
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for item in value.values():
                        yield from strings(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from strings(item)
            for value in strings(record):
                for private in (directory, str(ROOT), SENTINEL):
                    self.assertNotIn(private, value)
            faults = [event for event in public['events'] if event['kind'] == 'FAULT']
            self.assertTrue(faults)
            self.assertTrue(all(set(event) == {
                'schema', 'operation', 'kind', 'module', 'line', 'category'
            } for event in faults), faults)
            observed = json.loads((Path(directory) / 'observed.json').read_text())
            return faults, observed

    def test_release_material_verification_failure(self):
        from scripts.tests.test_formal_profile_runner import FormalProfileRunnerTests
        faults, observed = self._failure('release-materials')
        self.assertTrue(any(f['module'] == 'updater.source' for f in faults), faults)
        self.assertEqual(observed, [['release-http',
            '/repos/yanyuhanyue/AniMemo/releases/tags/' + FormalProfileRunnerTests().authority().rc_tag]])
        lines = (ROOT / 'updater/source.py').read_text(encoding='utf-8').splitlines()
        rejected_line = next(index for index, line in enumerate(lines, 1)
            if 'raise RequestRejected("Exact immutable public Release ID is required")' in line)
        self.assertTrue(any(f['module'] == 'updater.source' and f['line'] == rejected_line
                            for f in faults), faults)
        self.assertFalse(any(call[0] == 'release-gh' for call in observed))
        self.assertFalse(any(call[0] in {'stage0-process', 'platform-process'} for call in observed))

    def test_online_stage0_without_independent_trust_fails_before_verification(self):
        faults, observed = self._failure('online-stage0')
        self.assertTrue(any(f['module'] in {'installer.bootstrap', 'installer.tokenless_stage0'}
                            and f['category'] in {'BootstrapAuthorityError', 'TokenlessStage0Error'}
                            for f in faults), faults)
        self.assertEqual(observed[0], ['direct-stage0-refusal'])
        self.assertIn(observed[-1][1], {'BOOTSTRAP_TOKENLESS_PRODUCTION_ROOT_REQUIRED',
                                       'BOOTSTRAP_TOKENLESS_INDEPENDENT_TRUST_REQUIRED'})
        self.assertFalse(any(call[0] in {'release-gh', 'release-http'} for call in observed))
        self.assertFalse(any(call[0] == 'platform-process' for call in observed))

    def test_bootstrap_source_file_tampering(self):
        faults, observed = self._failure('bootstrap-source')
        self.assertTrue(any(f['module'] == 'installer.bootstrap'
                            and f['category'] == 'BootstrapAuthorityError' for f in faults), faults)
        # Assert the intended identity mismatch, not an earlier archive/setup
        # error which would happen to use the same exception class.
        lines = (ROOT / 'installer' / 'bootstrap.py').read_text(encoding='utf-8').splitlines()
        mismatch_line = next(index for index, line in enumerate(lines, 1)
                             if '_reject("BOOTSTRAP_RUNTIME_SOURCE_IDENTITY_MISMATCH")' in line)
        self.assertTrue(any(f['module'] == 'installer.bootstrap' and f['line'] == mismatch_line
                            for f in faults), faults)
        self.assertEqual(observed[0], ['direct-source-negative'])
        self.assertEqual(observed[-1], ['fixed-error-code', 'BOOTSTRAP_RUNTIME_SOURCE_IDENTITY_MISMATCH'])
        self.assertFalse(any(call[0] in {'stage0-process', 'release-gh', 'release-http'} for call in observed))
        self.assertFalse(any(call[0] == 'platform-process' for call in observed))

    def test_platform_fact_process_failure(self):
        faults, observed = self._failure('platform-facts')
        self.assertTrue(any(f['module'] == 'installer.platform_bootstrap'
                            and f['category'] == 'PlatformBootstrapError' for f in faults), faults)
        self.assertEqual(observed[0], ['direct-platform-negative'])
        self.assertIn(['platform-process', ['/usr/bin/dpkg', '--print-architecture']], observed)
        self.assertFalse(any(call[0] in {'stage0-process', 'release-gh', 'release-http'} for call in observed))


if __name__ == '__main__':
    unittest.main()
