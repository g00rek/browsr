#!/usr/bin/python3
"""Attach chrome-devtools-mcp to the Browsr Chromium owned by Herdr."""

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import cdp_filter  # noqa: E402


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
    # chrome-devtools-mcp is pointed at a local filtering proxy instead of at
    # Chromium directly, so that commands which would raise the Browsr window
    # (and steal the user's focus) never reach the browser. The MCP protocol
    # runs over stdio, so the child inherits ours; the proxy lives in daemon
    # threads of this process and dies with it, which is why this is no longer
    # an exec.
    _server, proxy_url = cdp_filter.start_proxy(browser_url)
    child = subprocess.Popen([
        "npx",
        "chrome-devtools-mcp@latest",
        f"--browser-url={proxy_url}",
    ])

    def forward(signum, _frame):
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue


if __name__ == "__main__":
    sys.exit(main())
