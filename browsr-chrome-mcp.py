#!/usr/bin/python3
"""Attach chrome-devtools-mcp to the Browsr Chromium owned by Herdr."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    if not os.environ.get("HERDR_WORKSPACE_ID"):
        print("Browsr MCP must run inside a Herdr pane", file=sys.stderr)
        return 2
    endpoint = subprocess.run(
        [str(ROOT / "bridge.py"), "mcp-endpoint"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if endpoint.returncode != 0:
        print(endpoint.stderr.strip() or "Browsr failed to provide DevTools", file=sys.stderr)
        return endpoint.returncode
    browser_url = endpoint.stdout.strip()
    if not browser_url.startswith("http://127.0.0.1:"):
        print("Browsr returned an invalid DevTools endpoint", file=sys.stderr)
        return 2
    os.execvp("npx", [
        "npx",
        "chrome-devtools-mcp@latest",
        f"--browser-url={browser_url}",
    ])


if __name__ == "__main__":
    sys.exit(main())
