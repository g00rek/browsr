import base64
import hashlib
import json
import os
import socket
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cdp_filter  # noqa: E402


def decode(raw):
    return json.loads(raw)


class FilterMessageTests(unittest.TestCase):
    def test_bring_to_front_is_answered_not_forwarded(self):
        action, out = cdp_filter.filter_message(b'{"id":7,"method":"Page.bringToFront","sessionId":"S1"}')
        self.assertEqual(action, "reply")
        self.assertEqual(decode(out), {"id": 7, "sessionId": "S1", "result": {}})

    def test_activate_target_is_answered_without_session_id(self):
        action, out = cdp_filter.filter_message(b'{"id":8,"method":"Target.activateTarget","params":{"targetId":"T"}}')
        self.assertEqual(action, "reply")
        self.assertEqual(decode(out), {"id": 8, "result": {}})

    def test_create_target_is_handed_to_the_extension_not_forwarded(self):
        raw = b'{"id":1,"method":"Target.createTarget","params":{"url":"about:blank","newWindow":true,"background":false}}'
        action, out = cdp_filter.filter_message(raw)
        self.assertEqual(action, "open_tab")
        self.assertEqual(out["id"], 1)

    def test_create_target_without_params_is_also_handed_over(self):
        action, _ = cdp_filter.filter_message(b'{"id":1,"method":"Target.createTarget"}')
        self.assertEqual(action, "open_tab")

    def test_create_target_for_another_address_is_refused(self):
        action, out = cdp_filter.filter_message(b'{"id":1,"method":"Target.createTarget","params":{"url":"file:///etc/passwd"}}')
        self.assertEqual(action, "reply")
        self.assertEqual(decode(out)["error"]["code"], -32000)

    def test_create_target_in_a_context_is_refused(self):
        action, out = cdp_filter.filter_message(
            b'{"id":2,"method":"Target.createTarget","params":{"url":"x","browserContextId":"C"}}'
        )
        self.assertEqual(action, "reply")
        reply = decode(out)
        self.assertEqual(reply["id"], 2)
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn("separate browser contexts are disabled", reply["error"]["message"])

    def test_create_browser_context_is_refused(self):
        action, out = cdp_filter.filter_message(b'{"id":3,"method":"Target.createBrowserContext","params":{}}')
        self.assertEqual(action, "reply")
        self.assertEqual(decode(out)["error"]["code"], -32000)
        self.assertEqual(decode(out)["id"], 3)

    def test_everything_else_returns_the_original_bytes(self):
        for raw in (
            b'{"id": 4,  "method":"Target.getTargets"}',
            b'{"id":5,"method":"Page.navigate","params":{"url":"http://x"},"sessionId":"S"}',
            b"not json at all",
            b"[1,2]",
        ):
            action, out = cdp_filter.filter_message(raw)
            self.assertEqual(action, "forward")
            self.assertIs(out, raw)


class FakeChromium:
    """A tiny HTTP + WebSocket server that records what reaches it."""

    def __init__(self):
        self.received = []
        self.tabs = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                port = self.server.server_port
                if self.path == "/json/list":
                    body = json.dumps(fake.tabs).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/json/version":
                    body = json.dumps({
                        "Browser": "Fake/1",
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1(key.encode() + cdp_filter.WS_GUID).digest()).decode()
                self.wfile.write((
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode())
                self.wfile.flush()
                self.close_connection = True
                peer = cdp_filter.Peer(self.connection, masked=False)
                try:
                    for opcode, payload in peer.messages():
                        fake.received.append(payload)
                        message = json.loads(payload)
                        peer.send(opcode, json.dumps({"id": message["id"], "result": {"echo": message.get("method"), "size": len(payload)}}).encode()
                                  if message.get("method") != "Test.big" else payload)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def connect(url):
    """Minimal WebSocket client; returns a masking Peer."""
    host, port, path = url.split("//")[1].split(":")[0], int(url.split(":")[2].split("/")[0]), "/" + url.split("/", 3)[3]
    sock = socket.create_connection((host, port), timeout=10)
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    assert b" 101 " in response.split(b"\r\n")[0], response
    return cdp_filter.Peer(sock, masked=True)


class ProxyEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.chromium = FakeChromium()
        self.opened = []

        def open_tab(url):
            # What the extension does: a tab appears in the browser, in the background.
            self.opened.append(url)
            self.chromium.tabs.append({"id": "TAB1", "type": "page", "url": url})

        self.server, self.proxy = cdp_filter.start_proxy(self.chromium.url, open_tab=open_tab)
        self.addCleanup(self.chromium.close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def ask(self, peer, message):
        peer.send(cdp_filter.OP_TEXT, json.dumps(message).encode())
        _, payload = next(peer.messages())
        return json.loads(payload)

    def test_version_page_points_at_the_proxy(self):
        with urllib.request.urlopen(self.proxy + "/json/version") as response:
            info = json.loads(response.read())
        proxy_port = self.server.server_port
        self.assertEqual(info["webSocketDebuggerUrl"], f"ws://127.0.0.1:{proxy_port}/devtools/browser/abc")
        self.assertEqual(info["Browser"], "Fake/1")

    def test_a_failed_tab_open_is_an_error_not_a_cdp_fallback(self):
        def broken(url):
            raise RuntimeError("extension is not answering")

        self.server.open_tab = broken
        with urllib.request.urlopen(self.proxy + "/json/version") as response:
            url = json.loads(response.read())["webSocketDebuggerUrl"]
        peer = connect(url)
        self.addCleanup(peer.close)
        reply = self.ask(peer, {"id": 5, "method": "Target.createTarget", "params": {"url": "about:blank"}})
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn("extension is not answering", reply["error"]["message"])
        self.assertEqual(self.chromium.received, [])

    def test_commands_through_the_real_seam(self):
        with urllib.request.urlopen(self.proxy + "/json/version") as response:
            url = json.loads(response.read())["webSocketDebuggerUrl"]
        peer = connect(url)
        self.addCleanup(peer.close)

        self.assertEqual(self.ask(peer, {"id": 1, "method": "Page.bringToFront", "sessionId": "S"}),
                         {"id": 1, "sessionId": "S", "result": {}})
        self.assertEqual(self.chromium.received, [])

        count = len(self.chromium.received)
        reply = self.ask(peer, {"id": 2, "method": "Target.createTarget",
                                "params": {"url": "about:blank", "newWindow": True}})
        self.assertEqual(reply, {"id": 2, "result": {"targetId": "TAB1"}})
        self.assertEqual(len(self.opened), 1)
        self.assertRegex(self.opened[0], r"^about:blank#browsr-[0-9a-f]+$")
        # Chromium itself never saw a createTarget: that is what raised the window.
        self.assertEqual(len(self.chromium.received), count)

        reply = self.ask(peer, {"id": 3, "method": "Target.createBrowserContext"})
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertEqual(len(self.chromium.received), count)

        big = json.dumps({"id": 4, "method": "Test.big", "params": {"blob": "x" * (5 * 1024 * 1024)}}).encode()
        peer.send(cdp_filter.OP_TEXT, big)
        _, echoed = next(peer.messages())
        self.assertEqual(echoed, big)
        self.assertEqual(self.chromium.received[-1], big)


if __name__ == "__main__":
    unittest.main()
