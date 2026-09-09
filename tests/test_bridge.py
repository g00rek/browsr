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
        with patch.dict(os.environ, {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(context),
        }):
            self.assertEqual(bridge.event_command(), {
                "type": "workspace",
                "event": "focused",
                "session_id": "default",
                "workspace_id": "w7",
                "label": "My app",
            })


if __name__ == "__main__":
    unittest.main()
