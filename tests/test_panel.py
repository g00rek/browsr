import contextlib
import importlib.util
import os
import unittest

SPEC = importlib.util.spec_from_file_location(
    "panel", os.path.join(os.path.dirname(__file__), "..", "panel.py")
)
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)

RUNNING = {
    "browser": True,
    "bridge": True,
    "extension_stale": False,
    "workspaces": ["api", "search"],
    "strip": {
        "windows": 1,
        "ungrouped": 1,
        "groups": [
            {"title": "api", "tabs": 2, "adopted": False},
            {"title": "search", "tabs": 1, "adopted": False},
        ],
    },
}


def screen(status, note="", width=80):
    return "\n".join(panel.render(status, note, width))


class PanelTests(unittest.TestCase):
    def test_a_matching_strip_says_so(self):
        text = screen(RUNNING)
        self.assertIn("Strip matches Herdr (2)", text)
        self.assertNotIn("leftover", text)
        self.assertNotIn("no group", text)

    def test_a_leftover_group_is_named_as_leftover(self):
        status = dict(RUNNING, strip=dict(RUNNING["strip"], groups=[
            {"title": "api", "tabs": 2},
            {"title": "search", "tabs": 1},
            {"title": "old-project", "tabs": 1},
        ]))
        text = screen(status)
        self.assertIn("does NOT match", text)
        self.assertRegex(text, r"old-project\s+1 tab\s+leftover")

    def test_a_workspace_with_no_group_is_named_too(self):
        status = dict(RUNNING, workspaces=["api", "search", "browsr"])
        self.assertRegex(screen(status), r"browsr\s+no group")

    def test_two_workspaces_sharing_a_label_are_not_read_as_leftovers(self):
        status = dict(
            RUNNING,
            workspaces=["api", "api"],
            strip=dict(RUNNING["strip"], groups=[
                {"title": "api", "tabs": 1},
                {"title": "api", "tabs": 1},
            ]),
        )
        text = screen(status)
        self.assertIn("Strip matches Herdr (2)", text)
        self.assertNotIn("leftover", text)

    def test_a_second_window_is_called_out(self):
        status = dict(RUNNING, strip=dict(RUNNING["strip"], windows=2))
        self.assertIn("there should be one", screen(status))

    def test_stale_extension_points_at_the_key_that_fixes_it(self):
        self.assertIn("restart with [r]", screen(dict(RUNNING, extension_stale=True)))

    def test_nothing_running_still_renders(self):
        """The panel is summoned precisely when things are broken."""
        text = screen({
            "browser": False, "bridge": False, "extension_stale": True,
            "workspaces": [], "strip": None,
        })
        self.assertIn("Browser        not running", text)
        self.assertIn("Herdr link     not answering", text)
        self.assertIn("[r] restart", text)

    def test_tab_counts_read_correctly(self):
        self.assertEqual(panel.tab_count(1), "1 tab")
        self.assertEqual(panel.tab_count(2), "2 tabs")
        self.assertEqual(panel.tab_count(0), "0 tabs")

    def test_every_key_names_what_it_acts_on(self):
        """A label has to say what happens, not name the machinery."""
        for label in panel.KEYS:
            words = label.split("] ", 1)[1]
            self.assertGreaterEqual(len(words.split()), 2, label)
        self.assertNotIn("reconcile", " ".join(panel.KEYS))

    def test_the_keys_fold_instead_of_running_off_a_narrow_pane(self):
        """The panel is tiled now, so its width is whatever the user leaves it."""
        # A row may only exceed the width when it holds one key that cannot be
        # folded any further.
        unfoldable = max(len(key) for key in panel.KEYS) + 2
        for width in (80, 46, 28, 16):
            rows = panel.key_lines(width)
            self.assertTrue(all(len(row) <= max(width, unfoldable) for row in rows), width)
            self.assertIn("[q] close panel", " ".join(rows))
            self.assertIn("[r] restart browser", " ".join(rows))

    def test_a_narrow_pane_still_shows_every_key(self):
        text = screen(RUNNING, width=30)
        for key in ("[p]", "[s]", "[r]", "[k]", "[q]"):
            self.assertIn(key, text)

    def test_a_failed_action_is_reported_not_fatal(self):
        """The panel must survive the thing it exists to recover from.

        Restarting raises when the browser comes up but the extension does not
        answer — which is exactly when the panel is open — and the exception
        took the whole pane down with it, leaving the user with neither a
        browser nor the screen telling them why.
        """
        from unittest.mock import patch

        def explode(*args, **kwargs):
            raise RuntimeError("Chromium started but the extension never answered")

        for key, name in (("r", "ensure_browser"), ("s", "sync_workspaces"), ("p", "show_window")):
            with patch.object(panel.bridge, name, explode), \
                    patch.object(panel.bridge, "quit_browser", lambda: 1), \
                    patch.object(panel.bridge, "browser_pids", list):
                outcome = panel.act(key)
            self.assertIsInstance(outcome, str, key)
            self.assertIn("never answered", outcome, key)

    def test_restart_can_actually_bring_the_browser_back(self):
        """`[r]` is the key the panel itself points at for a stale extension,
        and it quits the browser first. It asked ensure_browser to make the
        browser usable without permission to start one, so after the quit the
        guard refused and the key left the user with no browser at all — with
        the refusal printed as a note, which reads like a failed restart
        rather than a browser that is now gone.

        The older test misses this because it patches ensure_browser out; this
        one drives the real one and watches for the spawn.
        """
        from unittest.mock import patch

        spawned = []
        with patch.object(panel.bridge, "quit_browser", lambda: 1), \
                patch.object(panel.bridge, "browser_pids", list), \
                patch.object(panel.bridge, "setup", lambda: "/usr/bin/chromium"), \
                patch.object(panel.bridge, "bridge_alive", lambda: False), \
                patch.object(panel.bridge, "browser_alive", lambda: False), \
                patch.object(panel.bridge, "launch_lock", contextlib.nullcontext), \
                patch.object(panel.bridge, "spawn_browser", spawned.append), \
                patch.object(panel.bridge, "wait_for_bridge", lambda: True), \
                patch.object(panel.bridge, "show_window", lambda: True), \
                patch.object(panel.bridge, "sync_workspaces", lambda: None):
            outcome = panel.act("r")

        self.assertEqual(spawned, ["/usr/bin/chromium"], outcome)
        self.assertEqual(outcome, "Browsr restarted.")

    def test_a_group_title_cannot_move_the_cursor_around_the_panel(self):
        """Titles are shown as they come back from the browser, and anything
        driving the browser can set one. An escape sequence in a title would
        be executed by the terminal the panel is drawn in."""
        status = dict(RUNNING, strip=dict(RUNNING["strip"], groups=[
            {"title": "api\x1b[2J\x1b[31mwiped", "tabs": 1},
            {"title": "search", "tabs": 1},
        ]))
        text = screen(status)
        # The escape byte is what the terminal obeys; what is left of the
        # sequence is then only text, and inventing a way to guess which
        # leftovers to also delete would be guesswork.
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", screen(dict(RUNNING, strip=dict(
            RUNNING["strip"], groups=[{"title": "api\x07\x00", "tabs": 1}]))))
        self.assertIn("api", text)

    def test_escape_and_q_both_leave(self):
        self.assertIsNone(panel.act("q"))
        self.assertIsNone(panel.act("\x1b"))

    def test_x_is_left_free_for_herdrs_own_close_pane(self):
        """prefix+x closes a pane; an `x` here that killed the browser is a trap."""
        keys = " ".join(panel.KEYS)
        self.assertNotIn("[x]", keys)
        self.assertIn("[k] quit browser", keys)
        self.assertIn("[q] close panel", keys)


if __name__ == "__main__":
    unittest.main()
