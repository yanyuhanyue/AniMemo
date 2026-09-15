"""Real bounded pipe tests plus transport/failure contract negative cases."""
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from installer import apt_diagnostics as apt
from installer.platform_bootstrap import SubprocessPlatformCommandRunner, _apt_argv
from scripts import candidate_diagnostics as diagnostics


class AptDiagnosticsTests(unittest.TestCase):
    def run_child(self, source, *, timeout=10):
        return apt.capture_process((sys.executable, '-c', source), timeout=timeout,
                                   environment={'PATH': os.environ.get('PATH', '')})

    def fixture(self, *, stderr=b'', code=100, outcome='EXITED'):
        capture = apt.Capture()
        capture.feed(stderr)
        capture.finish()
        return apt.CapturedProcess(code, outcome, b'', stderr,
            '2026-09-15T10:00:00Z', '2026-09-15T10:00:01Z',
            apt.Capture().public(), capture.public(), ())

    def test_real_separate_pipe_limits_scan_safe_tail_without_persisting_body(self):
        result = self.run_child(
            "import os;"
            "os.write(1,b'x'*(2*1024*1024)+b'\\nTemporary failure resolving archive.ubuntu.com\\n');"
            "os.write(2,b'z'*(2*1024*1024)+b'\\n429 Too Many Requests\\n');"
            "raise SystemExit(100)"
        )
        self.assertEqual(result.returncode, 100)
        self.assertEqual(result.outcome, 'EXITED')
        self.assertEqual(len(result.stdout), apt.STREAM_LIMIT)
        self.assertEqual(len(result.stderr), apt.STREAM_LIMIT)
        self.assertTrue(result.stdout_summary['truncated'])
        self.assertTrue(result.stderr_summary['truncated'])
        self.assertIn('DNS', result.stdout_summary['categories'])
        self.assertIn('RATE_LIMIT', result.stderr_summary['categories'])
        public = apt.apt_observation(_apt_argv('update'), result, '2.8.3')
        diagnostics.validate_apt_observation(public)
        self.assertLess(len(json.dumps(public)), 2048)
        self.assertNotIn('xxxx', json.dumps(public))

    def test_unknown_private_text_and_url_components_never_cross_channel(self):
        private = b'PRIVATE_SENTINEL https://name:password@archive.ubuntu.com/path?token=SECRET InRelease\n'
        value = apt.apt_observation(_apt_argv('update'), self.fixture(stderr=private), '2.8.3')
        self.assertEqual(value['categories'], ['UNKNOWN'])
        self.assertEqual(value['returncode'], 100)
        self.assertEqual(value['stderr']['hosts'], ['archive.ubuntu.com'])
        self.assertEqual(value['stderr']['indexes'], ['InRelease'])
        serialized = json.dumps(value)
        for forbidden in ('PRIVATE_SENTINEL', 'password', 'SECRET', 'token', '/path', 'name:'):
            self.assertNotIn(forbidden, serialized)

    def test_fixed_failure_categories_across_fragmented_streams(self):
        cases = {
            b'NO_PUBKEY secretkey': 'SIGNATURE',
            b'Certificate verification failed': 'CERTIFICATE',
            b'Hash Sum mismatch': 'SOURCE_IDENTITY',
            b'Could not get lock': 'LOCK',
            b'429 Too Many Requests': 'RATE_LIMIT',
            b'Temporary failure resolving': 'DNS',
            b'Connection timed out': 'NETWORK',
            b'No space left on device': 'DISK',
            b'Some index files failed to download. old ones used instead': 'PARTIAL_INDEX',
        }
        for message, expected in cases.items():
            with self.subTest(expected=expected):
                capture = apt.Capture()
                for byte in message + b'\n':
                    capture.feed(bytes([byte]))
                capture.finish()
                self.assertIn(expected, capture.public()['categories'])

    def test_real_timeout_preserves_os_exit_and_output(self):
        result = self.run_child("import os,time;os.write(2,b'unknown private text\\n');time.sleep(30)", timeout=0.2)
        self.assertEqual(result.outcome, 'TIMEOUT')
        self.assertIsInstance(result.returncode, int)
        value = apt.apt_observation(_apt_argv('update'), result, '2.8.3')
        self.assertIn('TIMEOUT', value['categories'])
        self.assertEqual(value['returncode'], result.returncode)

    def test_real_missing_executable_has_no_invented_returncode(self):
        result = apt.capture_process(('this-animemo-test-executable-does-not-exist',),
                                     timeout=1, environment={})
        self.assertIsNone(result.returncode)
        self.assertEqual(result.outcome, 'LAUNCH_FAILED')

    def test_cancellation_is_explicit_and_cleans_owned_process(self):
        process = mock.Mock()
        process.stdout, process.stderr = io.BytesIO(), io.BytesIO()
        process.returncode = -15
        process.wait.side_effect = [KeyboardInterrupt(), -15, -15]
        with mock.patch.object(apt.subprocess, 'Popen', return_value=process), \
                mock.patch.object(apt.os, 'killpg', create=True):
            result = apt.capture_process(('test',), timeout=1, environment={})
        self.assertEqual(result.outcome, 'CANCELLED')
        self.assertEqual(result.returncode, -15)

    def test_actual_runner_to_framed_reader_keeps_primary_and_secondary_error(self):
        operation = 'sha256:' + '1' * 64
        result = replace(self.fixture(stderr=b'Could not get lock'),
                         secondary_errors=('PROCESS_CLEANUP_FAILED',))
        with tempfile.TemporaryFile() as stream:
            writer = diagnostics.DiagnosticWriter(stream.fileno(), operation)
            with mock.patch.object(diagnostics, 'inherited_writer', return_value=writer):
                output = SubprocessPlatformCommandRunner._apt_result(result,
                    apt.apt_observation(_apt_argv('update'), result, '2.8.3'))
            stream.seek(0)
            reader = diagnostics.DiagnosticReader(operation)
            while item := diagnostics.read_frame(stream):
                reader.accept(*item)
        observed = reader.public()['events'][0]['observation']
        self.assertEqual(observed['returncode'], 100)
        self.assertEqual(observed['categories'], ['LOCK'])
        self.assertEqual(output.outcome, 'EXITED')
        self.assertEqual(observed['secondary_errors'], ['PROCESS_CLEANUP_FAILED'])
        self.assertEqual(output.returncode, 100)

    def test_diagnostic_write_failure_does_not_replace_apt_error(self):
        result = self.fixture(stderr=b'Could not get lock')
        writer = mock.Mock()
        writer.event.side_effect = OSError('PRIVATE_EXCEPTION')
        with mock.patch.object(diagnostics, 'inherited_writer', return_value=writer):
            output = SubprocessPlatformCommandRunner._apt_result(result,
                apt.apt_observation(_apt_argv('update'), result, '2.8.3'))
        self.assertEqual(output.returncode, 100)
        self.assertEqual(output.diagnostic_error, 'APT_DIAGNOSTIC_WRITE_FAILED')
        self.assertNotIn('PRIVATE_EXCEPTION', repr(output))

    def test_unknown_tool_version_blocks_before_mutating_apt_command(self):
        result = replace(self.fixture(code=0), stdout=b'apt 99.0.0 (amd64)\n')
        with mock.patch.object(apt, 'capture_process', return_value=result) as capture, \
                mock.patch.object(diagnostics, 'inherited_writer', return_value=None):
            output = SubprocessPlatformCommandRunner().run(_apt_argv('update'), timeout=900, environment={})
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.args[0], ('/usr/bin/apt-get', '--version'))
        self.assertEqual(output.outcome, 'TOOL_VERSION_UNSUPPORTED')
        self.assertIsNone(output.returncode)

    def test_supported_version_uses_strict_full_index_command_once(self):
        probe = replace(self.fixture(code=0), stdout=b'apt 2.8.3 (amd64)\n')
        result = self.fixture(code=0)
        with mock.patch.object(apt, 'capture_process', side_effect=[probe, result]) as capture, \
                mock.patch.object(diagnostics, 'inherited_writer', return_value=None):
            output = SubprocessPlatformCommandRunner().run(_apt_argv('update'), timeout=900, environment={})
        self.assertEqual(capture.call_count, 2)
        self.assertIn('--error-on=any', capture.call_args.args[0])
        self.assertEqual(output.returncode, 0)
        self.assertEqual(output.observation['tool_version'], '2.8.3')

    def test_reader_rejects_injected_text_or_unknown_public_fields(self):
        original = apt.apt_observation(_apt_argv('update'), self.fixture(), '2.8.3')
        for field, replacement in [('body', 'SECRET'), ('categories', ['unreviewed text']),
                                   ('returncode', True), ('ended_at', 'yesterday')]:
            value = dict(original, **{field: replacement})
            with self.subTest(field=field), self.assertRaises(diagnostics.DiagnosticError):
                diagnostics.validate_apt_observation(value)


if __name__ == '__main__':
    unittest.main()
