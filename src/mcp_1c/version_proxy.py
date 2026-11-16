"""TCP proxy between the test manager and a test client of another platform version.

The test manager and the thin test client exchange the platform version in the opening messages
(plain text such as ``{"S","8.5.1.1343"}``) and refuse to work when they differ.  The protocol is the same
within a platform branch (checked: manager 8.5.4.1878 with client 8.5.1.1343), so the proxy presents the manager
as the client's version and the client as the manager's: this lets the manager run on any installed platform
when the target platform (for example, a cloud's) has only a thin client.
"""

from __future__ import annotations

import logging
import socket
import threading

LOGGER = logging.getLogger(__name__)


class VersionProxy:
    """Forwards a manager connection to the test client port, rewriting the platform version both ways."""

    def __init__(self, client_host: str, client_port: int, manager_version: str, client_version: str) -> None:
        self.client_host = client_host
        self.client_port = client_port
        self.manager_version = manager_version.encode()
        self.client_version = client_version.encode()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1] if self._listener else 0

    def start(self) -> int:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="version-proxy", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            sockets, self._sockets = list(self._sockets), set()
        for sock in sockets:
            _close(sock)

    def rewrite(self, data: bytes, to_client: bool) -> bytes:
        if self.manager_version == self.client_version:
            return data
        if to_client:
            return data.replace(self.manager_version, self.client_version)
        return data.replace(self.client_version, self.manager_version)

    def _accept_loop(self) -> None:
        while True:
            listener = self._listener
            if listener is None:
                return
            try:
                manager_side, _ = listener.accept()
                client_side = socket.create_connection((self.client_host, self.client_port), timeout=10)
                client_side.settimeout(None)
            except OSError:
                if self._listener is None:
                    return
                continue
            with self._lock:
                self._sockets.update({manager_side, client_side})
            threading.Thread(target=self._pipe, args=(manager_side, client_side, True), daemon=True).start()
            threading.Thread(target=self._pipe, args=(client_side, manager_side, False), daemon=True).start()

    def _pipe(self, source: socket.socket, target: socket.socket, to_client: bool) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(self.rewrite(data, to_client))
        except OSError:
            pass
        finally:
            _close(source)
            _close(target)
            with self._lock:
                self._sockets.discard(source)
                self._sockets.discard(target)


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
