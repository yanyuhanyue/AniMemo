"""Real owned workers and loopback bytes. All injected workers are TEST_ONLY."""
import gc
import hashlib
import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
import weakref
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from bootstrap_kit import http_supervisor as supervisor
from bootstrap_kit import owned_process
from bootstrap_kit.http_protocol import HttpFailure, HttpSelection
from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    remove_owned_directory,
)

ROOT = Path(tempfile.gettempdir())
ORIGIN = str(Path(supervisor.__file__).parent.parent)
CANARY = 'NONSECRET_HOST_CREDENTIAL_CANARY'


def test_command(program):
    def command(nonce):
        # Same fixed interpreter; bypass only the Windows venv redirector so
        # subsecond fault timing measures the worker rather than a second launch.
        return [sys._base_executable, '-I', '-S', '-B', '-c',
            'import sys;sys.path.insert(0,' + repr(ORIGIN) + ');' + program, nonce]
    return command


def loopback_program(port, extra=''):
    return ('from bootstrap_kit import http_worker as w;import http.client;'
        'Original=http.client.HTTPConnection\n'
        'class Local(Original):\n'
        ' def __init__(self,host,timeout,context): super().__init__("127.0.0.1",' + str(port) + ',timeout=timeout)\n'
        'w.http.client.HTTPSConnection=Local\n' + extra + '\nraise SystemExit(w.main())')


@contextmanager
def server(mode='ok'):
    calls = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            calls.append((self.path, list(self.headers.items())))
            try:
                if mode in {'redirect', 'twice'} and (mode == 'twice' or self.path.startswith('/repos/')):
                    self.send_response(302)
                    self.send_header('Location', 'https://release-assets.githubusercontent.com/github-production-release-asset/1327429673/6b04b10a-2b03-4ef6-9f41-9c1dae586843?sig=NONSECRET_SIGNED_QUERY')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if mode in {'forbidden', 'notfound'}:
                    self.send_response(404 if mode == 'notfound' else 403)
                    self.send_header('Content-Length', str(len(CANARY)))
                    self.end_headers()
                    self.wfile.write(CANARY.encode())
                    return
                if mode == 'headers':
                    self.connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    while not finished.wait(0.01):
                        self.connection.sendall(b' ')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream' if mode in {'asset', 'redirect'} else 'application/json')
                if mode == 'duplicate':
                    self.send_header('Content-Length', '2')
                self.send_header('Content-Length', '100000' if mode == 'body' else '2')
                self.end_headers()
                if mode == 'body':
                    while not finished.wait(0.01):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                else:
                    self.wfile.write(b'{}')
            except (OSError, ValueError):
                pass
    finished = threading.Event()
    listener = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=listener.serve_forever, name='animemo-test-http-server')
    worker.start()
    try:
        yield listener.server_port, calls
    finally:
        finished.set()
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=2)
        if worker.is_alive():
            raise AssertionError('Loopback server survived cleanup')


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.root = create_private_directory(ROOT, prefix='animemo-http-tests-')
        self.identity = directory_identity(self.root)
        self.client = supervisor.SupervisedAnonymousHttp(private_root=self.root)
        self.selection = HttpSelection.github_json('/repos/yanyuhanyue/AniMemo/releases/tags/v2.0.0-rc.3')
        self._initial_retained = set(supervisor._RETAINED_HTTP_RESOURCES)

    def tearDown(self):
        # Negative tests inject missing confirmations only after actual reaping.
        # Release their private registries explicitly; production has no retry API.
        for identity in set(supervisor._RETAINED_HTTP_RESOURCES) - self._initial_retained:
            retained = supervisor._RETAINED_HTTP_RESOURCES[identity]
            self.assertEqual(retained._slot.parent, self.root)
            self.assertIsNotNone(retained._owned.process.poll())
            if retained._reader is not None:
                self.assertFalse(retained._reader.thread.is_alive())
            if retained._writer is not None:
                self.assertFalse(retained._writer.is_alive())
            supervisor._RETAINED_HTTP_RESOURCES.pop(identity)
        remove_owned_directory(self.root, self.identity)
        self.assertFalse(any(t.name in {'animemo-http-control', 'animemo-http-go', 'animemo-test-http-server'}
                             for t in threading.enumerate()))

    def closed(self):
        self.assertTrue(self.client.last_process_receipt['root_reaped'])
        self.assertTrue(self.client.last_process_receipt['tree_empty'])
        self.assertFalse(self.client.last_process_receipt['failures'])

    def test_real_worker_success_is_untrusted_and_anonymous(self):
        extra = ('import os\n'
            'assert not {"GH_TOKEN","GITHUB_TOKEN","HTTPS_PROXY","SSL_CERT_FILE","PYTHONPATH"}&set(os.environ)\n'
            'assert os.environ.get("HOME") != ' + repr(CANARY))
        with server() as (port, calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port, extra))), \
                mock.patch.object(supervisor.os, 'environ', {'GH_TOKEN': CANARY, 'GITHUB_TOKEN': CANARY,
                    'HTTPS_PROXY': CANARY, 'SSL_CERT_FILE': CANARY, 'SystemRoot': 'C:\\Windows'}):
            with self.client.fetch(self.selection, deadline=time.monotonic()+5) as result:
                self.assertEqual(result.authority, 'UNTRUSTED_HTTP_BYTES')
                self.assertEqual(result.read_bytes(), b'{}')
                self.assertEqual(result.headers['content-length'], '2')
                self.assertEqual(result.sha256, hashlib.sha256(b'{}').hexdigest())
                self.assertNotIn(CANARY, repr(result.observations))
            self.closed()
            self.assertEqual(len(calls), 1)
            self.assertFalse({'authorization','cookie','proxy-authorization'} & {k.lower() for k,v in calls[0][1]})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_real_dns_tcp_tls_stalls_are_killed_by_same_supervisor(self):
        programs = [
            'socket.getaddrinfo=stall',
            'socket.create_connection=stall',
            'pair=socket.socketpair();socket.create_connection=lambda *a,**k:pair[0];ssl.SSLContext.wrap_socket=stall',
        ]
        for patch in programs:
            program = ('from bootstrap_kit import http_worker as w;import socket,time,ssl\n'
                'def stall(*a,**k): time.sleep(120)\n' + patch + '\nraise SystemExit(w.main())')
            with self.subTest(patch=patch), mock.patch.object(supervisor, '_command', test_command(program)):
                started = time.monotonic()
                with self.assertRaisesRegex(HttpFailure, 'BOOTSTRAP_HTTP_DEADLINE'):
                    self.client.fetch(self.selection, deadline=time.monotonic()+0.5)
                self.assertLess(time.monotonic()-started, 3)
                self.closed()

    def test_slow_headers_body_and_duplicate_headers_use_real_worker(self):
        for mode in ('headers', 'body', 'duplicate'):
            with self.subTest(mode=mode), server(mode) as (port, calls), \
                    mock.patch.object(supervisor, '_command', test_command(loopback_program(port))):
                with self.assertRaises(HttpFailure) as caught:
                    self.client.fetch(self.selection, deadline=time.monotonic()+2.5)
                self.assertEqual(caught.exception.code, 'BOOTSTRAP_HTTP_HEADERS_INVALID' if mode == 'duplicate' else 'BOOTSTRAP_HTTP_DEADLINE')
                self.closed()
                self.assertEqual(len(calls), 1)

    def test_asset_200_parent_independently_checks_bytes(self):
        selection = HttpSelection.github_asset(version='v2.0.0-rc.3',release_id=392113678,
            asset_id=574986486,name='installer-materials.tar',size=2,sha256=hashlib.sha256(b'{}').hexdigest())
        with server('asset') as (port, _calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port))):
            with self.client.fetch(selection, deadline=time.monotonic()+5) as result:
                self.assertEqual(result.read_bytes(), b'{}')
            self.closed()

    def test_asset_302_exact_cdn_and_second_redirect_rejection(self):
        selection = HttpSelection.github_asset(version='v2.0.0-rc.3',release_id=392113678,
            asset_id=574986486,name='installer-materials.tar',size=2,sha256=hashlib.sha256(b'{}').hexdigest())
        for mode in ('redirect', 'twice'):
            with self.subTest(mode=mode), server(mode) as (port, calls), \
                    mock.patch.object(supervisor, '_command', test_command(loopback_program(port))):
                if mode == 'redirect':
                    with self.client.fetch(selection, deadline=time.monotonic()+5) as result:
                        self.assertEqual(result.read_bytes(), b'{}')
                        self.assertEqual([o['status'] for o in result.observations], [302,200])
                        self.assertNotIn('NONSECRET_SIGNED_QUERY', repr((result.headers,result.observations)))
                        self.assertNotIn('location', result.headers)
                else:
                    with self.assertRaises(HttpFailure) as caught:
                        self.client.fetch(selection, deadline=time.monotonic()+5)
                    self.assertEqual(caught.exception.http_status,302)
                    self.assertNotIn('NONSECRET_SIGNED_QUERY', repr(caught.exception.observations))
                self.assertEqual(len(calls),2)
                self.closed()

    def test_http_error_has_fixed_code_real_status_and_no_response_body(self):
        with server('forbidden') as (port,_calls), mock.patch.object(supervisor,'_command',test_command(loopback_program(port))):
            with self.assertRaises(HttpFailure) as caught:
                self.client.fetch(self.selection,deadline=time.monotonic()+5)
            self.assertEqual(caught.exception.http_status,403)
            self.assertEqual(caught.exception.observations[0]['status'],403)
            self.assertNotIn(CANARY,str(caught.exception))
            self.assertEqual(list(self.root.iterdir()),[])
            self.closed()

    def test_actual_pyz_worker_closure_imports_without_product_or_repository(self):
        archive = self.root / 'test-only-entry.pyz'
        with server() as (port,calls):
            with zipfile.ZipFile(archive,'w') as bundle:
                for name in ('__init__.py','http_protocol.py','http_worker.py','http_supervisor.py','owned_process.py','safe_files.py','egress.py'):
                    data = (Path(ORIGIN)/'bootstrap_kit'/name).read_bytes()
                    if name == 'http_worker.py':
                        data += ('\n# TEST_ONLY loopback low-level connection boundary.\n'
                            'assert ".pyz" in __file__\n'
                            'class Local(http.client.HTTPConnection):\n'
                            ' def __init__(self,host,timeout,context): super().__init__("127.0.0.1",' + str(port) + ',timeout=timeout)\n'
                            'http.client.HTTPSConnection=Local\n').encode()
                    bundle.writestr('bootstrap_kit/'+name,data)
            with mock.patch.object(supervisor,'__file__',str(archive/'bootstrap_kit'/'http_supervisor.py')):
                with self.client.fetch(self.selection,deadline=time.monotonic()+5) as result:
                    self.assertEqual(result.read_bytes(),b'{}')
                self.closed()
            self.assertEqual(len(calls),1)
        archive.unlink()

    def test_parent_rehash_rejects_worker_self_report(self):
        extra = ('original=w.execute\n'
            'def corrupt(selection,deadline,**kwargs):\n'
            ' result=original(selection,deadline,**kwargs)\n'
            ' result["sha256"]="a"*64\n'
            ' return result\n'
            'w.execute=corrupt')
        with server() as (port,_calls), mock.patch.object(supervisor,'_command',test_command(loopback_program(port,extra))):
            with self.assertRaisesRegex(HttpFailure,'OUTPUT_IDENTITY_MISMATCH'):
                self.client.fetch(self.selection,deadline=time.monotonic()+5)
            self.closed()

    @unittest.skipUnless(os.name == 'nt', 'real Windows Job cleanup status injection')
    def test_reported_job_termination_failure_keeps_slot_unconsumable(self):
        terminate = owned_process._WindowsJob.terminate
        def report_failure(job):
            terminate(job)  # The real owned Job is terminated before injecting its error report.
            raise owned_process.ChildProcessError()
        with server() as (port,_calls), mock.patch.object(supervisor,'_command',test_command(loopback_program(port))), \
                mock.patch.object(owned_process._WindowsJob,'terminate',report_failure):
            with self.assertRaisesRegex(HttpFailure,'CLEANUP_FAILED') as caught:
                self.client.fetch(self.selection,deadline=time.monotonic()+5)
            self.assertIn('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED',caught.exception.secondary_errors)
            self.assertTrue(self.client.last_process_receipt['root_reaped'])
            self.assertTrue(self.client.last_process_receipt['tree_empty'])
            self.assertEqual(len(list(self.root.iterdir())),1)
            # Test-owned retained slot is removed by tearDown only after actual reaping above.

    @unittest.skipUnless(os.name == 'nt', 'real Windows suspended creation')
    def test_failed_assignment_never_runs_worker_or_network(self):
        with server() as (port, calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port))), \
                mock.patch.object(owned_process._WindowsJob, 'assign_and_resume', side_effect=owned_process.ChildProcessError()):
            with self.assertRaisesRegex(HttpFailure, 'LOCAL_FAILED'):
                self.client.fetch(self.selection, deadline=time.monotonic()+5)
            self.assertEqual(calls, [])
            self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'real Windows Job query failure injection')
    def test_query_failure_preserves_http_primary_and_retains_output_slot(self):
        active = owned_process._WindowsJob.active_processes
        def fail_after_independent_observation(job):
            # Separate test evidence permits later fixture cleanup; production
            # still sees an unknown query and must retain its output slot.
            self.assertEqual(active(job), 0)
            raise owned_process.ChildProcessError()
        with server('forbidden') as (port, calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port))), \
                mock.patch.object(owned_process._WindowsJob, 'active_processes', fail_after_independent_observation):
            with self.assertRaisesRegex(HttpFailure, 'HTTP_403') as caught:
                self.client.fetch(self.selection, deadline=time.monotonic()+5)
            self.assertIn('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED', caught.exception.secondary_errors)
            self.assertTrue(self.client.last_process_receipt['root_reaped'])
            self.assertFalse(self.client.last_process_receipt['tree_empty'])
            self.assertEqual(len(list(self.root.iterdir())), 1)
            self.assertEqual(len(calls), 1)

    def test_reap_failure_never_returns_completed_worker_bytes(self):
        wait = owned_process.subprocess.Popen.wait
        def fail_after_reap(process, *args, **kwargs):
            wait(process, *args, **kwargs)
            raise owned_process.subprocess.TimeoutExpired('TEST_ONLY_REAP_RESULT', 0)
        with server() as (port, calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port))), \
                mock.patch.object(owned_process.subprocess.Popen, 'wait', fail_after_reap):
            with self.assertRaisesRegex(HttpFailure, 'CLEANUP_FAILED'):
                self.client.fetch(self.selection, deadline=time.monotonic()+5)
            self.assertFalse(self.client.last_process_receipt['root_reaped'])
            self.assertTrue(self.client.last_process_receipt['tree_empty'])
            self.assertEqual(len(list(self.root.iterdir())), 1)
            self.assertEqual(len(calls), 1)

    def test_cancel_terminates_actual_descendant_and_late_output(self):
        marker = self.root / 'descendant-started'
        program = ('from bootstrap_kit import http_worker as w;import socket,time,subprocess\n'
            'def stall(*a,**k):\n'
            ' subprocess.Popen([sys.executable,"-I","-S","-c",' + repr('from pathlib import Path;import time;Path('+repr(str(marker))+').write_bytes(b"started");time.sleep(120)') + '])\n'
            ' time.sleep(120)\n'
            'socket.getaddrinfo=stall\nraise SystemExit(w.main())')
        cancel = threading.Event()
        def after_descendant():
            limit = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < limit:
                time.sleep(0.01)
            cancel.set()
        trigger = threading.Thread(target=after_descendant)
        trigger.start()
        try:
            with mock.patch.object(supervisor, '_command', test_command(program)), \
                    self.assertRaisesRegex(HttpFailure, 'BOOTSTRAP_HTTP_CANCELLED'):
                self.client.fetch(self.selection,deadline=time.monotonic()+5,cancel_event=cancel)
        finally:
            trigger.join(timeout=5)
            self.assertFalse(trigger.is_alive())
        self.assertTrue(marker.exists())
        self.closed()
        marker.unlink()

    def test_control_flood_and_missing_ready_fail_before_bytes_consume(self):
        for program in ('import time;time.sleep(120)', 'sys.stdout.buffer.write(b"x"*100000);sys.stdout.buffer.flush();import time;time.sleep(120)'):
            with self.subTest(program=program), mock.patch.object(supervisor, '_command', test_command(program)), \
                    self.assertRaises(HttpFailure):
                self.client.fetch(self.selection,deadline=time.monotonic()+0.5)
            self.closed()

    def test_late_cancel_or_deadline_preserves_primary_and_never_retries_delete(self):
        original_init = supervisor.UntrustedHttpObject.__init__
        actual_clock = time.monotonic
        for mode in ('cancel', 'deadline'):
            cancelled = threading.Event()
            expired = [False]
            deadline = actual_clock() + 5
            def initialize(output, *args, mode=mode, cancelled=cancelled, expired=expired, **kwargs):
                original_init(output, *args, **kwargs)
                if mode == 'cancel':
                    cancelled.set()
                else:
                    expired[0] = True
            def clock(deadline=deadline, expired=expired):
                return deadline + 1 if expired[0] else actual_clock()
            with self.subTest(mode=mode), server() as (port, _calls), \
                    mock.patch.object(supervisor, '_command', test_command(loopback_program(port))), \
                    mock.patch.object(supervisor.UntrustedHttpObject, '__init__', initialize), \
                    mock.patch.object(supervisor.time, 'monotonic', clock), \
                    mock.patch.object(supervisor, 'remove_owned_directory', side_effect=OSError('TEST_ONLY_DELETE_REJECTION')) as remove:
                with self.assertRaises(HttpFailure) as caught:
                    self.client.fetch(self.selection, deadline=deadline, cancel_event=cancelled)
                self.assertEqual(caught.exception.code,
                    'BOOTSTRAP_HTTP_CANCELLED' if mode == 'cancel' else 'BOOTSTRAP_HTTP_DEADLINE')
                self.assertIn('BOOTSTRAP_HTTP_OUTPUT_CLOSE_FAILED', caught.exception.secondary_errors)
                self.assertEqual(remove.call_count, 1)
                self.closed()
                self.assertEqual(set(supervisor._RETAINED_HTTP_RESOURCES), self._initial_retained)
            # Only a simulated rejection occurred; the reaped test slot remains
            # until the normal fixture teardown, never a real policy retry.

    def test_early_failure_clears_previous_process_receipt(self):
        with server() as (port, calls), mock.patch.object(supervisor, '_command', test_command(loopback_program(port))):
            for mode in ('cancel', 'invalid'):
                with self.client.fetch(self.selection, deadline=time.monotonic()+5):
                    self.closed()
                cancelled = threading.Event()
                if mode == 'cancel':
                    cancelled.set()
                with self.assertRaises(HttpFailure):
                    self.client.fetch(self.selection if mode == 'cancel' else None,
                        deadline=time.monotonic()+5, cancel_event=cancelled)
                self.assertIsNone(self.client.last_process_receipt)
            self.assertEqual(len(calls), 2)

    @unittest.skipUnless(os.name == 'nt', 'actual suspended worker ownership')
    def test_unconfirmed_constructor_cleanup_retains_actual_owner_on_fixed_failure(self):
        actual_stop = owned_process.OwnedProcess.stop_and_reap
        def uncertain_after_actual_reap(owner, *args, **kwargs):
            actual_stop(owner, *args, **kwargs)
            # Actual empty Job/root exit is known only to this TEST_ONLY fixture.
            # The public parent is deliberately given an unconfirmed result.
            raise owned_process.ChildProcessError()
        with mock.patch.object(owned_process._WindowsJob, 'assign_and_resume', side_effect=owned_process.ChildProcessError()), \
                mock.patch.object(owned_process.OwnedProcess, 'stop_and_reap', uncertain_after_actual_reap), \
                self.assertRaises(HttpFailure) as caught:
            self.client.fetch(self.selection, deadline=time.monotonic()+5)
        self.assertEqual(caught.exception.code, 'BOOTSTRAP_HTTP_LOCAL_FAILED')
        self.assertIn('BOOTSTRAP_HTTP_PROCESS_CLEANUP_FAILED', caught.exception.secondary_errors)
        self.assertTrue(caught.exception.cleanup_failed)
        owner = caught.exception._owned_process
        self.assertIsInstance(owner, owned_process.OwnedProcess)
        self.assertIsNotNone(owner.process.poll())
        self.assertTrue(owner.receipt['root_reaped'])
        self.assertTrue(owner.receipt['tree_empty'])
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_http_failure_retains_real_owner_and_pipe_objects_after_exception_gc(self):
        actual_stop = owned_process.OwnedProcess.stop_and_reap
        def uncertain_after_reap(owner, *args, **kwargs):
            actual_stop(owner, *args, **kwargs)
            raise owned_process.ChildProcessError()
        with server('forbidden') as (port, _calls), \
                mock.patch.object(supervisor, '_command', test_command(loopback_program(port))), \
                mock.patch.object(owned_process.OwnedProcess, 'stop_and_reap', uncertain_after_reap):
            with self.assertRaisesRegex(HttpFailure, 'HTTP_403') as caught:
                self.client.fetch(self.selection, deadline=time.monotonic()+5)
            record = caught.exception._retained_http_resources
            identity = record._identity
            reference = weakref.ref(record)
            self.assertTrue(record._owned.closed)
            self.assertTrue(record._owned.receipt['tree_empty'])
            del record, caught
            gc.collect()
            retained = reference()
            self.assertIs(retained, supervisor._RETAINED_HTTP_RESOURCES[identity])
            self.assertIsNotNone(retained._owned.process.poll())
            self.assertFalse(retained._reader.thread.is_alive())
            self.assertFalse(retained._writer.is_alive())
            observed = supervisor.retained_http_observations()
            self.assertIn(identity, [item['slot_identity'] for item in observed])
            self.assertNotIn(str(self.root), repr(observed))
