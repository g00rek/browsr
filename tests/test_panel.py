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
    "workspaces": ["TasteRay", "atlas"],
    "strip": {
        "windows": 1,
        "ungrouped": 1,
        "groups": [
            {"title": "TasteRay", "tabs": 2, "adopted": False},
            {"title": "atlas", "tabs": 1, "adopted": False},
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
            {"title": "TasteRay", "tabs": 2},
            {"title": "atlas", "tabs": 1},
            {"title": "board-games", "tabs": 1},
        ]))
        text = screen(status)
        self.assertIn("does NOT match", text)
        self.assertRegex(text, r"board-games\s+1 tab\s+leftover")

    def test_a_workspace_with_no_group_is_named_too(self):
        status = dict(RUNNING, workspaces=["TasteRay", "atlas", "browsr"])
        self.assertRegex(screen(status), r"browsr\s+no group")

    def test_two_workspaces_sharing_a_label_are_not_read_as_leftovers(self):
        status = dict(
            RUNNING,
            workspaces=["TasteRay", "TasteRay"],
            strip=dict(RUNNING["strip"], groups=[
                {"title": "TasteRay", "tabs": 1},
                {"title": "TasteRay", "tabs": 1},
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
        self.assertIn("Bridge         not answering", text)
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
