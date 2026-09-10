"""A minimal SOCKS5 server, enough to prove the Tor transport really works.

It speaks just enough of RFC 1928 to accept a CONNECT, record the address the
client asked for, and hand back a canned HTTP response. Recording the requested
address is the point: it proves the hostname reached the proxy rather than being
resolved locally, which is exactly what .onion depends on.
"""

from __future__ import annotations

import contextlib
import socket
import threading

SOCKS5_VERSION = 0x05
ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 0x01, 0x03, 0x04


class SocksStub:
    def __init__(self, response: bytes | None = None, fail_code: int = 0x00) -> None:
        self.response = response or (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 24\r\n\r\n<html><body>ok</body></html>"[:24 + 62]
        )
        self.fail_code = fail_code
        self.requested: list[tuple[str, int]] = []
        self.atypes: list[int] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.host, self.port = self._server.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"socks5://{self.host}:{self.port}"

    def __enter__(self) -> SocksStub:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            # Greeting: version, count, methods.
            header = conn.recv(2)
            if len(header) < 2 or header[0] != SOCKS5_VERSION:
                return
            conn.recv(header[1])
            conn.sendall(bytes([SOCKS5_VERSION, 0x00]))  # NO_AUTH

            # Request: version, command, reserved, address type.
            request = conn.recv(4)
            if len(request) < 4:
                return
            atyp = request[3]
            self.atypes.append(atyp)

            if atyp == ATYP_IPV4:
                host = socket.inet_ntoa(conn.recv(4))
            elif atyp == ATYP_DOMAIN:
                length = conn.recv(1)[0]
                host = conn.recv(length).decode("ascii")
            elif atyp == ATYP_IPV6:
                host = socket.inet_ntop(socket.AF_INET6, conn.recv(16))
            else:
                return
            port = int.from_bytes(conn.recv(2), "big")
            self.requested.append((host, port))

            reply = bytes([SOCKS5_VERSION, self.fail_code, 0x00, ATYP_IPV4])
            reply += socket.inet_aton("127.0.0.1") + (0).to_bytes(2, "big")
            conn.sendall(reply)
            if self.fail_code != 0x00:
                return

            conn.recv(65535)  # the HTTP request itself
            conn.sendall(self.response)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def http_response(body: str, status: str = "200 OK") -> bytes:
    payload = body.encode()
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode() + payload
