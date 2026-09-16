"""Synthetic command construction; no native confirmation, process or Guest."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_guest_session as c
from scripts import candidate_vm_harness as h


class CandidateCommandPreflightTests(unittest.TestCase):
    def test_all_profiles_use_runtime_builders_and_resolved_executable(self):
        profiles = tuple(SimpleNamespace(profile=name) for name in h.PROFILES)
        plan = SimpleNamespace(profiles=profiles, plan_digest='sha256:' + 'a' * 64)
        provider = mock.Mock()
        provider._tool_path.return_value = Path('C:/held tools/ssh.exe')
        provider._active_profile_authority.side_effect = lambda profile, selected: profile
        provider._ssh_argv.side_effect = lambda authority, remote: ('ssh', authority.profile, remote)
        with mock.patch.object(c, '_root_program', return_value='pass') as root, \
                mock.patch.object(c, '_diagnostic_operation', return_value='sha256:' + 'b' * 64), \
                mock.patch.object(c, '_remote_workload_command', return_value='fixed remote') as remote:
            result = c.preflight_candidate_workload_commands(provider, plan)
        self.assertEqual(set(result['profiles']), set(h.PROFILES))
        self.assertEqual(result['plan_digest'], plan.plan_digest)
        self.assertFalse(result['secret_capture_required'])
        self.assertEqual(root.call_count, len(profiles))
        self.assertEqual(remote.call_count, len(profiles))
        for profile in profiles:
            root.assert_any_call(provider, plan, profile, h._initial_platform_state(profile.profile))
            expected = c.validate_workload_command_budget((str(provider._tool_path.return_value), profile.profile, 'fixed remote'))
            self.assertEqual(result['profiles'][profile.profile]['windows_command_utf16_units_including_nul'],
                expected['windows_command_utf16_units_including_nul'])
        provider._run.assert_not_called()

    def test_complete_argv_overflow_rejects_before_any_process(self):
        plan = SimpleNamespace(profiles=(SimpleNamespace(profile='FRESH_BASE'),), plan_digest='sha256:'+'a'*64)
        provider = mock.Mock()
        provider._tool_path.return_value = Path('C:/held/ssh.exe')
        provider._ssh_argv.return_value = ('ssh', 'x' * 32767)
        with mock.patch.object(c, '_root_program', return_value='pass'), \
                mock.patch.object(c, '_diagnostic_operation', return_value='sha256:'+'b'*64), \
                mock.patch.object(c, '_remote_workload_command', return_value='short command'):
            with self.assertRaisesRegex(c.ControllerFailure, 'CANDIDATE_WORKLOAD_COMMAND_LIMIT_EXCEEDED'):
                c.preflight_candidate_workload_commands(provider, plan)
        provider._run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
