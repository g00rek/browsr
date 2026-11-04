#!/usr/bin/python3
"""Attach chrome-devtools-mcp to the Browsr Chromium owned by Herdr."""

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
import cdp_filter  # noqa: E402


def main():
    # Ask the bridge, not the environment: the terminal Herdr hands a pane
    # HERDR_WORKSPACE_ID, the Herdr desktop app hands it a terminal id
    # instead, and only the bridge knows both. Checking the variable alone
    # refused every session started from the app.
    if not bridge.current_workspace_id():
        print("Browsr MCP must run inside a Herdr pane", file=sys.stderr)
        return 2
    # Nothing is asked of the browser here. This used to exit when Browsr was
    # not running, and Claude Code never retries a server that failed to start,
    # so every session begun while the browser was down stayed without the tool
    # until /mcp was reconnected by hand. The proxy now finds the browser on
    # each request and answers 503 with the reason while it is down;
    # chrome-devtools-mcp connects lazily and retries on its next tool call.
    # chrome-devtools-mcp is pointed at a local filtering proxy instead of at
    # Chromium directly, so that commands which would raise the Browsr window
    # (and steal the user's focus) never reach the browser. The MCP protocol
    # runs over stdio, so the child inherits ours; the proxy lives in daemon
    # threads of this process and dies with it, which is why this is no longer
    # an exec.
    _server, proxy_url = cdp_filter.start_proxy(cdp_filter.resolve_with_bridge)
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
