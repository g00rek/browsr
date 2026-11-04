import base64
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
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


class BrowserStartsLateTests(unittest.TestCase):
    """The proxy outlives the browser: down means 503, up again means working."""

    def setUp(self):
        self.up = None

        def resolve():
            if not self.up:
                raise cdp_filter.BrowserDown("Browsr is not running. Start it from Herdr.")
            return self.up.url

        self.opened = []
        self.server, self.proxy = cdp_filter.start_proxy(resolve, open_tab=self.opened.append)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.proxy + path) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def test_down_gives_503_with_the_reason_then_recovers(self):
        status, body = self.get("/json/version")
        self.assertEqual(status, 503)
        self.assertIn(b"Browsr is not running", body)

        port = self.server.server_port
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(b"GET /devtools/browser/abc HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==\r\n"
                     b"Sec-WebSocket-Version: 13\r\n\r\n")
        self.assertTrue(sock.recv(4096).startswith(b"HTTP/1.1 503"))
        sock.close()

        self.up = FakeChromium()
        self.addCleanup(self.up.close)
        status, body = self.get("/json/version")
        self.assertEqual(status, 200)
        peer = connect(json.loads(body)["webSocketDebuggerUrl"])
        self.addCleanup(peer.close)
        peer.send(cdp_filter.OP_TEXT, b'{"id":1,"method":"Browser.getVersion"}')
        self.assertEqual(json.loads(next(peer.messages())[1])["id"], 1)

    def test_a_browser_that_moves_is_found_again(self):
        first = FakeChromium()
        self.up = first
        self.assertEqual(self.get("/json/version")[0], 200)
        first.close()
        self.up = FakeChromium()
        self.addCleanup(self.up.close)
        # The cached address is dead; the failed connection makes it re-resolve.
        self.assertEqual(self.get("/json/version")[0], 200)

    def test_opening_a_tab_while_down_is_a_clear_error(self):
        def refuse(url):
            raise RuntimeError("Browsr is not running. Start it from Herdr.")

        self.server.open_tab = refuse
        # Through the real seam: bridge.py open-tab with Browsr down must fail
        # loudly rather than hang; here we check the proxy turns it into an error.
        events = []
        message = {"id": 9, "method": "Target.createTarget", "params": {"url": "about:blank"}}

        class Sink:
            def send(self, opcode, payload):
                events.append(json.loads(payload))

        handler = cdp_filter._Handler.__new__(cdp_filter._Handler)
        handler.server = self.server
        handler._create_tab(Sink(), message)
        self.assertEqual(events[0]["id"], 9)
        self.assertIn("Browsr is not running", events[0]["error"]["message"])


class WrapperTests(unittest.TestCase):
    def test_wrapper_starts_its_child_even_though_the_browser_is_down(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with tempfile.TemporaryDirectory() as home:
            npx = os.path.join(home, "npx")
            record = os.path.join(home, "args")
            with open(npx, "w") as handle:
                handle.write(f'#!/bin/sh\necho "$@" > {record}\nexec sleep 30\n')
            os.chmod(npx, os.stat(npx).st_mode | stat.S_IEXEC)
            env = {"PATH": home + ":" + os.environ["PATH"], "HOME": home, "HERDR_WORKSPACE_ID": "w1"}
            wrapper = subprocess.Popen([sys.executable, os.path.join(root, "browsr-chrome-mcp.py")], env=env)
            try:
                for _ in range(100):
                    if os.path.exists(record) and os.path.getsize(record):
                        break
                    time.sleep(0.05)
                self.assertIsNone(wrapper.poll(), "the wrapper exited instead of waiting for the browser")
                self.assertIn("--browser-url=http://127.0.0.1:", open(record).read())
            finally:
                wrapper.send_signal(signal.SIGTERM)
                wrapper.wait(timeout=10)


    def test_the_wrapper_starts_without_a_herdr_pane_and_says_so(self):
        """A pane variable that is briefly unreadable cost the whole session
        its browser tooling: the wrapper exited, and Claude Code never starts
        a stdio server again until someone reconnects it by hand. Opening a
        tab is the only thing that needs the workspace, and it says so
        itself."""
        root = os.path.join(os.path.dirname(__file__), "..")
        with tempfile.TemporaryDirectory() as home:
            npx = os.path.join(home, "npx")
            record = os.path.join(home, "args")
            with open(npx, "w") as handle:
                handle.write(f'#!/bin/sh\necho "$@" > {record}\nexec sleep 30\n')
            os.chmod(npx, os.stat(npx).st_mode | stat.S_IEXEC)
            state = os.path.join(home, ".local", "state", "browsr")
            # No HERDR_WORKSPACE_ID, no HC_TERMINAL_ID: nothing says which
            # project this is, and `herdr-app` is not on this PATH either.
            env = {"PATH": home + ":" + os.environ["PATH"], "HOME": home}
            wrapper = subprocess.Popen([sys.executable, os.path.join(root, "browsr-chrome-mcp.py")], env=env)
            try:
                for _ in range(100):
                    if os.path.exists(record) and os.path.getsize(record):
                        break
                    time.sleep(0.05)
                self.assertIsNone(wrapper.poll(), "the wrapper exited with no workspace to act on")
                args = open(record).read()
                self.assertIn("--browser-url=http://127.0.0.1:", args)
                # Pinned, not @latest: npm was asked on every start, and a
                # start that waits on the network races a 30 second timeout.
                self.assertNotIn("@latest", args)
                self.assertIn("chrome-devtools-mcp@", args)
                log = os.path.join(state, "mcp.log")
                for _ in range(60):
                    if os.path.exists(log):
                        break
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(log), "the wrapper left no record of its start")
                written = open(log).read()
                self.assertIn("workspace:", written)
                self.assertIn("child ", written)
            finally:
                wrapper.send_signal(signal.SIGTERM)
                wrapper.wait(timeout=10)

    def test_the_pinned_version_is_one_npm_actually_has(self):
        """A pinned version is only better than `@latest` while it exists.
        This is the one place the real registry is asked, because every other
        test here stands in for npx with a shell script."""
        import importlib.util
        root = os.path.join(os.path.dirname(__file__), "..")
        spec = importlib.util.spec_from_file_location(
            "browsr_mcp", os.path.join(root, "browsr-chrome-mcp.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            answer = subprocess.run(
                ["npm", "view", f"chrome-devtools-mcp@{module.MCP_VERSION}", "version"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise unittest.SkipTest(f"npm could not be asked: {error}")
        if answer.returncode != 0:
            raise unittest.SkipTest(f"npm could not be asked: {answer.stderr.strip()[:120]}")
        self.assertEqual(answer.stdout.strip(), module.MCP_VERSION)


if __name__ == "__main__":
    unittest.main()
