import importlib.util
import json
import os
import unittest
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
            self.assertEqual(bridge.event_command(), {
                "type": "workspace",
                "event": "focused",
                "session_id": "default",
                "workspace_id": "w7",
                "label": "My app",
            })


    def test_devtools_port_is_fixed(self):
        """A restart must not move the DevTools port.

        The port is resolved once by the automation wrapper and handed to a
        client as a URL, so a random port per launch silently breaks every agent
        session that was already running. Chromium keeps writing the port it
        actually opened to `DevToolsActivePort`, which stays the source of truth
        — this only pins what is asked for.
        """
        launched = {}

        def fake_popen(argv, **kwargs):
            launched["argv"] = argv
            return None

        with patch.object(bridge, "setup", return_value="/usr/bin/chromium"), \
                patch.object(bridge, "bridge_alive", side_effect=[False, True]), \
                patch.object(bridge.subprocess, "Popen", fake_popen), \
                patch.object(bridge, "show_window", lambda: None), \
                patch.object(bridge, "sync_workspaces", lambda: None):
            bridge.ensure_browser(wait=True)

        self.assertIn(f"--remote-debugging-port={bridge.DEVTOOLS_PORT}", launched["argv"])
        self.assertNotIn("--remote-debugging-port=0", launched["argv"])
        # Not the port the user's own daily Chrome answers on for the same
        # protocol: the two must never be mistaken for one another.
        self.assertNotEqual(bridge.DEVTOOLS_PORT, 9222)


if __name__ == "__main__":
    unittest.main()
