"""
Local proxy bridge for Chromium.

Chromium cannot use user:pass in --proxy-server (ERR_NO_SUPPORTED_PROXIES)
and modern Chromium rejects MV2 proxy-auth extensions.

Solution:
  Chrome  →  http://127.0.0.1:<local>  (no auth)
  Bridge  →  upstream host:port with Proxy-Authorization: Basic …

Supports HTTP CONNECT (HTTPS sites) and plain HTTP forward.
"""

from __future__ import annotations

import base64
import select
import socket
import threading
import time
from typing import Optional, Tuple


def _recv_until(conn: socket.socket, marker: bytes = b"\r\n\r\n", limit: int = 65536) -> bytes:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _pipe(a: socket.socket, b: socket.socket, timeout: float = 120.0) -> None:
    sockets = [a, b]
    end = time.time() + timeout
    try:
        while time.time() < end:
            r, _, x = select.select(sockets, [], sockets, 1.0)
            if x:
                break
            if not r:
                continue
            for s in r:
                other = b if s is a else a
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


class LocalProxyBridge:
    """
    Listen on 127.0.0.1:ephemeral and forward to an authenticated upstream proxy.
    """

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        username: str = "",
        password: str = "",
        *,
        scheme: str = "http",
    ):
        self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        self.username = username or ""
        self.password = password or ""
        self.scheme = (scheme or "http").lower()
        if self.username or self.password:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode("utf-8")
            ).decode("ascii")
            self._auth_header = f"Proxy-Authorization: Basic {token}\r\n"
        else:
            self._auth_header = ""

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.port: int = 0

    @property
    def chrome_server(self) -> str:
        """Value for Chromium --proxy-server (no credentials)."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.chrome_server

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        srv.settimeout(1.0)
        self._sock = srv
        self.port = int(srv.getsockname()[1])
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name=f"proxy-bridge-{self.port}",
            daemon=True,
        )
        self._thread.start()
        return self.chrome_server

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self.port = 0

    def _serve(self) -> None:
        srv = self._sock
        if srv is None:
            return
        while not self._stop.is_set():
            try:
                client, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
            )
            t.start()

    def _connect_upstream(self) -> socket.socket:
        up = socket.create_connection(
            (self.upstream_host, self.upstream_port),
            timeout=20,
        )
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return up

    def _handle_client(self, client: socket.socket) -> None:
        up: Optional[socket.socket] = None
        try:
            client.settimeout(30)
            head = _recv_until(client)
            if not head:
                client.close()
                return

            # First line: METHOD target PROTO
            try:
                first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            except Exception:
                client.close()
                return
            bits = first.split()
            if len(bits) < 2:
                client.close()
                return
            method = bits[0].upper()
            target = bits[1]

            up = self._connect_upstream()
            up.settimeout(30)

            if method == "CONNECT":
                # Tunnel: Chrome → us → upstream CONNECT host:443
                # Failures surface in Chromium as ERR_TUNNEL_CONNECTION_FAILED
                # (common on flaky residential when auth hops to auth.grokipedia.com).
                last_status = b""
                ok = False
                for attempt in range(1, 3):  # 2 tries
                    if up is None:
                        up = self._connect_upstream()
                        up.settimeout(45)
                    req = (
                        f"CONNECT {target} HTTP/1.1\r\n"
                        f"Host: {target}\r\n"
                        f"{self._auth_header}"
                        f"Proxy-Connection: keep-alive\r\n"
                        f"Connection: keep-alive\r\n"
                        f"\r\n"
                    ).encode("latin-1")
                    try:
                        up.sendall(req)
                        resp = _recv_until(up)
                    except OSError:
                        try:
                            up.close()
                        except Exception:
                            pass
                        up = None
                        continue
                    first_line = resp.split(b"\r\n", 1)[0] if resp else b""
                    last_status = first_line
                    # HTTP/1.x 200 Connection established (space variants)
                    ok = (
                        b" 200 " in first_line
                        or first_line.endswith(b" 200")
                        or b" 200\r" in first_line + b"\r"
                    )
                    if ok:
                        break
                    # retry on 5xx / empty / 407 once with fresh upstream
                    try:
                        up.close()
                    except Exception:
                        pass
                    up = None
                    time.sleep(0.15 * attempt)

                if not ok or up is None:
                    # Chromium shows ERR_TUNNEL_CONNECTION_FAILED for non-200 CONNECT
                    try:
                        client.sendall(
                            b"HTTP/1.1 502 Bad Gateway\r\n"
                            b"Content-Type: text/plain\r\n"
                            b"Connection: close\r\n"
                            b"Content-Length: "
                            + str(len(last_status) + 32).encode()
                            + b"\r\n\r\n"
                            + b"upstream CONNECT failed: "
                            + (last_status or b"(empty)")
                        )
                    except OSError:
                        pass
                    return
                try:
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                except OSError:
                    return
                client.settimeout(None)
                up.settimeout(None)
                _pipe(client, up, timeout=300.0)
                return

            # Plain HTTP: rewrite absolute-form request through upstream
            # head may be absolute URL: GET http://host/path HTTP/1.1
            # Inject Proxy-Authorization; strip any existing one
            lines = head.split(b"\r\n")
            out_lines = []
            for i, line in enumerate(lines):
                if i == 0:
                    out_lines.append(line)
                    continue
                if not line:
                    continue
                low = line.lower()
                if low.startswith(b"proxy-authorization:"):
                    continue
                if low.startswith(b"proxy-connection:"):
                    continue
                out_lines.append(line)
            # rebuild
            body_sep = head.find(b"\r\n\r\n")
            body = head[body_sep + 4 :] if body_sep >= 0 else b""
            new_head = b"\r\n".join(out_lines)
            if self._auth_header:
                new_head += b"\r\n" + self._auth_header.strip().encode("latin-1")
            new_head += b"\r\n\r\n"
            up.sendall(new_head + body)
            client.settimeout(None)
            up.settimeout(None)
            _pipe(client, up)
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            if up is not None:
                try:
                    up.close()
                except Exception:
                    pass


# Process-wide active bridge (one upstream at a time per worker process)
_active: Optional[LocalProxyBridge] = None
_active_key: Optional[Tuple[str, int, str, str]] = None
_lock = threading.Lock()


def ensure_local_bridge(
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    *,
    scheme: str = "http",
) -> str:
    """
    Start or reuse a local bridge to the given upstream.
    Returns chrome --proxy-server value: http://127.0.0.1:PORT
    """
    global _active, _active_key
    key = (host, int(port), username or "", password or "")
    with _lock:
        if _active is not None and _active_key == key and _active.port:
            return _active.chrome_server
        # restart if upstream changed
        if _active is not None:
            try:
                _active.stop()
            except Exception:
                pass
            _active = None
            _active_key = None
        bridge = LocalProxyBridge(
            host, int(port), username, password, scheme=scheme
        )
        chrome = bridge.start()
        _active = bridge
        _active_key = key
        return chrome


def stop_local_bridge() -> None:
    global _active, _active_key
    with _lock:
        if _active is not None:
            try:
                _active.stop()
            except Exception:
                pass
        _active = None
        _active_key = None
