"""A process-owned loopback ingress for an isolated Candidate Web container.

Docker does not publish host ports for an internal-only bridge. Keep the
configured local endpoint usable during acceptance without giving containers an
external network or changing Doctor's endpoint. The Installer supplies the
already verified Web address; peers cannot choose a destination.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import threading
import time


class CandidateLoopbackListener:
    """One bounded, transparent connection at a time; no payload logging."""

    def __init__(self, listen: tuple[str, int], target: tuple[str, int]) -> None:
        address = ipaddress.ip_address(listen[0])
        if not address.is_loopback or not 1 <= listen[1] <= 65535:
            raise ValueError("Candidate ingress must use the configured loopback")
        self.target = target
        self.endpoint = listen
        self._stop = threading.Event()
        self._listener = socket.socket(
            socket.AF_INET6 if address.version == 6 else socket.AF_INET,
            socket.SOCK_STREAM,
        )
        try:
            # No SO_REUSEPORT or fallback port: another owner is a hard failure.
            self._listener.bind(listen)
            self._listener.listen(4)
            self._listener.settimeout(0.1)
        except BaseException:
            self._listener.close()
            raise
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def require_active(self) -> None:
        if self._stop.is_set() or not self._thread.is_alive():
            raise OSError("Candidate loopback ingress is unavailable")
        if self._listener.getsockname()[:2] != self.endpoint:
            raise OSError("Candidate loopback ingress changed")

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with client:
                try:
                    with socket.create_connection(self.target, timeout=2) as upstream:
                        self._forward(client, upstream)
                except OSError:
                    # The real probe observes transport failure; never synthesize
                    # a successful response or emit an exception/payload.
                    continue

    def _forward(self, client: socket.socket, upstream: socket.socket) -> None:
        peers = {client: upstream, upstream: client}
        pending = {client: bytearray(), upstream: bytearray()}
        reading = set(peers)
        written_eof: set[socket.socket] = set()
        total = 0
        deadline = time.monotonic() + 30
        for item in peers:
            item.setblocking(False)
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                for source, destination in peers.items():
                    if (
                        source not in reading
                        and not pending[destination]
                        and destination not in written_eof
                    ):
                        destination.shutdown(socket.SHUT_WR)
                        written_eof.add(destination)
                if not reading and not any(pending.values()):
                    return
                ready, writable, _ = select.select(
                    [item for item in reading if len(pending[peers[item]]) < 65536],
                    [item for item in peers if pending[item]],
                    [],
                    0.1,
                )
                for item in ready:
                    chunk = item.recv(65536 - len(pending[peers[item]]))
                    if not chunk:
                        reading.remove(item)
                        continue
                    total += len(chunk)
                    if total > 16 * 1024 * 1024:
                        return
                    pending[peers[item]].extend(chunk)
                for item in writable:
                    sent = item.send(pending[item])
                    if sent == 0:
                        return
                    del pending[item][:sent]
        finally:
            for buffer in pending.values():
                buffer[:] = bytes(len(buffer))
                buffer.clear()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            raise OSError("Candidate loopback ingress cleanup failed")
