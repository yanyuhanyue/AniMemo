"""Only loopback CONNECT/TLS and owned workers; no public proxy or network."""
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bootstrap_kit import egress, http_supervisor as supervisor, seed
from bootstrap_kit.http_protocol import HttpFailure, HttpSelection, canonical
from bootstrap_kit.safe_files import create_private_directory, directory_identity, remove_owned_directory
from bootstrap_kit.tests.test_http_supervisor import test_command
from bootstrap_kit.tests.fixtures import NOW, selection, synthetic_kit


SELECTION = HttpSelection.github_json('/repos/yanyuhanyue/AniMemo/releases/tags/v2.0.0-rc.3')


def certificates(root, name):
    """A local test-only CA, never installed into the OS or shipped in a Kit."""
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'AniMemo local test CA')])
    ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256()))
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(issuer).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256()))
    ca_path, cert_path, key_path = root/'ca.pem', root/'leaf.pem', root/'key.pem'
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, cert_path, key_path


def tls_worker(ca_path=None, *, drop_argv=False):
    program = 'from bootstrap_kit import http_worker as w\n'
    if drop_argv:
        program += 'import sys;sys.argv=sys.argv[:2]\n'
    if ca_path is not None:
        program += ('original=w.ssl.create_default_context\n'
            'def context():\n c=original();c.load_verify_locations(cafile='+repr(str(ca_path))+' );return c\n'
            'w.ssl.create_default_context=context\n')
    return program + 'raise SystemExit(w.main())'


@contextmanager
def proxy_server(cert_path, key_path, mode='success'):
    calls, sni, failures = [], [], []
    started, stop = threading.Event(), threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]

    def read_headers(stream):
        raw = bytearray()
        while not raw.endswith(b'\r\n\r\n') and len(raw) < 8192:
            chunk = stream.recv(1)
            if not chunk:
                break
            raw.extend(chunk)
        return bytes(raw)

    def serve():
        connection = None
        try:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                    break
                except TimeoutError:
                    continue
            if connection is None:
                return
            connection.settimeout(3)
            calls.append(read_headers(connection))
            started.set()
            if mode == 'stall':
                stop.wait(5)
                return
            if mode == 'eof':
                return
            if mode in ('401', '403', '407', '429'):
                connection.sendall(('HTTP/1.1 '+mode+' Denied\r\nProxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\n\r\n').encode())
                return
            if mode == 'unsafe':
                connection.sendall(b'HTTP/1.1 200 Connection established\r\nProxy-Authenticate: Basic\r\n\r\n')
                return
            connection.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            if mode == 'tls-alert':
                # A real fatal TLS handshake_failure alert, before application data.
                connection.recv(4096)
                connection.sendall(b'\x15\x03\x03\x00\x02\x02\x28')
                return
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert_path, key_path)
            server_context.set_servername_callback(lambda _sock, name, _ctx: sni.append(name))
            connection = server_context.wrap_socket(connection, server_side=True)
            calls.append(read_headers(connection))
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}')
        except (OSError, ssl.SSLError) as error:
            # Local cert-rejection cases legitimately abort server handshake.
            failures.append(type(error).__name__)
        finally:
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=serve, name='animemo-connect-test')
    thread.start()
    try:
        yield port, calls, sni, started
    finally:
        stop.set()
        thread.join(5)
        listener.close()
        if thread.is_alive():
            raise AssertionError('Local CONNECT server not closed')


class EgressTests(unittest.TestCase):
    def setUp(self):
        self.root = create_private_directory(Path(tempfile.gettempdir()), prefix='egress-tests-')
        self.identity = directory_identity(self.root)
        self.ca, self.cert, self.key = certificates(self.root, 'api.github.com')

    def tearDown(self):
        remove_owned_directory(self.root, self.identity)
        self.assertFalse(any(t.name in {'animemo-connect-test', 'animemo-http-control', 'animemo-http-go'}
            for t in threading.enumerate()))

    def client(self, port):
        return supervisor.SupervisedAnonymousHttp(private_root=self.root,
            egress=egress.EgressSelection('http://127.0.0.1:'+str(port)))

    def closed(self, client):
        self.assertTrue(client.last_process_receipt['root_reaped'])
        self.assertTrue(client.last_process_receipt['tree_empty'])
        self.assertFalse(client.last_process_receipt['failures'])

    def test_only_canonical_numeric_loopback_http_endpoints(self):
        for invalid in ('http://localhost:10808', 'https://127.0.0.1:10808',
            'socks5://127.0.0.1:10808', 'http://user:pass@127.0.0.1:10808',
            'http://127.0.0.1:10808/', 'http://127.0.0.1:10808?q=x',
            'http://127.0.0.1:10808#x', 'http://127.0.0.2:10808',
            'http://[::ffff:127.0.0.1]:10808', 'http://127.0.0.1:0',
            'http://127.0.0.1:65536', 'http://127.0.0.1:010808', '', 10808):
            with self.subTest(endpoint=invalid), self.assertRaises(HttpFailure):
                egress.EgressSelection(invalid)
        for endpoint in ('http://127.0.0.1:10808', 'http://[::1]:10808'):
            selected = egress.EgressSelection(endpoint)
            self.assertEqual(egress.EgressSelection.from_record(selected.record()), selected)

    def test_no_implicit_environment_or_no_proxy_selection_and_offline_rejects_proxy(self):
        with mock.patch.dict(os.environ, {'HTTPS_PROXY':'http://secret@invalid:9', 'NO_PROXY':'*'}):
            self.assertIsNone(egress.select_egress().endpoint)
            selected = egress.EgressSelection('http://127.0.0.1:10808')
            self.assertEqual(egress.select_egress(selected), selected)
        with self.assertRaisesRegex(HttpFailure, 'EGRESS_OFFLINE_FORBIDDEN'):
            supervisor.SupervisedAnonymousHttp(private_root=self.root, egress=selected, offline=True)
        client = supervisor.SupervisedAnonymousHttp(private_root=self.root, offline=True)
        with mock.patch.object(supervisor, 'OwnedProcess') as process:
            with self.assertRaisesRegex(HttpFailure, 'OFFLINE_NETWORK_FORBIDDEN'):
                client.fetch(SELECTION, deadline=time.monotonic()+2)
            process.assert_not_called()

    def test_runtime_pair_rejects_missing_or_mismatched_field(self):
        selected = egress.EgressSelection('http://127.0.0.1:10808')
        for endpoint, identity in ((selected.endpoint, None), (None, selected.identity),
            (selected.endpoint, '0'*64)):
            with self.assertRaisesRegex(HttpFailure, 'EGRESS_BINDING_MISMATCH'):
                egress.bound_runtime_egress(endpoint, identity)
        self.assertEqual(egress.bound_runtime_egress(selected.endpoint, selected.identity), selected)

    def test_seed_forwards_explicit_config_to_real_isolated_child_only(self):
        selected = egress.EgressSelection('http://127.0.0.1:10808')
        program = ('import sys,os,json\n'
            'a=sys.argv\n'
            'assert a[a.index("--egress-proxy")+1]=='+repr(selected.endpoint)+'\n'
            'assert a[a.index("--egress-sha256")+1]=='+repr(selected.identity)+'\n'
            'assert os.environ["ANIMEMO_EXPECTED_EGRESS_SHA256"]=='+repr(selected.identity)+'\n'
            'assert "HTTPS_PROXY" not in os.environ and "NO_PROXY" not in os.environ\n'
            'print(json.dumps({"state":"TEST_ONLY","production_authority_granted":False}))\n')
        fixture, _, _ = synthetic_kit(self.root, runtime=program)
        import json
        value = json.loads(fixture['paths']['manifest'].read_bytes())
        kit = seed.verify_local(manifest_path=fixture['paths']['manifest'], archive_path=fixture['paths']['archive'],
            selection=selection(value), destination_parent=self.root, now=NOW)
        with kit, mock.patch.dict(os.environ, {'HTTPS_PROXY':'http://user:secret@invalid:9', 'NO_PROXY':'*'}):
            result = seed.run_local(kit, version='v2.0.0-rc.3', output=self.root, egress=selected, timeout=5)
        self.assertEqual(result, {'state':'TEST_ONLY', 'production_authority_granted':False})

    def test_real_connect_tls_keeps_target_sni_and_has_no_credentials(self):
        with proxy_server(self.cert, self.key) as (port, calls, sni, _), \
             mock.patch.object(supervisor, '_command', side_effect=test_command(tls_worker(self.ca))), \
             mock.patch.dict(os.environ, {'HTTPS_PROXY':'http://user:secret@invalid:9','NO_PROXY':'*'}):
            client = self.client(port)
            with client.fetch(SELECTION, deadline=time.monotonic()+8) as result:
                self.assertEqual(result.read_bytes(), b'{}')
                self.assertEqual(result.observations[0]['egress'], {
                    'mode':'HTTP_CONNECT','endpoint':'http://127.0.0.1:'+str(port),
                    'target':'api.github.com','connect':'ESTABLISHED'})
            self.closed(client)
        self.assertEqual(sni, ['api.github.com'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith(b'CONNECT api.github.com:443 HTTP/1.1\r\n'))
        self.assertTrue(calls[1].startswith(b'GET /repos/yanyuhanyue/AniMemo/'))
        for message in calls:
            self.assertNotIn(b'authorization:', message.lower())
            self.assertNotIn(b'cookie:', message.lower())
            self.assertNotIn(b'secret', message.lower())

    def test_connect_rejections_and_eof_never_fall_back(self):
        for mode, code in (('401','PROXY_HTTP_401'),('403','PROXY_HTTP_403'),('407','PROXY_HTTP_407'),('429','PROXY_HTTP_429'),
                          ('eof','PROXY_CONNECT_EOF'),('unsafe','PROXY_CONNECT_INVALID')):
            with self.subTest(mode=mode), proxy_server(self.cert, self.key, mode) as (port, calls, _, _):
                client = self.client(port)
                with self.assertRaisesRegex(HttpFailure, 'BOOTSTRAP_HTTP_'+code):
                    client.fetch(SELECTION, deadline=time.monotonic()+5)
                self.closed(client)
            self.assertEqual(len(calls), 1)

    def test_real_tls_hostname_and_untrusted_ca_are_rejected(self):
        for mode in ('hostname', 'ca'):
            if mode == 'hostname':
                ca, cert, key = certificates(self.root, 'wrong.invalid')
                program = tls_worker(ca)
            else:
                cert, key, program = self.cert, self.key, tls_worker()
            with self.subTest(mode=mode), proxy_server(cert, key) as (port, calls, _, _), \
                 mock.patch.object(supervisor, '_command', side_effect=test_command(program)):
                client = self.client(port)
                with self.assertRaisesRegex(HttpFailure, 'TLS_IDENTITY_FAILED'):
                    client.fetch(SELECTION, deadline=time.monotonic()+5)
                self.closed(client)
            self.assertEqual(len(calls), 1)

    def test_tls_protocol_alert_is_not_an_ordinary_network_retry_failure(self):
        with proxy_server(self.cert, self.key, 'tls-alert') as (port, calls, _, _):
            client = self.client(port)
            with self.assertRaisesRegex(HttpFailure, 'TLS_HANDSHAKE_FAILED'):
                client.fetch(SELECTION, deadline=time.monotonic()+5)
            self.closed(client)
        self.assertEqual(len(calls), 1)

    def test_connect_stall_deadline_and_cancel_reap_real_worker(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), proxy_server(self.cert, self.key, 'stall') as (port, _, _, started):
                event = threading.Event()
                def cancellation():
                    if started.wait(3):
                        event.set()
                thread = threading.Thread(target=cancellation) if cancel else None
                if thread:
                    thread.start()
                client = self.client(port)
                try:
                    with self.assertRaisesRegex(HttpFailure, 'CANCELLED' if cancel else 'DEADLINE'):
                        client.fetch(SELECTION, deadline=time.monotonic()+(4 if cancel else 2.5), cancel_event=event)
                finally:
                    if thread:
                        thread.join(4)
                        self.assertFalse(thread.is_alive())
                self.closed(client)
                self.assertTrue(started.is_set(), 'Failure must occur after actual CONNECT was received')

    def test_worker_argv_loss_and_go_loss_never_connect(self):
        for mode in ('argv','go','go-mismatch'):
            with self.subTest(mode=mode), proxy_server(self.cert, self.key) as (port, calls, _, _):
                client = self.client(port)
                def go_bytes(value):
                    if value.get('event') == 'GO':
                        value = dict(value)
                        if mode == 'go':
                            value.pop('egress')
                        elif mode == 'go-mismatch':
                            value['egress'] = {'mode':'HTTP_CONNECT','endpoint':'http://127.0.0.1:1'}
                    return canonical(value)
                with mock.patch.object(supervisor, '_command', side_effect=test_command(tls_worker(drop_argv=mode=='argv'))), \
                     mock.patch.object(supervisor, 'canonical', side_effect=go_bytes if mode!='argv' else canonical):
                    with self.assertRaises(HttpFailure):
                        client.fetch(SELECTION, deadline=time.monotonic()+4)
                self.closed(client)
            self.assertEqual(calls, [])

    def test_unsafe_target_rejected_before_worker_creation(self):
        client = self.client(10808)
        with mock.patch.object(supervisor, 'OwnedProcess') as process:
            with self.assertRaises(HttpFailure):
                client.fetch(HttpSelection('GITHUB_JSON',{'path':'https://example.com/'}), deadline=time.monotonic()+2)
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
