import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("bridge", os.path.join(os.path.dirname(__file__), "..", "bridge.py"))
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def test_local_url_filter(self):
        self.assertTrue(bridge.is_local_url("http://localhost:3000/path"))
        self.assertTrue(bridge.is_local_url("https://127.0.0.1:8787"))
        self.assertTrue(bridge.is_local_url("http://[::1]:5173"))
        self.assertFalse(bridge.is_local_url("https://example.com"))
        self.assertFalse(bridge.is_local_url("javascript:alert(1)"))

    def test_focus_event(self):
        event = {"event": "workspace_focused", "data": {"workspace_id": "w7"}}
        context = {"workspace_id": "w7", "workspace_label": "My app"}
        # `clear=True`: run from inside a Herdr pane, HERDR_SOCKET_PATH is set in
        # the real environment and event_command reads it, so the session id
        # asserted below depended on where the suite was run from.
        with patch.dict(os.environ, {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context),
        }, clear=True):
            with patch.object(bridge, "herdr_session_id", return_value="/run/herdr.sock"):
                self.assertEqual(bridge.event_command(), {
                    "type": "workspace",
                    "event": "focused",
                    "session_id": "/run/herdr.sock",
                    "workspace_id": "w7",
                    "label": "My app",
                })


    def test_devtools_port_is_fixed(self):
        """A restart must not move the DevTools port.

        The port is resolved once by the automation wrapper and handed to a
        client as a URL, so a random port per launch silently breaks every agent
        session that was already running.
        """
        launched = {}

        def fake_popen(argv, **kwargs):
            launched["argv"] = argv
            return None

        with patch.object(bridge, "setup", return_value="/usr/bin/chromium"), \
                patch.object(bridge, "bridge_alive", side_effect=[False, False, True]), \
                patch.object(bridge, "browser_alive", return_value=False), \
                patch.object(bridge.subprocess, "Popen", fake_popen), \
                patch.object(bridge, "show_window", lambda: None), \
                patch.object(bridge, "sync_workspaces", lambda: None):
            bridge.ensure_browser(wait=True)

        self.assertIn(f"--remote-debugging-port={bridge.DEVTOOLS_PORT}", launched["argv"])
        self.assertNotIn("--remote-debugging-port=0", launched["argv"])
        # Not the port the user's own daily Chrome answers on for the same
        # protocol: the two must never be mistaken for one another.
        self.assertNotEqual(bridge.DEVTOOLS_PORT, 9222)

    def test_a_running_browser_is_never_launched_a_second_time(self):
        """Chromium refuses to start twice; the second start opens a window.

        Every extra window is what multiplied the tab strip: the extension then
        has to guess which window is the real one, and the groups sitting in the
        other one look missing and get remade. So when Chromium answers but the
        bridge does not, the only correct move is to wait for the extension.
        """
        launched = []

        with patch.object(bridge, "setup", return_value="/usr/bin/chromium"), \
                patch.object(bridge, "bridge_alive", return_value=False), \
                patch.object(bridge, "browser_alive", return_value=True), \
                patch.object(bridge, "BRIDGE_WAIT_SECONDS", 0.05), \
                patch.object(bridge.subprocess, "Popen", lambda *a, **k: launched.append(a)), \
                patch.object(bridge, "sync_workspaces", lambda: None):
            with self.assertRaises(RuntimeError):
                bridge.ensure_browser(wait=True)

        self.assertEqual(launched, [])

    def test_devtools_endpoint_reports_the_port_that_answers(self):
        """The stale port file must never reach the automation wrapper.

        Verified against the installed Chromium 152: `DevToolsActivePort` is
        written only when the port is left to Chromium (`--remote-debugging-port=0`).
        With a fixed port the file is never refreshed, so whatever it holds is a
        leftover from an older run, and handing that number to the client fails
        as "could not connect" long after the port died.
        """
        printed = []

        with patch.object(bridge, "ensure_browser", lambda **kwargs: None), \
                patch.object(bridge, "devtools_alive", lambda port: port == bridge.DEVTOOLS_PORT), \
                patch("builtins.print", lambda *args, **kwargs: printed.append(args[0])):
            bridge.devtools_endpoint()

        self.assertEqual(printed, [f"http://127.0.0.1:{bridge.DEVTOOLS_PORT}"])

    def test_devtools_endpoint_refuses_a_port_nothing_answers_on(self):
        printed = []

        with patch.object(bridge, "ensure_browser", lambda **kwargs: None), \
                patch.object(bridge, "devtools_alive", lambda port: False), \
                patch("builtins.print", lambda *args, **kwargs: printed.append(args[0])):
            with self.assertRaises(RuntimeError):
                bridge.devtools_endpoint()

        self.assertEqual(printed, [])

    def test_setup_survives_several_callers_at_once(self):
        """After a reboot every caller runs setup() at the same moment."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "manifest.json"
            writer = (
                "import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('bridge', {str(Path(bridge.__file__).resolve())!r})\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "from pathlib import Path\n"
                "for _ in range(40):\n"
                f"    module.atomic_json(Path({str(target)!r}), {{'ok': True}})\n"
            )
            writers = [subprocess.Popen([sys.executable, "-c", writer]) for _ in range(4)]
            codes = [writer_process.wait() for writer_process in writers]

            self.assertEqual(codes, [0, 0, 0, 0])
            self.assertEqual(json.loads(target.read_text()), {"ok": True})

    def test_an_edited_extension_is_not_run_from_the_profile_cache(self):
        """Chromium keeps its own compiled copy of the service worker.

        A browser restart runs that copy again, and so does a version bump in
        the manifest, so an edited extension can go on running its old code
        while the repository looks fixed. Verified on the installed Chromium
        152: only dropping the profile's service-worker registration makes it
        read the files again.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension = root / "extension"
            extension.mkdir()
            (extension / "service-worker.js").write_text("first")
            cache = root / "profile" / "Default" / "Service Worker"
            cache.mkdir(parents=True)

            with patch.object(bridge, "EXTENSION_DIR", extension), \
                    patch.object(bridge, "PROFILE_DIR", root / "profile"), \
                    patch.object(bridge, "EXTENSION_STAMP", root / "extension.stamp"):
                self.assertTrue(bridge.refresh_extension_if_changed())
                self.assertFalse(cache.exists())

                cache.mkdir(parents=True)
                self.assertFalse(bridge.refresh_extension_if_changed())
                self.assertTrue(cache.exists(), "an untouched extension was reloaded for nothing")

                (extension / "service-worker.js").write_text("second and longer")
                self.assertTrue(bridge.refresh_extension_if_changed())
                self.assertFalse(cache.exists())

    def test_only_one_process_at_a_time_may_start_the_browser(self):
        """Several callers wake at once after a reboot; only one may launch."""
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "launch.lock"
            probe = (
                "import fcntl,sys\n"
                f"handle = open({str(lock)!r}, 'w')\n"
                "try:\n"
                "    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except OSError:\n"
                "    sys.exit(3)\n"
                "sys.exit(0)\n"
            )
            with patch.object(bridge, "LAUNCH_LOCK", lock):
                with bridge.launch_lock():
                    held = subprocess.run([sys.executable, "-c", probe])
                free = subprocess.run([sys.executable, "-c", probe])

        self.assertEqual(held.returncode, 3, "a second process got in while one was launching")
        self.assertEqual(free.returncode, 0, "the lock was not released")


if __name__ == "__main__":
    unittest.main()
