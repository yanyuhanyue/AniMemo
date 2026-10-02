"""No public requests: real supervised workers retain bounded failure facts."""
import hashlib
import json
import sys
import threading
import time
import unittest
from unittest import mock

from bootstrap_kit import http_supervisor as supervisor
from bootstrap_kit import runtime
from bootstrap_kit.http_protocol import (
    CONTROL_BYTES,
    PROGRESS_FRAMES,
    HttpFailure,
    HttpSelection,
)
from bootstrap_kit.tests import test_http_supervisor as fixture
from bootstrap_kit.tests.test_http_supervisor import (
    CANARY,
    loopback_program,
    server,
    test_command,
)


class DiagnosticTests(unittest.TestCase):
    setUp = fixture.SupervisorTests.setUp
    tearDown = fixture.SupervisorTests.tearDown
    closed = fixture.SupervisorTests.closed

    def test_body_timeout_keeps_real_status_bytes_and_runtime_projection(self):
        observed = runtime._ObservedClient(self.client, time.monotonic() + 5)
        with server('body') as (port, calls), mock.patch.object(
                supervisor, '_command', test_command(loopback_program(port))), \
                self.assertRaisesRegex(HttpFailure, 'DEADLINE') as caught:
            observed.fetch(self.selection, deadline=time.monotonic() + 2.5)
        self.closed()
        self.assertEqual(len(calls), 1)
        facts = caught.exception.diagnostics
        self.assertEqual(facts['http_status'], 200)
        self.assertGreaterEqual(facts['received_bytes'], 1)
        self.assertIsInstance(facts['persisted_bytes'], int)
        self.assertEqual(facts['received_bytes_kind'], 'LAST_OBSERVED_LOWER_BOUND')
        self.assertEqual(observed.records[0]['diagnostics'], facts)
        self.assertNotIn(CANARY, json.dumps(observed.records))
        self.assertNotIn(str(self.root), json.dumps(observed.records))

    def test_incomplete_headers_do_not_invent_status_or_received_bytes(self):
        with server('headers') as (port, _), mock.patch.object(
                supervisor, '_command', test_command(loopback_program(port))), \
                self.assertRaisesRegex(HttpFailure, 'DEADLINE') as caught:
            self.client.fetch(self.selection, deadline=time.monotonic() + 2.5)
        self.closed()
        self.assertIsNone(caught.exception.diagnostics['http_status'])
        self.assertEqual(caught.exception.diagnostics['http_responses'], [])
        self.assertIsNone(caught.exception.diagnostics['persisted_bytes'])

    def test_result_then_exit_stall_keeps_facts_but_never_returns_bytes(self):
        with server() as (port, _):
            program = loopback_program(port).replace(
                'raise SystemExit(w.main())', 'code=w.main();import time;time.sleep(120)')
            with mock.patch.object(supervisor, '_command', test_command(program)), \
                    self.assertRaisesRegex(HttpFailure, 'DEADLINE') as caught:
                self.client.fetch(self.selection, deadline=time.monotonic() + 2.5)
        self.closed()
        facts = caught.exception.diagnostics
        self.assertEqual(facts['http_status'], 200)
        self.assertEqual(facts['received_bytes'], 2)
        self.assertEqual(facts['persisted_bytes'], 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_broken_pipe_after_header_observation_preserves_status(self):
        extra = (
            'original=w.execute\n'
            'def broken(selection,deadline,*,progress):\n'
            ' def observe(*args):\n'
            '  progress(*args)\n'
            '  if args[0]=="HEADERS":\n'
            '   import os;os.close(1)\n'
            '   import time;time.sleep(120)\n'
            ' return original(selection,deadline,progress=observe)\n'
            'w.execute=broken')
        with server() as (port, _):
            command = test_command(loopback_program(port, extra))
            def direct(nonce):
                arguments = command(nonce)
                # A Windows venv redirector itself retains stdout until exit;
                # use its same trusted base interpreter to exercise real EOF.
                arguments[0] = sys._base_executable
                return arguments
            with mock.patch.object(supervisor, '_command', direct), \
                    self.assertRaisesRegex(HttpFailure, 'CONTROL_TRUNCATED') as caught:
                self.client.fetch(self.selection, deadline=time.monotonic() + 3)
        self.closed()
        self.assertEqual(caught.exception.diagnostics['http_status'], 200)

    def test_cancel_after_headers_preserves_primary_and_real_facts(self):
        cancel = threading.Event()
        original_next = supervisor._ControlReader.next
        def after_body(reader, **kwargs):
            frame = original_next(reader, **kwargs)
            if frame.get('phase') == 'BODY':
                cancel.set()
            return frame
        with mock.patch.object(supervisor._ControlReader, 'next', after_body):
            with server('body') as (port, _), mock.patch.object(
                    supervisor, '_command', test_command(loopback_program(port))), \
                    self.assertRaisesRegex(HttpFailure, 'CANCELLED') as caught:
                self.client.fetch(self.selection, deadline=time.monotonic() + 3, cancel_event=cancel)
        self.closed()
        self.assertEqual(caught.exception.diagnostics['http_status'], 200)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_malformed_late_and_oversized_progress_are_not_diagnostic_authority(self):
        prefix = (
            'from bootstrap_kit.http_protocol import *;import time\n'
            'nonce=sys.argv[1]\n'
            'sys.stdout.buffer.write(canonical(dict(protocol=PROTOCOL,event="READY",nonce=nonce)));sys.stdout.buffer.flush()\n'
            'go=strict_json(sys.stdin.buffer.read());s=HttpSelection.from_record(go["selection"])\n'
            'frame=dict(protocol=PROTOCOL,event="PROGRESS",nonce=nonce,identity=s.identity,sequence=1,phase="REQUEST",request_index=0,http_status=None,received_bytes=0,observed_monotonic=time.monotonic())\n')
        variants = [
            'frame["received_bytes"]=s.maximum_bytes+1',
            'frame["observed_monotonic"]=go["deadline"]+1',
            'frame["observed_monotonic"]=10**400',
            'frame["phase"]=[]',
            'frame["nonce"]="0"*32',
            'frame["query"]=' + repr(CANARY),
            'frame["sequence"]=25',
        ]
        for change in variants:
            program = prefix + change + '\nsys.stdout.buffer.write(canonical(frame));sys.stdout.buffer.flush();time.sleep(120)'
            with self.subTest(change=change), mock.patch.object(supervisor, '_command', test_command(program)):
                with self.assertRaisesRegex(HttpFailure, 'CONTROL_INVALID') as caught:
                    self.client.fetch(self.selection, deadline=time.monotonic() + 3)
                self.closed()
                self.assertIsNone(caught.exception.diagnostics['http_status'])
                self.assertNotIn(CANARY, repr(caught.exception.diagnostics))
        program = prefix + '\nsys.stdout.buffer.write(b"x"*' + str(CONTROL_BYTES + 1) + ');sys.stdout.buffer.flush();time.sleep(120)'
        with mock.patch.object(supervisor, '_command', test_command(program)), \
                self.assertRaisesRegex(HttpFailure, 'CONTROL_INVALID'):
            self.client.fetch(self.selection, deadline=time.monotonic() + 3)
        self.closed()

    def test_early_cancel_clears_old_diagnostics(self):
        self.client.last_diagnostics = {'http_status': 200, 'received_bytes': 500}
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(HttpFailure, 'CANCELLED'):
            self.client.fetch(self.selection, deadline=time.monotonic() + 3, cancel_event=cancel)
        self.assertIsNone(self.client.last_diagnostics)
        self.assertIsNone(self.client.last_process_receipt)

    def test_redirect_statuses_survive_body_stall_without_signed_query(self):
        selection = HttpSelection.github_asset(version='v2.0.0-rc.3', release_id=392113678,
            asset_id=574986486, name='installer-materials.tar', size=2,
            sha256=hashlib.sha256(b'{}').hexdigest())
        extra = ('original=w.execute\n'
            'def stalled(selection,deadline,*,progress):\n'
            ' def observe(*args):\n'
            '  progress(*args)\n'
            '  if args[0]=="HEADERS" and args[2]==200:\n'
            '   import time;time.sleep(120)\n'
            ' return original(selection,deadline,progress=observe)\n'
            'w.execute=stalled')
        with server('redirect') as (port, calls), mock.patch.object(
                supervisor, '_command', test_command(loopback_program(port, extra))), \
                self.assertRaisesRegex(HttpFailure, 'DEADLINE') as caught:
            self.client.fetch(selection, deadline=time.monotonic() + 2.5)
        self.closed()
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.diagnostics['http_responses'],
                         [{'request_index': 0, 'status': 302}, {'request_index': 1, 'status': 200}])
        self.assertNotIn('NONSECRET_SIGNED_QUERY', repr(caught.exception.diagnostics))

    def test_progress_sequence_budget_is_cumulative(self):
        nonce = 'a' * 32
        state = supervisor._ProgressState(nonce, self.selection, time.monotonic() + 10)
        state.go = time.monotonic()
        for index in range(PROGRESS_FRAMES):
            phase = 'REQUEST' if index == 0 else 'HEADERS' if index == 1 else 'BODY'
            frame = {'protocol': supervisor.PROTOCOL, 'event': 'PROGRESS', 'nonce': nonce,
                'identity': self.selection.identity, 'sequence': index + 1, 'phase': phase,
                'request_index': 0, 'http_status': None if index == 0 else 200,
                'received_bytes': max(0, index - 1), 'observed_monotonic': time.monotonic()}
            state.accept(frame)
        frame['sequence'] += 1
        with self.assertRaisesRegex(HttpFailure, 'CONTROL_INVALID'):
            state.accept(frame)

    def test_result_is_terminal_even_for_otherwise_valid_late_progress(self):
        extra = ('original=w.execute;state={"count":0}\n'
            'def observed(selection,deadline,*,progress):\n'
            ' state["identity"]=selection.identity\n'
            ' def report(*args):\n'
            '  state["count"]+=1;progress(*args)\n'
            ' return original(selection,deadline,progress=report)\n'
            'w.execute=observed')
        tail = ('code=w.main();import time\n'
            'sys.stdout.buffer.write(w.canonical(dict(protocol=w.PROTOCOL,event="PROGRESS",'
            'nonce=sys.argv[1],identity=state["identity"],sequence=state["count"]+1,'
            'phase="FAILED",request_index=0,http_status=200,received_bytes=2,'
            'observed_monotonic=time.monotonic())));sys.stdout.buffer.flush();raise SystemExit(code)')
        with server() as (port, _):
            program = loopback_program(port, extra).replace('raise SystemExit(w.main())', tail)
            with mock.patch.object(supervisor, '_command', test_command(program)), \
                    self.assertRaisesRegex(HttpFailure, 'CONTROL_INVALID'):
                self.client.fetch(self.selection, deadline=time.monotonic() + 5)
        self.closed()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_redirect_progress_cannot_move_observation_time_backwards(self):
        selection = HttpSelection.github_asset(version='v2.0.0-rc.3', release_id=392113678,
            asset_id=574986486, name='installer-materials.tar', size=2,
            sha256=hashlib.sha256(b'{}').hexdigest())
        now = time.monotonic()
        state = supervisor._ProgressState('a' * 32, selection, now + 5)
        state.go = now - 2
        frame = {'protocol': supervisor.PROTOCOL, 'event': 'PROGRESS', 'nonce': 'a' * 32,
            'identity': selection.identity, 'sequence': 1, 'phase': 'REQUEST', 'request_index': 0,
            'http_status': None, 'received_bytes': 0, 'observed_monotonic': now - 1}
        state.accept(frame)
        frame.update(sequence=2, phase='HEADERS', http_status=302, observed_monotonic=now)
        state.accept(frame)
        frame.update(sequence=3, phase='REQUEST', request_index=1, http_status=None,
                     observed_monotonic=now - 0.5)
        with self.assertRaisesRegex(HttpFailure, 'CONTROL_INVALID'):
            state.accept(frame)

    def test_observed_404_never_turns_deadline_or_bad_protocol_into_tuf_not_found(self):
        from bootstrap_kit.trust import supervised_tuf_fetcher
        from release.trust_bootstrap import (
            _TRACKS,
            TrustBootstrapError,
            TUFMetadataNotFound,
        )
        for mode in ('deadline', 'protocol', 'ordinary', 'trailing'):
            action = ('import time;time.sleep(120)' if mode == 'deadline' else
                      'sys.stdout.buffer.write(b"{}\\n");sys.stdout.buffer.flush();import os;os._exit(0)')
            extra = '' if mode in {'ordinary', 'trailing'} else (
                'original=w.execute\n'
                'def changed(selection,deadline,*,progress):\n'
                ' def report(*args):\n'
                '  progress(*args)\n'
                '  if args[0]=="HEADERS":\n'
                '   ' + action + '\n'
                ' return original(selection,deadline,progress=report)\n'
                'w.execute=changed')
            with self.subTest(mode=mode), server('notfound') as (port, _):
                program = loopback_program(port, extra)
                if mode == 'trailing':
                    program = program.replace('raise SystemExit(w.main())',
                        'code=w.main();sys.stdout.buffer.write(b"{}\\n");sys.stdout.buffer.flush();raise SystemExit(code)')
                with mock.patch.object(supervisor, '_command', test_command(program)):
                    observed = runtime._ObservedClient(self.client, time.monotonic() + 3)
                    fetch = supervised_tuf_fetcher(observed, deadline=time.monotonic() + 2.5)
                    with self.assertRaises((TrustBootstrapError, TUFMetadataNotFound)) as caught:
                        fetch(_TRACKS['github']['repository'] + '/123.root.json', 1024)
                    if mode == 'ordinary':
                        self.assertIsInstance(caught.exception, TUFMetadataNotFound)
                    else:
                        self.assertNotIsInstance(caught.exception, TUFMetadataNotFound)
                        expected = {'deadline': 'DEADLINE', 'protocol': 'CONTROL_INVALID', 'trailing': 'HTTP_404'}
                        self.assertIn(expected[mode], str(caught.exception))
                        if mode == 'trailing':
                            self.assertIn('BOOTSTRAP_HTTP_CONTROL_INVALID', caught.exception.secondary_errors)
                            self.assertIn('BOOTSTRAP_HTTP_CONTROL_INVALID', observed.records[0]['secondary_errors'])
                    self.assertEqual(self.client.last_diagnostics['http_status'], 404)
                    self.closed()

    def test_tuf_current_record_is_exclusive_and_write_failure_is_fixed(self):
        # This is a writer test, not a mocked cryptographic verification result.
        value = {'authority': 'DEVELOPMENT_ONLY', 'state': 'TEST_ONLY',
                 'status': 'VERIFIED', 'tracks': {'github': {'fixture': 'WRITER_ONLY'}}}
        runtime._record_tuf_verification(self.root, value)
        self.assertEqual(json.loads((self.root / 'tuf-verification.json').read_bytes()), value)
        with self.assertRaisesRegex(ValueError, 'BOOTSTRAP_KIT_TUF_RECORD_FAILED'):
            runtime._record_tuf_verification(self.root, value)
        self.assertEqual(json.loads((self.root / 'tuf-verification.json').read_bytes()), value)


if __name__ == '__main__':
    unittest.main()
