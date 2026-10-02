"""CLI boundary tests with in-memory journal observations only."""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from release import cli, frozen_occupancy
from release.publication_transaction import PublicationTransactionError
from release.r2_prestate import ACCESS_KEY_ENV, SECRET_KEY_ENV, SESSION_TOKEN_ENV

ARGV = ['resolve-version', '--tags-file', 'unused-tags.txt',
        '--publication-reservations-file', 'unused-reservations.json',
        '--bump', 'minor', '--channel', 'rc', '--github-output', 'unused-output.txt']


class ReleaseCliTransactionBoundaryTests(unittest.TestCase):
    def invoke_frozen(self, error):
        config = {'schemaVersion': 1, 'reservations': [{'kind': 'synthetic-frozen-record'}]}
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.object(cli, '_read_json', return_value=config),
              mock.patch.object(frozen_occupancy, 'frozen_records', return_value=config['reservations']),
              mock.patch.object(frozen_occupancy, 'verify_live', side_effect=error) as verify,
              mock.patch.object(cli, '_read_tags') as tags,
              mock.patch.object(cli, 'resolve_prerelease') as resolve,
              mock.patch.object(cli, '_write_outputs') as outputs,
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            status = cli.main(ARGV)
        verify.assert_called_once_with(config, Path.cwd())
        tags.assert_not_called()
        resolve.assert_not_called()
        outputs.assert_not_called()
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), '')
        return json.loads(stderr.getvalue())

    def test_journal_unknown_and_global_freeze_are_json_exit_two_without_resolution(self):
        for code in ('TRANSACTION_JOURNAL_READBACK_UNKNOWN', 'TRANSACTION_GLOBAL_FREEZE'):
            with self.subTest(code=code):
                payload = self.invoke_frozen(PublicationTransactionError(code))
                self.assertEqual(payload, {'code': code, 'detail': code})

    def test_transaction_diagnostic_redacts_credentials_and_signed_request_fields(self):
        environment = {ACCESS_KEY_ENV: 'synthetic-access-value',
                       SECRET_KEY_ENV: 'synthetic-secret-value',
                       SESSION_TOKEN_ENV: 'synthetic-session-value'}
        error = PublicationTransactionError('TRANSACTION_JOURNAL_READBACK_UNKNOWN')
        detail = ('synthetic-access-value synthetic-secret-value synthetic-session-value\n'
                  'Authorization: Bearer synthetic-auth-value\n'
                  'https://example.invalid/?X-Amz-Signature=synthetic-signature-value')
        error.args = (detail,)
        with mock.patch.dict(os.environ, environment, clear=True):
            payload = self.invoke_frozen(error)
        self.assertEqual(payload['code'], 'TRANSACTION_JOURNAL_READBACK_UNKNOWN')
        text = json.dumps(payload)
        for secret in (*environment.values(), 'synthetic-auth-value', 'synthetic-signature-value'):
            self.assertNotIn(secret, text)
        self.assertIn('[REDACTED]', text)

    def test_existing_frozen_occupancy_failure_still_blocks_resolution_and_outputs(self):
        payload = self.invoke_frozen(frozen_occupancy.FrozenOccupancyError('UNRESOLVED_PUBLICATION_TRANSACTION'))
        self.assertEqual(payload['code'], 'release_contract_invalid')
        self.assertEqual(payload['detail'], 'UNRESOLVED_PUBLICATION_TRANSACTION')

    def test_verified_frozen_proof_remains_required_before_success(self):
        config = {'schemaVersion': 1, 'reservations': [{'kind': 'synthetic-frozen-record'}]}
        proof, payload = object(), {'releaseTag': 'v2.0.0-rc.1'}
        with (mock.patch.object(cli, '_read_json', return_value=config),
              mock.patch.object(frozen_occupancy, 'frozen_records', return_value=config['reservations']),
              mock.patch.object(frozen_occupancy, 'verify_live', return_value=proof) as verify,
              mock.patch.object(cli, '_read_tags', return_value=['v1.0.0']),
              mock.patch.object(cli, 'resolve_prerelease', return_value=payload) as resolve,
              mock.patch.object(cli, '_write_outputs') as outputs,
              contextlib.redirect_stdout(io.StringIO()) as stdout):
            self.assertEqual(cli.main(ARGV), 0)
        verify.assert_called_once_with(config, Path.cwd())
        self.assertIs(resolve.call_args.kwargs['frozen_observations'], proof)
        outputs.assert_called_once_with(Path('unused-output.txt'), payload)
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_unrelated_runtime_errors_are_not_converted_to_transaction_errors(self):
        with (mock.patch.object(cli, '_resolve', side_effect=RuntimeError('synthetic unrelated defect')),
              self.assertRaisesRegex(RuntimeError, 'synthetic unrelated defect')):
            cli.main(ARGV)
