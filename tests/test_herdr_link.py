import importlib.util
import json
import os
import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SPEC = importlib.util.spec_from_file_location(
    "bridge", os.path.join(os.path.dirname(__file__), "..", "bridge.py")
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeHerdr:
    """A real Unix socket speaking Herdr's newline-JSON, not a stand-in.

    The link is the seam that matters — reconnecting, framing, matching a reply
    to its request — and none of that is exercised by a mock that hands back a
    dictionary.
    """

    def __init__(self, path, drop_after=None):
        self.path = str(path)
        # One drop, then healthy again — a restart, not a permanent outage.
        self.drop_after = drop_after
        self.dropped = False
        self.served = 0
        self.connections = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        self.server.settimeout(0.5)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection):
        buffer = b""
        with connection:
            while not self.stop.is_set():
                try:
                    chunk = connection.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line)
                    self.served += 1
                    if (self.drop_after is not None and not self.dropped
                            and self.served > self.drop_after):
                        self.dropped = True
                        return  # Herdr restarted under us.
                    reply = {
                        "id": request["id"],
                        "result": {"workspaces": [{"workspace_id": "w1", "focused": True}]},
                    }
                    connection.sendall(json.dumps(reply).encode() + b"\n")

    def shutdown(self):
        self.stop.set()
        self.server.close()


class HerdrLinkTests(unittest.TestCase):
    def test_one_connection_serves_many_requests(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "herdr.sock"
            herdr = FakeHerdr(path)
            try:
                link = bridge.HerdrLink(str(path))
                for _ in range(5):
                    answer = link.request("workspace.list")
                    self.assertEqual(answer["workspaces"][0]["workspace_id"], "w1")
                link.close()
            finally:
                herdr.shutdown()

        self.assertEqual(herdr.served, 5)
        self.assertEqual(herdr.connections, 1, "the link opened a connection per request")

    def test_it_reconnects_when_herdr_goes_away(self):
        """Herdr restarts; the browser must not stop following after it."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "herdr.sock"
            herdr = FakeHerdr(path, drop_after=2)
            try:
                link = bridge.HerdrLink(str(path))
                self.assertIsNotNone(link.request("workspace.list"))
                self.assertIsNotNone(link.request("workspace.list"))
                # The third is dropped mid-flight; the fourth has to work anyway.
                link.request("workspace.list")
                self.assertIsNotNone(link.request("workspace.list"))
                link.close()
            finally:
                herdr.shutdown()

        self.assertGreater(herdr.connections, 1, "the link never reconnected")

    def test_a_dead_socket_is_reported_not_raised(self):
        with TemporaryDirectory() as directory:
            link = bridge.HerdrLink(str(Path(directory) / "nothing.sock"))
            self.assertIsNone(link.request("workspace.list"))


if __name__ == "__main__":
    unittest.main()
