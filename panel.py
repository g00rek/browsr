#!/usr/bin/python3
"""A small Browsr panel for Herdr: what the browser is doing, and five keys."""

import importlib.util
import os
import select
import shutil
import sys
import termios
import time
import tty
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("bridge", ROOT / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

REFRESH_SECONDS = 2.0


def tab_count(count):
    return "1 tab" if count == 1 else f"{count} tabs"


def compare(strip, workspaces):
    """Which groups have no workspace, and which workspaces have no group."""
    if strip is None:
        return [], list(workspaces)
    remaining = Counter(workspaces)
    surplus = []
    for group in strip.get("groups", []):
        title = group.get("title", "")
        if remaining.get(title):
            remaining[title] -= 1
        else:
            surplus.append(title)
    missing = []
    for label, count in remaining.items():
        missing.extend([label] * count)
    return surplus, missing


# Each label names its object. "restart" alone reads as "restart the panel", and
# a label like "reconcile" is a word for the mechanism rather than for what the
# person gets, which is a strip that matches Herdr again.
#
# `x` is deliberately not used: Herdr's own close-pane key is prefix+x, and a key
# that closes the browser sitting next to one that closes the pane is a trap.
KEYS = [
    "[p] show browser",
    "[s] match Herdr",
    "[r] restart browser",
    "[k] quit browser",
    "[q] close panel",
]


def key_lines(width):
    """The key row, folded rather than cut off when the pane is narrow.

    This is a tiled pane now, so its width is whatever the user leaves it. A row
    that runs off the edge hides the very key that fixes what the panel is
    reporting.
    """
    rows, current = [], "  "
    for key in KEYS:
        candidate = current + key + "  "
        if len(candidate.rstrip()) > width and current.strip():
            rows.append(current.rstrip())
            current = "  " + key + "  "
        else:
            current = candidate
    if current.strip():
        rows.append(current.rstrip())
    return rows


def render(status, note="", width=80):
    """The whole screen as a list of lines. Pure, so it can be tested."""
    strip = status.get("strip")
    workspaces = status.get("workspaces", [])
    surplus, missing = compare(strip, workspaces)

    lines = ["  BROWSR", ""]
    lines.append(f"  Browser        {'running' if status.get('browser') else 'not running'}")
    # Not "bridge": the person reading this did not build it and has no reason
    # to know the machinery has a name. What they need to know is whether Herdr
    # and the browser are still talking.
    lines.append(f"  Herdr link     {'answering' if status.get('bridge') else 'not answering'}")
    if status.get("extension_stale"):
        lines.append("  Extension      older than the files — restart with [r]")
    else:
        lines.append("  Extension      up to date")

    if strip is None:
        lines.append("")
        lines.append("  Tab groups     unknown, Herdr link is silent")
    else:
        windows = strip.get("windows", 0)
        suffix = "" if windows == 1 else "  <- there should be one"
        lines.append(f"  Windows        {windows}{suffix}")
        lines.append("")
        if surplus or missing:
            lines.append(
                f"  Strip does NOT match Herdr "
                f"({len(strip.get('groups', []))} groups, {len(workspaces)} workspaces)"
            )
        else:
            lines.append(f"  Strip matches Herdr ({len(workspaces)})")
        shown = list(surplus)
        for group in strip.get("groups", []):
            title = group.get("title") or "(no name)"
            mark = "leftover" if title in shown else ""
            if mark:
                shown.remove(title)
            lines.append(f"    {title:<22} {tab_count(group.get('tabs', 0)):<8} {mark}")
        for label in missing:
            lines.append(f"    {label:<22} {'':<8} no group")

    lines.append("")
    if note:
        lines.append(f"  {note}")
        lines.append("")
    lines.extend(key_lines(width))
    return lines


def draw(status, note=""):
    width = shutil.get_terminal_size((80, 24)).columns
    sys.stdout.write("\033[2J\033[H")
    for line in render(status, note, width):
        sys.stdout.write(line[:width] + "\r\n")
    sys.stdout.flush()


def restart_browser():
    bridge.quit_browser()
    deadline = time.monotonic() + 10
    while bridge.browser_pids() and time.monotonic() < deadline:
        time.sleep(0.2)
    bridge.ensure_browser(wait=True, show=True, sync=True)


def act(key):
    """Run one key's action. Returns the line to show, or None to quit."""
    if key in ("q", "\x1b", "\x03"):
        return None
    if key == "p":
        return "Showing Browsr..." if bridge.show_window() else "No Browsr window found."
    if key == "s":
        bridge.sync_workspaces()
        return "Strip reconciled."
    if key == "r":
        restart_browser()
        return "Browsr restarted."
    if key == "k":
        closed = bridge.quit_browser()
        return "Browsr closed." if closed else "Browsr was not running anyway."
    return ""


def main():
    note = ""
    # Say where this pane is, so one key can also reach or close it.
    tab = bridge.current_tab_id()
    pane_id = os.environ.get("HERDR_PANE_ID")
    if tab and pane_id:
        bridge.record_panel_pane(tab, pane_id)
    settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())
        while True:
            draw(bridge.browser_status(), note)
            note = ""
            if not select.select([sys.stdin], [], [], REFRESH_SECONDS)[0]:
                continue
            key = sys.stdin.read(1)
            draw(bridge.browser_status(), "Working...")
            outcome = act(key)
            if outcome is None:
                return
            note = outcome
    finally:
        if tab:
            bridge.forget_panel_pane(tab)
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


if __name__ == "__main__":
    if not sys.stdin.isatty():
        print("The Browsr panel only runs in a terminal", file=sys.stderr)
        sys.exit(2)
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:  # a panel that crashes tells the user nothing
        os.write(2, f"Browsr panel: {error}\n".encode())
        sys.exit(1)
