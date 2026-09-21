import contextlib
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


@contextlib.contextmanager
def sandboxed_state():
    """Keep a test off the real profile and state directory.

    Every path the bridge writes to is derived from the home directory at
    import time, so a test that launches has to move all of them. Without this
    the suite creates files under the user's own Browsr state — and passes on
    the author's machine while failing on a clean one, which is how a broken
    test reached the default branch.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state").mkdir()
        (root / "profile").mkdir()
        (root / "extension").mkdir()
        with patch.object(bridge, "STATE_DIR", root / "state"), \
                patch.object(bridge, "DATA_DIR", root / "data"), \
                patch.object(bridge, "SOCKET_PATH", root / "state" / "control.sock"), \
                patch.object(bridge, "PROFILE_DIR", root / "profile"), \
                patch.object(bridge, "LAUNCH_LOCK", root / "state" / "launch.lock"), \
                patch.object(bridge, "EXTENSION_DIR", root / "extension"), \
                patch.object(bridge, "EXTENSION_STAMP", root / "state" / "extension.stamp"), \
                patch.object(bridge, "PANEL_PANES", root / "state" / "panel-panes.json"):
            yield root


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

        with sandboxed_state(), \
                patch.object(bridge, "setup", return_value="/usr/bin/chromium"), \
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

    def test_switching_workspace_carries_the_whole_list(self):
        """The strip has to be able to come back from a dropped event.

        Workspace events are fire-and-forget: one that happens while the bridge
        is down is never resent, and the group it should have added or removed
        stays wrong for as long as the browser runs. Sending the full list
        alongside the switch — the one event that happens all day and cannot
        race with the workspace it names — makes that self-correcting.
        """
        sent = []
        event = {"event": "workspace_focused"}
        context = {"workspace_id": "w7", "workspace_label": "My app"}

        with patch.dict(os.environ, {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context),
            "HERDR_SOCKET_PATH": "/run/herdr.sock",
        }, clear=True), \
                patch.object(bridge, "bridge_alive", return_value=True), \
                patch.object(bridge, "herdr_workspaces", return_value=[
                    {"workspace_id": "w7", "label": "My app"},
                    {"workspace_id": "w8", "label": "Other"},
                ]), \
                patch.object(bridge, "socket_request", lambda message, **kw: sent.append(message)):
            bridge.hook()

        self.assertEqual([message["type"] for message in sent], ["workspace_set", "workspace"])
        self.assertEqual(
            [item["workspace_id"] for item in sent[0]["workspaces"]], ["w7", "w8"]
        )
        self.assertEqual(sent[0]["session_id"], "/run/herdr.sock")
        self.assertEqual(sent[1]["event"], "focused")

    def test_a_workspace_switch_survives_herdr_not_answering(self):
        sent = []
        event = {"event": "workspace_focused"}
        context = {"workspace_id": "w7", "workspace_label": "My app"}

        with patch.dict(os.environ, {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context),
            "HERDR_SOCKET_PATH": "/run/herdr.sock",
        }, clear=True), \
                patch.object(bridge, "bridge_alive", return_value=True), \
                patch.object(bridge, "herdr_workspaces", return_value=None), \
                patch.object(bridge, "socket_request", lambda message, **kw: sent.append(message)):
            bridge.hook()

        self.assertEqual([message["type"] for message in sent], ["workspace"])

    def test_the_extension_announcing_itself_reconciles_the_strip(self):
        """A service worker that wakes on its own is nobody's launch.

        Chromium stops an idle extension and starts it again on the next event,
        and nothing in the plugin runs when it does. Until the extension itself
        is the trigger, a browser left running for days keeps whatever strip it
        drifted into.
        """
        spec = importlib.util.spec_from_file_location(
            "native_host", os.path.join(os.path.dirname(__file__), "..", "native_host.py")
        )
        native_host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native_host)
        written = []

        # native_host imports bridge by name, which is a different module object
        # from the one this suite loads by path; patch the one it actually holds.
        with patch.object(native_host, "write_native_message", written.append), \
                patch.object(native_host.bridge, "herdr_workspaces", return_value=[
                    {"workspace_id": "w7", "label": "My app"},
                ]), \
                patch.object(native_host.bridge, "herdr_session_id",
                             return_value="/run/herdr.sock"):
            native_host.reconcile_workspaces()

        self.assertEqual([message["type"] for message in written], ["workspace_set"])
        self.assertEqual(written[0]["workspaces"], [{"workspace_id": "w7", "label": "My app"}])

    def test_status_reports_what_the_panel_has_to_show(self):
        with patch.object(bridge, "browser_alive", return_value=True), \
                patch.object(bridge, "bridge_alive", return_value=True), \
                patch.object(bridge, "extension_is_stale", return_value=False), \
                patch.object(bridge, "herdr_workspaces", return_value=[
                    {"workspace_id": "w7", "label": "My app"},
                    {"workspace_id": "w8"},
                ]), \
                patch.object(bridge, "socket_request", lambda message, **kw: {
                    "ok": True,
                    "result": {"windows": 1, "ungrouped": 1, "groups": [
                        {"title": "My app", "tabs": 2, "adopted": False},
                    ]},
                }):
            status = bridge.browser_status()

        self.assertTrue(status["browser"])
        self.assertTrue(status["bridge"])
        self.assertFalse(status["extension_stale"])
        # A workspace with no label still has to be nameable on screen.
        self.assertEqual(status["workspaces"], ["My app", "w8"])
        self.assertEqual(status["strip"]["windows"], 1)

    def test_status_still_answers_when_nothing_is_running(self):
        """The panel exists for exactly this moment; it must not crash into it."""
        def refuse(message, **kwargs):
            raise RuntimeError("The Chromium bridge did not answer")

        with patch.object(bridge, "browser_alive", return_value=False), \
                patch.object(bridge, "bridge_alive", return_value=False), \
                patch.object(bridge, "extension_is_stale", return_value=True), \
                patch.object(bridge, "herdr_workspaces", return_value=None), \
                patch.object(bridge, "socket_request", refuse):
            status = bridge.browser_status()

        self.assertFalse(status["browser"])
        self.assertIsNone(status["strip"])
        self.assertEqual(status["workspaces"], [])

    def test_only_the_browsr_process_is_ever_signalled(self):
        """Browsr shares its binary with the browser the user lives in.

        The profile directory is the only thing that tells them apart, and the
        helper processes carry it too, so matching on it alone would signal the
        renderers as well as the browser.

        Both argv shapes appear here on purpose. Chromium rewrites its own
        command line into one space-separated blob with no NUL separators, and a
        version of this that only understood the normal shape passed against a
        made-up /proc while finding nothing at all on the running browser.
        """
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            separated = {
                "12": ["chromium", "--type=renderer", "--user-data-dir=/prof/chromium"],
                "13": ["chromium", "--user-data-dir=/somewhere/else"],
                "14": ["python3", "unrelated.py"],
                "15": ["chromium", "--user-data-dir=/prof/chromium-other"],
            }
            for name, argv in separated.items():
                (proc / name).mkdir()
                (proc / name / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            # The real browser, exactly as Chromium leaves it.
            (proc / "11").mkdir()
            (proc / "11" / "cmdline").write_bytes(
                b"/usr/lib/chromium/chromium --user-data-dir=/prof/chromium --new-window\0"
            )
            (proc / "self").mkdir()
            (proc / "self" / "cmdline").write_bytes(b"not a pid\0")

            with patch.object(bridge, "PROFILE_DIR", Path("/prof/chromium")):
                self.assertEqual(bridge.browser_pids(proc), [11])

    def test_the_key_opens_focuses_then_closes(self):
        """The convention every other Herdr sidebar plugin follows.

        Pressing the key while working in another pane should bring the panel
        under the cursor, not destroy it; only pressing it while already in the
        panel closes it.
        """
        ran = []
        call = lambda *argv: ran.append(argv) or ""

        with sandboxed_state(), \
                patch.object(bridge, "herdr_call", call), \
                patch.object(bridge, "current_tab_id", return_value="w7:t1"):
            # Nothing open yet.
            with patch.object(bridge, "focused_pane_id", return_value="w7:p1"):
                bridge.toggle_panel()
            self.assertIn("open", ran[0])

            bridge.record_panel_pane("w7:t1", "w7:p3")
            ran.clear()

            # Open, but the cursor is elsewhere.
            with patch.object(bridge, "live_pane_ids", return_value={"w7:p1", "w7:p3"}), \
                    patch.object(bridge, "focused_pane_id", return_value="w7:p1"):
                bridge.toggle_panel()
            self.assertEqual(ran, [("plugin", "pane", "focus", "w7:p3")])
            ran.clear()

            # Open, and the cursor is already in it.
            with patch.object(bridge, "live_pane_ids", return_value={"w7:p1", "w7:p3"}), \
                    patch.object(bridge, "focused_pane_id", return_value="w7:p3"):
                bridge.toggle_panel()
            self.assertEqual(ran, [("plugin", "pane", "close", "w7:p3")])

    def test_a_panel_that_died_without_tidying_up_reopens(self):
        """A crash leaves the recorded pane id behind; the key must still work."""
        ran = []

        with sandboxed_state(), \
                patch.object(bridge, "herdr_call", lambda *argv: ran.append(argv) or ""), \
                patch.object(bridge, "current_tab_id", return_value="w7:t1"), \
                patch.object(bridge, "focused_pane_id", return_value="w7:p1"), \
                patch.object(bridge, "live_pane_ids", return_value={"w7:p1"}):
            bridge.record_panel_pane("w7:t1", "w7:p3")
            bridge.toggle_panel()

        self.assertIn("open", ran[0])

    def test_the_panel_in_another_tab_is_left_alone(self):
        ran = []

        with sandboxed_state(), \
                patch.object(bridge, "herdr_call", lambda *argv: ran.append(argv) or ""), \
                patch.object(bridge, "live_pane_ids", return_value={"w7:p3", "w8:p2"}), \
                patch.object(bridge, "focused_pane_id", return_value="w8:p2"):
            bridge.record_panel_pane("w7:t1", "w7:p3")
            bridge.record_panel_pane("w8:t1", "w8:p2")
            with patch.object(bridge, "current_tab_id", return_value="w8:t1"):
                bridge.toggle_panel()

        self.assertEqual(ran, [("plugin", "pane", "close", "w8:p2")])

    def test_the_browser_follows_the_focused_workspace_however_it_was_switched(self):
        """Herdr does not call a plugin when the user switches with the mouse.

        Verified against this machine's Herdr 0.9.1: a switch made through the
        API is dispatched to plugins within the same second, while six switches
        made in the UI left no plugin invocation at all, though the server
        recorded every one of them. Waiting to be told is therefore not a
        mechanism; the host asks instead, and speaks only when the answer
        changes.
        """
        spec = importlib.util.spec_from_file_location(
            "native_host", os.path.join(os.path.dirname(__file__), "..", "native_host.py")
        )
        native_host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native_host)
        written = []

        class Link:
            answer = {"workspace_id": "w7", "label": "app"}

            def focused_workspace(self):
                return self.answer

        link = Link()

        with patch.object(native_host, "write_native_message", written.append), \
                patch.object(native_host.bridge, "herdr_session_id", return_value="/run/h.sock"):
            last = native_host.focus_step(None, link)
            self.assertEqual(last, "w7")
            # Nothing changed, so nothing is said.
            last = native_host.focus_step(last, link)
            self.assertEqual(len(written), 1)

            link.answer = {"workspace_id": "w8", "label": "docs"}
            last = native_host.focus_step(last, link)
            self.assertEqual(last, "w8")

            # Herdr not answering must not be read as a switch.
            link.answer = None
            last = native_host.focus_step(last, link)
            self.assertEqual(last, "w8")

        self.assertEqual([m["event"] for m in written], ["focused", "focused"])
        self.assertEqual([m["workspace_id"] for m in written], ["w7", "w8"])
        self.assertEqual(written[1]["label"], "docs")

    def test_an_exiting_bridge_does_not_delete_the_live_one(self):
        """Two hosts overlap whenever the extension reconnects.

        The newer one rebinds the same path; the older one then exits and, if it
        tidies up blindly, deletes the socket the live host is listening on. The
        bridge then looks dead to every caller, which is how an extra browser
        window got opened in the first place.
        """
        spec = importlib.util.spec_from_file_location(
            "native_host", os.path.join(os.path.dirname(__file__), "..", "native_host.py")
        )
        native_host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native_host)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            path.write_text("older host")
            mine = path.stat().st_ino

            # The newer host replaces the file at the same path.
            path.unlink()
            path.write_text("newer host")

            with patch.object(native_host, "SOCKET_PATH", path):
                native_host.remove_socket_if_ours(mine)
                self.assertTrue(path.exists(), "the live host's socket was deleted")

                native_host.remove_socket_if_ours(path.stat().st_ino)
                self.assertFalse(path.exists(), "a host failed to clean up after itself")

    def test_the_sandbox_covers_every_path_the_bridge_writes_to(self):
        """A path added later must not quietly escape into the user's own state.

        This has already happened twice: the suite wrote the Chromium log and
        then the panel's pane record into the real Browsr directory, passing on
        the author's machine and failing on a clean one. Rather than trust the
        next person to remember, ask the module what paths it has.
        """
        owned = {
            name
            for name, value in vars(bridge).items()
            if isinstance(value, Path)
            and any(str(value).startswith(str(root))
                    for root in (bridge.STATE_DIR, bridge.DATA_DIR))
        }
        with sandboxed_state() as root:
            escaped = sorted(
                name for name in owned
                if not str(getattr(bridge, name)).startswith(str(root))
            )
        self.assertEqual(escaped, [], f"not redirected by sandboxed_state(): {escaped}")

    def test_a_running_browser_is_never_launched_a_second_time(self):
        """Chromium refuses to start twice; the second start opens a window.

        Every extra window is what multiplied the tab strip: the extension then
        has to guess which window is the real one, and the groups sitting in the
        other one look missing and get remade. So when Chromium answers but the
        bridge does not, the only correct move is to wait for the extension.
        """
        launched = []

        with sandboxed_state(), \
                patch.object(bridge, "setup", return_value="/usr/bin/chromium"), \
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
        with sandboxed_state() as root:
            lock = root / "state" / "launch.lock"
            probe = (
                "import fcntl,sys\n"
                f"handle = open({str(lock)!r}, 'w')\n"
                "try:\n"
                "    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except OSError:\n"
                "    sys.exit(3)\n"
                "sys.exit(0)\n"
            )
            with bridge.launch_lock():
                held = subprocess.run([sys.executable, "-c", probe])
            free = subprocess.run([sys.executable, "-c", probe])

        self.assertEqual(held.returncode, 3, "a second process got in while one was launching")
        self.assertEqual(free.returncode, 0, "the lock was not released")


if __name__ == "__main__":
    unittest.main()
