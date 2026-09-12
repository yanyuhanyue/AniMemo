from __future__ import annotations

import socket
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from installer.candidate_listener import CandidateLoopbackListener
from installer.production import ProductionFreshInstallPort


def free_port():
    with socket.socket() as source:
        source.bind(("127.0.0.1", 0))
        return source.getsockname()[1]


class CandidateListenerTests(unittest.TestCase):
    def test_real_duplex_stream_and_half_close_preserve_bytes(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(5)
            payload = bytes(range(256)) * 2048
            observed = []

            def respond():
                with server.accept()[0] as peer:
                    peer.settimeout(5)
                    body = bytearray()
                    while chunk := peer.recv(65536):
                        body.extend(chunk)
                    observed.append(bytes(body))
                    peer.sendall(payload[::-1])

            worker = threading.Thread(target=respond)
            worker.start()
            relay = CandidateLoopbackListener(
                ("127.0.0.1", free_port()), server.getsockname()
            )
            try:
                relay.require_active()
                with socket.create_connection(relay.endpoint, timeout=5) as client:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                    response = bytearray()
                    while chunk := client.recv(65536):
                        response.extend(chunk)
                self.assertEqual(response, payload[::-1])
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(observed, [payload])
            finally:
                relay.close()
            with self.assertRaises(OSError):
                relay.require_active()
            with self.assertRaises(OSError):
                socket.create_connection(relay.endpoint, timeout=1)

    def test_close_interrupts_an_idle_accepted_connection(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            server.settimeout(5)
            relay = CandidateLoopbackListener(
                ("127.0.0.1", free_port()), server.getsockname()
            )
            with socket.create_connection(relay.endpoint, timeout=2) as client:
                with server.accept()[0]:
                    relay.close()
                    client.settimeout(2)
                    self.assertEqual(client.recv(1), b"")
            self.assertFalse(relay._thread.is_alive())

    def test_rejects_external_bind_and_existing_port_owner(self):
        with self.assertRaises(ValueError):
            CandidateLoopbackListener(("0.0.0.0", 18080), ("172.30.0.2", 80))
        with socket.socket() as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen(1)
            with self.assertRaises(OSError):
                CandidateLoopbackListener(owner.getsockname(), ("172.30.0.2", 80))

    def test_candidate_revalidates_web_network_target_and_endpoint(self):
        fresh = ProductionFreshInstallPort(
            releases=mock.Mock(),
            configuration=mock.Mock(),
            candidate_network_isolation=True,
        )
        fresh._validate_candidate_edge_proxy = mock.Mock()
        deployment = mock.Mock()
        deployment.paths = SimpleNamespace(listen_host="127.0.0.1", listen_port=18080)
        deployment.exact_web_proxy.return_value = "172.30.0.2/32"
        listener = mock.Mock(target=("172.30.0.2", 80), endpoint=("127.0.0.1", 18080))
        with mock.patch(
            "installer.candidate_listener.CandidateLoopbackListener",
            return_value=listener,
        ):
            fresh._start_candidate_listener(deployment, {})
        listener.require_active.assert_called_once()
        deployment.exact_web_proxy.return_value = "172.30.0.3/32"
        with self.assertRaises(OSError):
            fresh._validate_candidate_listener(deployment, {})
        deployment.exact_web_proxy.return_value = "172.30.0.2/32"
        fresh._validate_candidate_edge_proxy.side_effect = OSError("changed")
        with self.assertRaises(OSError):
            fresh._validate_candidate_listener(deployment, {})
        fresh.close_candidate_listener()
        listener.close.assert_called_once()
        self.assertIsNone(fresh._candidate_listener)


if __name__ == "__main__":
    unittest.main()
