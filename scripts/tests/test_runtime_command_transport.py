"""Public program bytes only; no Guest, subprocess, credentials or native input."""
import ast
import base64
import hashlib
import lzma
import shlex
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import candidate_guest_session as candidate
from scripts import development_guest_session as development
from scripts import runtime_development_boundary as boundary
from scripts.tests.workload_transport_fixture import decode_workload_transport

OPERATION = 'sha256:' + hashlib.sha256(b'synthetic-runtime-operation').hexdigest()


class RuntimeCommandTransportTests(unittest.TestCase):
    def test_same_transport_and_exact_sudo_arguments_in_both_representations(self):
        root = "# public construction only\nvalue = '中文 空格 \\\\ \"'\n"
        ordinary = candidate._remote_workload_command(root, OPERATION)
        compact = candidate._remote_workload_command(root, OPERATION, runtime_offline_only=True)
        raw = decode_workload_transport(compact)
        self.assertEqual(raw, decode_workload_transport(ordinary))
        compile(raw, '<public-transport-construction>', 'exec')
        calls = [node for node in ast.walk(ast.parse(raw)) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'Popen']
        self.assertEqual(len(calls), 1)
        argv = ast.literal_eval(calls[0].args[0])
        self.assertEqual(argv[:6], ['/usr/bin/sudo', '-S', '-k', '-p', '', '--'])
        self.assertEqual(argv[-1], root)
        self.assertEqual(compact.encode('gbk').decode('gbk'), compact)
        self.assertIn('base64.b64decode(', ordinary)
        self.assertIn('base64.b85decode(', compact)

    def test_compact_wrapper_checks_stream_output_and_memory_before_compile(self):
        wrapper = shlex.split(candidate._remote_workload_command(
            'pass', OPERATION, runtime_offline_only=True))[-1]
        payload = next(ast.literal_eval(node.args[0]) for node in ast.walk(ast.parse(wrapper))
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == 'b85decode')

        def substitute(compressed):
            self.assertEqual(wrapper.count(repr(payload)), 1)
            return wrapper.replace(repr(payload), repr(base64.b85encode(compressed).decode('ascii')))

        harmless = lzma.compress(b'result = 17\n', format=lzma.FORMAT_XZ, preset=6)
        prefix = b'result = 17\n#'
        maximum = lzma.compress(prefix + b'x' * (candidate.MAX_REMOTE_PROGRAM_BYTES - len(prefix)))
        for stream in (harmless, maximum):
            scope = {}
            exec(substitute(stream), scope)  # noqa: S102 - fixed harmless payload, never a transport body
            self.assertEqual(scope['result'], 17)
        failures = (lzma.compress(b'x' * (candidate.MAX_REMOTE_PROGRAM_BYTES + 1)),
                    harmless[:-1], harmless + b'extra', harmless + harmless,
                    lzma.compress(b'result = 17\n', preset=9), b'not an xz stream')
        for stream in failures:
            with self.subTest(size=len(stream)), mock.patch('builtins.compile') as compiler:
                with self.assertRaises((ValueError, lzma.LZMAError)):
                    exec(substitute(stream), {})  # noqa: S102 - only fixed harmless or rejected data
                compiler.assert_not_called()

    def test_preflight_and_execution_select_the_same_single_runtime_representation(self):
        profile = SimpleNamespace(profile='RUNTIME_BASE_OFFLINE')
        plan = SimpleNamespace(profiles=(profile,), runtime_offline_only=True,
                               published_subject_digest=None)
        provider = mock.Mock()
        provider._tool_path.return_value = Path('C:/held tools/ssh.exe')
        provider._ssh_argv.side_effect = lambda authority, remote: ('ssh', 'candidate.invalid', remote)
        with (mock.patch.object(development, '_root_program', return_value='pass') as root,
              mock.patch.object(candidate, '_diagnostic_operation', return_value=OPERATION),
              mock.patch.object(candidate, '_remote_workload_command',
                                wraps=candidate._remote_workload_command) as command):
            prepared = development.preflight_development_workload_commands(provider, plan)
            observed = development.preflight_development_workload_commands(
                provider, plan, _construction_only=False)
        self.assertTrue(prepared['profiles'][profile.profile]['construction_only'])
        self.assertFalse(observed['profiles'][profile.profile]['construction_only'])
        self.assertEqual(root.call_args_list[0].kwargs, {'_construction_only': True})
        self.assertEqual(root.call_args_list[1].kwargs, {'_construction_only': False})
        self.assertTrue(all(call.kwargs['runtime_offline_only'] is True
                            for call in command.call_args_list))
        supervisor = object.__new__(development._DevelopmentWorkloadSupervisor)
        supervisor._plan, supervisor._profile = plan, profile
        supervisor._role = 'CANDIDATE_WORKLOAD'
        with (mock.patch.object(supervisor, '_program', return_value='pass'),
              mock.patch.object(candidate, '_diagnostic_operation', return_value=OPERATION),
              mock.patch.object(candidate, '_remote_workload_command', side_effect=RuntimeError('stop before effects')) as command,
              self.assertRaisesRegex(RuntimeError, 'stop before effects')):
            supervisor.execute()
        command.assert_called_once_with('pass', OPERATION, formal_ssh_context=False,
                                        runtime_offline_only=True, _program_sizes={})
        provider._run.assert_not_called()

    def test_codec_capability_has_a_real_bounded_roundtrip_and_fails_closed(self):
        self.assertTrue(boundary.runtime_transport_codec_ready())
        with mock.patch('lzma.LZMADecompressor', side_effect=lzma.LZMAError('synthetic unsupported codec')):
            self.assertFalse(boundary.runtime_transport_codec_ready())
        original_import = __import__

        def without_lzma(name, *args, **kwargs):
            if name == 'lzma':
                raise ImportError('synthetic missing codec')
            return original_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=without_lzma):
            self.assertFalse(boundary.runtime_transport_codec_ready())

    def test_current_root_constructor_with_63_high_entropy_members_and_observed_growth(self):
        from installer import development_trust
        from scripts import candidate_vm_harness as harness

        def digest(label):
            return 'sha256:' + hashlib.sha256(label.encode('ascii')).hexdigest()

        profile = SimpleNamespace(profile='RUNTIME_BASE_OFFLINE', clone_identity=digest('clone'),
            snapshot_identity=digest('snapshot'), snapshot_disk_graph_identity=digest('snapshot-disk'))
        plan = SimpleNamespace(profiles=(profile,), runtime_offline_only=True, platform_diagnostic=False,
            published_subject_digest=None, runtime_authorization_deadline='2099-01-01T00:00:00Z',
            session_id=hashlib.md5(b'synthetic session', usedforsecurity=False).hexdigest(),
            plan_digest=digest('plan'), qualification_run_id=35444443071,
            original_vm_hashes={f'Ubuntu 64 位-{index:06}-s001.vmdk': digest(f'vm-{index}') for index in range(63)})
        for name in ('execution_source_sha', 'execution_source_tree', 'source_sha', 'source_tree'):
            setattr(plan, name, hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest())
        for name in ('execution_inventory_digest', 'runtime_trust_selection_digest', 'candidate_input_digest',
                     'verified_candidate_digest', 'source_vm_digest', 'source_disk_graph_identity', 'source_vm_inventory_identity'):
            setattr(plan, name, digest(name))
        provider = SimpleNamespace(_candidate_material_authority=SimpleNamespace(tree_inventory_identity=digest('material')),
            _runtime_development_material=SimpleNamespace(guest_inventory_digest=digest('guest-material')),
            _runtime_workload_binding=None)
        source = SimpleNamespace(root=Path(development.__file__).resolve().parents[1])
        with (mock.patch.object(development, 'require_development_source', return_value=source),
              mock.patch.object(development_trust, 'runtime_material_boundary',
                                return_value={'selection_digest': plan.runtime_trust_selection_digest})):
            construction_binding = development._runtime_workload_binding(provider, plan, construction_only=True)
            facts = construction_binding['runtime_operation_boundary']['baseline_observation']
            for index, tool in enumerate(facts['tools'].values()):
                tool['resolved_path'] = '/usr/lib/' + '/'.join(
                    hashlib.sha512(f'tool-path-{index}-{chunk}'.encode()).hexdigest() for chunk in range(3))
                tool['sha256'] = digest(f'observed-tool-{index}')
            facts['docker_socket'] = {'state': 'KNOWN_LOCAL', 'path': '/var/run/docker.sock',
                'resolved_path': '/run/docker.sock', 'device': 10001, 'inode': 987654321,
                'mode': stat.S_IFSOCK | 0o660, 'uid': 0, 'gid': 999, 'ctime_ns': 1790779050123456789}
            facts['docker_daemon'] = {'state': 'OBSERVED', 'identity': digest('daemon'),
                'server_version': ''.join(hashlib.sha256(f'version-{index}'.encode()).hexdigest() for index in range(8)),
                'storage_root': '/var/lib/docker'}
            confirmed = SimpleNamespace(body=construction_binding['runtime_operation_boundary'],
                _lifetime=SimpleNamespace(guest_envelope=lambda: construction_binding['runtime_lifetime']))
            with mock.patch.object(boundary, 'require_confirmed_runtime_boundary', return_value=confirmed):
                root = development._root_program(provider, plan, profile, _construction_only=False)
                for index, tool in enumerate(facts['tools'].values()):
                    tool['resolved_path'] = '/usr/lib/' + '/'.join(
                        hashlib.sha512(f'long-tool-path-{index}-{chunk}'.encode()).hexdigest()
                        for chunk in range(6))
                oversized_provider = SimpleNamespace(**{**vars(provider), '_runtime_workload_binding': None})
                oversized_root = development._root_program(oversized_provider, plan, profile, _construction_only=False)
        command = candidate._remote_workload_command(root, candidate._diagnostic_operation(plan, profile),
                                                     runtime_offline_only=True)
        decoded = decode_workload_transport(command)
        compile(root, '<current-public-root-construction>', 'exec')
        compile(decoded, '<current-public-transport-construction>', 'exec')
        argv = next(ast.literal_eval(node.args[0]) for node in ast.walk(ast.parse(decoded))
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'Popen')
        self.assertEqual(argv[-1], root)
        self.assertEqual(root.encode('gbk').decode('gbk'), root)
        ssh_prefix = ('C:/public fixture/' + 'x' * 96 + '/ssh.exe', '-F', 'none',
            *(argument for option in harness.OPENSSH_REQUIRED_OPTIONS for argument in ('-o', option)),
            '-o', 'UserKnownHostsFile=C:/public fixture/' + 'y' * 220,
            '-o', 'IdentityFile=C:/public fixture/' + 'z' * 220, '--', harness.SSH_HOST)
        budget = candidate.validate_workload_command_budget((*ssh_prefix, command))
        self.assertLessEqual(budget['windows_command_utf16_units_including_nul'], 32767)
        # Accepted observation fields lack a combined length bound. A larger
        # legal path shape must fail the actual capacity gate, not lose facts.
        sizes = {}
        oversized_command = candidate._remote_workload_command(oversized_root, OPERATION,
            runtime_offline_only=True, _program_sizes=sizes)
        sizes['remote_command_utf8_bytes'] = len(oversized_command.encode('utf-8'))
        with self.assertRaises(candidate.WorkloadCommandBudgetFailure) as caught:
            candidate.validate_workload_command_budget((*ssh_prefix, oversized_command), program_sizes=sizes)
        self.assertGreater(caught.exception.command_budget['windows_command_excess_units'], 0)
        self.assertEqual(caught.exception.command_budget['root_program_utf8_bytes'],
                         len(oversized_root.encode('utf-8')))

    def test_rejection_preserves_all_four_program_sizes_and_exact_excess(self):
        sizes = {}
        command = candidate._remote_workload_command('pass', OPERATION,
            runtime_offline_only=True, _program_sizes=sizes)
        sizes['remote_command_utf8_bytes'] = len(command.encode('utf-8'))
        with self.assertRaises(candidate.WorkloadCommandBudgetFailure) as caught:
            candidate.validate_workload_command_budget(('x' * 32767, command), program_sizes=sizes)
        budget = caught.exception.command_budget
        self.assertEqual(budget['root_program_utf8_bytes'], 4)
        self.assertEqual(budget['decoded_transport_utf8_bytes'], len(decode_workload_transport(command).encode('utf-8')))
        self.assertEqual(budget['remote_command_utf8_bytes'], len(command.encode('utf-8')))
        self.assertEqual(budget['windows_command_excess_units'],
            budget['windows_command_utf16_units_including_nul'] - 32767)

    def test_observed_preflight_and_execution_preserve_metrics_before_exception_wrapping(self):
        profile = SimpleNamespace(profile='RUNTIME_BASE_OFFLINE')
        plan = SimpleNamespace(profiles=(profile,), runtime_offline_only=True,
                               published_subject_digest=None)
        provider = mock.Mock()
        provider._profile_operation_results = {}
        provider._tool_path.return_value = Path('C:/held tools/ssh.exe')
        provider._ssh_argv.side_effect = lambda authority, remote: ('ssh', 'x' * 32767, remote)
        with (mock.patch.object(development, '_root_program', return_value='pass'),
              mock.patch.object(candidate, '_diagnostic_operation', return_value=OPERATION),
              self.assertRaises(candidate.WorkloadCommandBudgetFailure) as preflight_error):
            development.preflight_development_workload_commands(provider, plan, _construction_only=False)
        recorded = provider._profile_operation_results[profile.profile]['workload_command_budget_failure']
        self.assertEqual(recorded, preflight_error.exception.command_budget)
        supervisor = object.__new__(development._DevelopmentWorkloadSupervisor)
        supervisor._plan, supervisor._profile, supervisor._provider = plan, profile, provider
        supervisor._authority = object()
        supervisor.close = mock.Mock()
        lifetime = mock.Mock()
        lifetime.workload_timeout.return_value = 300
        with (mock.patch.object(supervisor, '_program', return_value='pass'),
              mock.patch.object(candidate, '_diagnostic_operation', return_value=OPERATION),
              mock.patch.object(boundary, 'require_runtime_lifetime', return_value=lifetime),
              self.assertRaises(candidate.WorkloadCommandBudgetFailure) as execution_error):
            supervisor.execute()
        self.assertEqual(provider._profile_operation_results[profile.profile]['workload_command_budget_failure'],
                         execution_error.exception.command_budget)
        self.assertEqual(recorded, execution_error.exception.command_budget)
        self.assertEqual(recorded['root_program_utf8_bytes'], 4)
        provider._run.assert_not_called()
        supervisor.close.assert_called_once_with(failed=True)

if __name__ == '__main__':
    unittest.main()
