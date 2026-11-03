"""A small CDP filtering proxy between chrome-devtools-mcp and Browsr's Chromium.

Why this exists. Browsr promises that it never grabs your screen, and that
coding agents open pages without taking focus. But the agents drive the browser
through chrome-devtools-mcp, and that tool sends DevTools commands that raise
the window: `Page.bringToFront` and `Target.activateTarget` bring a tab to the
front, `Target.createTarget` opens foreground tabs by default, and an isolated
browser context always opens a NEW WINDOW (a tab of another context cannot live
in the existing window), which Hyprland then focuses, stealing keyboard and
pointer focus in the middle of the user's typing.

The MCP server offers no switch for any of that, so the commands are removed
on the way. The proxy listens on 127.0.0.1 only, looks like a Chromium DevTools
endpoint to the MCP server (same /json/* pages, same WebSocket paths) and
forwards everything to the real endpoint, except for the handful of commands
below. Messages it does not change are forwarded as the original bytes, so the
proxy cannot corrupt traffic it has no opinion about.

Agents never get separate logins or isolated contexts in Browsr: they work in
the tabs of the existing profile, in this workspace's group. That is an owner
decision, which is why createBrowserContext is refused rather than tamed.

Standard library only: http.server for the plain HTTP pages and a minimal
RFC 6455 WebSocket for both sides.
"""

import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BRIDGE = Path(__file__).resolve().parent / "bridge.py"

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

REFUSED_CONTEXT = (
    "Browsr: separate browser contexts are disabled for agents; "
    "work in this workspace's tabs"
)

# Commands that exist only to raise a window. Answering them with an empty
# result keeps the MCP server happy (it waits for a reply) and changes nothing.
SWALLOWED = {"Page.bringToFront", "Target.activateTarget"}


# ------------------------------------------------------ finding the browser

class BrowserDown(Exception):
    """Browsr is not running (or not answering) right now."""


class Endpoint:
    """Where the real DevTools endpoint is, asked for lazily.

    The proxy has to outlive the browser: an agent session that starts while
    Browsr is down, or while it restarts, must not be left without its browser
    tool for good (Claude Code never retries a server that failed to start).
    So nothing is resolved at start. Every request asks here; a successful
    answer is kept for a few seconds, and dropped as soon as a connection to it
    fails, so that a restarted browser on a new address is found at once.
    """

    def __init__(self, resolve, ttl=5.0):
        self._resolve = resolve
        self._ttl = ttl
        self._cached = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._cached and time.monotonic() - self._at < self._ttl:
                return self._cached
            try:
                raw = self._resolve()
            except BrowserDown:
                self._cached = None
                raise
            except Exception as error:
                self._cached = None
                raise BrowserDown(str(error)) from error
            parts = urlsplit(raw)
            self._cached, self._at = (parts.hostname, parts.port), time.monotonic()
            return self._cached

    def invalidate(self):
        with self._lock:
            self._cached = None


def resolve_with_bridge():
    """The default resolver: the same logic `bridge.py mcp-endpoint` runs."""
    spec = importlib.util.spec_from_file_location("browsr_bridge", BRIDGE)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge.devtools_url()


# ---------------------------------------------------------------- the filter

def filter_message(data):
    """Decide what to do with one message sent by the MCP server.

    Returns ("forward", bytes) to send to Chromium, ("reply", bytes) to send
    straight back to the MCP server, or ("open_tab", message) when the tab has
    to be created through Browsr's extension instead. Untouched messages come
    back as the very bytes that went in.
    """
    try:
        message = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return "forward", data
    if not isinstance(message, dict):
        return "forward", data
    method = message.get("method")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    if method in SWALLOWED:
        return "reply", _reply(message, {"result": {}})
    if method == "Target.createBrowserContext":
        return "reply", _reply(message, _refusal())
    if method == "Target.createTarget":
        if params.get("browserContextId"):
            return "reply", _reply(message, _refusal())
        # Not forwarded at all. Measured on the real window: even with
        # background:true, a tab created over DevTools raises the browser window
        # and takes the user's focus. The extension makes the tab instead
        # (chrome.tabs.create with active:false leaves the window alone) and the
        # proxy then hands the new tab's id back as if Chromium had made it.
        url = params.get("url") or "about:blank"
        if url != "about:blank":
            return "reply", _reply(message, {"error": {
                "code": -32000,
                "message": "Browsr: agents open new tabs as about:blank and navigate afterwards",
            }})
        return "open_tab", message
    return "forward", data


def _refusal():
    return {"error": {"code": -32000, "message": REFUSED_CONTEXT}}


def _reply(message, body):
    reply = {"id": message.get("id")}
    if "sessionId" in message:
        reply["sessionId"] = message["sessionId"]
    reply.update(body)
    return json.dumps(reply, separators=(",", ":")).encode()


# ------------------------------------------------------- WebSocket plumbing

def _recv_exact(sock, count):
    chunks = []
    while count:
        chunk = sock.recv(min(count, 1 << 20))
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_frame(sock):
    first, second = _recv_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack(">Q", _recv_exact(sock, 8))
    mask = _recv_exact(sock, 4) if second & 0x80 else None
    payload = _recv_exact(sock, length)
    if mask:
        payload = _xor_mask(payload, mask)
    return fin, opcode, payload


def _xor_mask(payload, mask):
    # Big payloads (screenshots) make a per-byte Python loop far too slow, so
    # mask with one big-integer XOR against the repeated key.
    if not payload:
        return payload
    repeated = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


def write_frame(sock, opcode, payload, masked):
    header = bytearray([0x80 | opcode])
    flag = 0x80 if masked else 0
    length = len(payload)
    if length < 126:
        header.append(flag | length)
    elif length < 1 << 16:
        header.append(flag | 126)
        header += struct.pack(">H", length)
    else:
        header.append(flag | 127)
        header += struct.pack(">Q", length)
    if masked:
        mask = os.urandom(4)
        header += mask
        payload = _xor_mask(payload, mask)
    sock.sendall(bytes(header) + payload)


class Peer:
    """One end of a WebSocket: a socket, whether we mask, and a write lock.

    Two threads write to the same socket (the pump for the other direction and
    this side's own replies/pongs), so frames must not interleave.
    """

    def __init__(self, sock, masked):
        self.sock = sock
        self.masked = masked
        self.lock = threading.Lock()

    def send(self, opcode, payload):
        with self.lock:
            write_frame(self.sock, opcode, payload, self.masked)

    def messages(self):
        """Yield whole (opcode, payload) data messages; answer pings, stop on close."""
        opcode, parts = None, []
        while True:
            fin, op, payload = read_frame(self.sock)
            if op == OP_PING:
                self.send(OP_PONG, payload)
            elif op == OP_PONG:
                continue
            elif op == OP_CLOSE:
                try:
                    self.send(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return
            else:
                if op != OP_CONT:
                    opcode, parts = op, []
                parts.append(payload)
                if fin:
                    yield opcode, b"".join(parts)
                    opcode, parts = None, []

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def _connect_upstream(host, port, path):
    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    # No Origin header: Chromium refuses cross-origin DevTools sockets.
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("upstream closed during handshake")
        response += chunk
    head, _, rest = response.partition(b"\r\n\r\n")
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise ConnectionError("upstream refused the WebSocket: " + head.split(b"\r\n")[0].decode(errors="replace"))
    if rest:
        raise ConnectionError("unexpected data after upstream handshake")
    sock.settimeout(None)
    return sock


# ----------------------------------------------------------------- the proxy

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        if path.lower().startswith(("/devtools/browser/", "/devtools/page/")):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                return self._websocket(path)
        if path.rstrip("/") in ("/json/version", "/json", "/json/list"):
            return self._json(path)
        self.send_error(404)

    def _unavailable(self, message):
        # 503 with the reason in plain text: the MCP server shows it to the agent
        # as its connection error, and retries on the next tool call.
        body = message.encode()
        self.send_response(503)
        self.send_header("Content-Type", "text/plain; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, path):
        try:
            real, payload = self.server.endpoint_call(
                lambda real: json.loads(_fetch(real, path))
            )
        except BrowserDown as down:
            return self._unavailable(str(down))
        except (OSError, ValueError):
            return self.send_error(502)
        body = json.dumps(_rewrite(payload, f"{real[0]}:{real[1]}", f"127.0.0.1:{self.server.server_port}")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self, path):
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            _, upstream_sock = self.server.endpoint_call(
                lambda real: _connect_upstream(real[0], real[1], path)
            )
        except BrowserDown as down:
            return self._unavailable(str(down))
        except OSError:
            return self.send_error(502)
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.wfile.write((
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())
        self.wfile.flush()
        self.close_connection = True
        # client = the MCP server (we send unmasked); upstream = Chromium (we mask).
        client = Peer(self.connection, masked=False)
        upstream = Peer(upstream_sock, masked=True)
        done = threading.Event()

        def to_client():
            try:
                for opcode, payload in upstream.messages():
                    client.send(opcode, payload)
            except OSError:
                pass
            finally:
                done.set()
                client.close()

        threading.Thread(target=to_client, daemon=True).start()
        try:
            for opcode, payload in client.messages():
                if opcode == OP_TEXT:
                    action, out = filter_message(payload)
                    if action == "reply":
                        client.send(OP_TEXT, out)
                        continue
                    if action == "open_tab":
                        # Off the read loop: finding the tab can take a second
                        # and the MCP server keeps sending meanwhile.
                        threading.Thread(
                            target=self._create_tab, args=(client, out), daemon=True
                        ).start()
                        continue
                    payload = out
                upstream.send(opcode, payload)
        except OSError:
            pass
        finally:
            upstream.close()
            done.wait(2)
            client.close()


    def _create_tab(self, client, message):
        nonce = os.urandom(6).hex()
        url = f"about:blank#browsr-{nonce}"
        try:
            self.server.open_tab(url)
            target_id = _find_target(self.server, url, timeout=10)
            body = {"result": {"targetId": target_id}}
        except Exception as error:  # a refusal here must reach the agent, not kill the pump
            body = {"error": {"code": -32000, "message": f"Browsr could not open a background tab: {error}"}}
        try:
            client.send(OP_TEXT, _reply(message, body))
        except OSError:
            pass


def _fetch(real, path):
    with urllib.request.urlopen(f"http://{real[0]}:{real[1]}{path}", timeout=10) as response:
        return response.read()


def _find_target(server, url, timeout):
    """Poll the browser's own target list until the tab we just made appears."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            _, listing = server.endpoint_call(lambda real: json.loads(_fetch(real, "/json/list")))
            for target in listing:
                if target.get("url") == url and target.get("type") == "page":
                    return target["id"]
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("the new tab never appeared")
        time.sleep(0.05)


def open_tab_with_bridge(url):
    """Default tab opener: ask the Browsr extension through bridge.py.

    The proxy runs in the agent's Herdr pane, so the workspace the tab belongs
    to is read from this process's environment by the bridge.
    """
    result = subprocess.run(
        [str(BRIDGE), "open-tab", url], capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "bridge open-tab failed")


def _rewrite(value, real_netloc, proxy_netloc):
    if isinstance(value, list):
        return [_rewrite(item, real_netloc, proxy_netloc) for item in value]
    if isinstance(value, dict):
        return {
            key: item.replace(real_netloc, proxy_netloc)
            if key in ("webSocketDebuggerUrl", "devtoolsFrontendUrl") and isinstance(item, str)
            else _rewrite(item, real_netloc, proxy_netloc)
            for key, item in value.items()
        }
    return value


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def endpoint_call(self, action):
        """Run action(real) against the current endpoint; returns (real, result).

        One retry after dropping the cached address, because a failed connection
        is how a restarted browser on a new address shows itself.
        """
        for attempt in (0, 1):
            real = self.endpoint.get()
            try:
                return real, action(real)
            except OSError:
                self.endpoint.invalidate()
                if attempt:
                    raise


def start_proxy(real_endpoint, open_tab=open_tab_with_bridge):
    """Start the proxy; returns (server, proxy_url).

    real_endpoint is either http://127.0.0.1:PORT or a callable returning one
    (raising when the browser is down), asked afresh as requests come in.

    The threads are daemons: the proxy lives and dies with the process that
    started it.
    """
    resolve = real_endpoint if callable(real_endpoint) else (lambda: real_endpoint)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.endpoint = Endpoint(resolve)
    server.open_tab = open_tab
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
