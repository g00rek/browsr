#!/usr/bin/python3
"""Attach chrome-devtools-mcp to the Browsr Chromium owned by Herdr."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
import cdp_filter  # noqa: E402

# The version of chrome-devtools-mcp to run.
#
# `@latest` asked npm on every single start. Claude Code gives a server 30
# seconds to answer the handshake and some starts spent 19 of them on that
# question; offline it cannot be answered at all, and the tool is then simply
# missing for the rest of the session, because a stdio server that fails to
# start is never retried until someone reconnects it by hand.
#
# Raise this deliberately, after trying the new version against Browsr.
MCP_VERSION = "1.12.0"

# Enough to see why a start went wrong, and little enough that nobody has to
# tend it. Rolled over rather than rotated: what matters is the last few
# starts, and a second file to read is a second file to forget.
LOG_LIMIT = 1_000_000

# What Herdr puts in a pane's environment, under either way of starting one.
HERDR_VARS = (
    "HERDR_WORKSPACE_ID",
    "HERDR_SOCKET_PATH",
    "HC_TERMINAL_ID",
    "HC_TERMINAL_SOCKET",
    "HERDR_APP_HOME",
)


def log_path():
    return bridge.STATE_DIR / "mcp.log"


def note(line):
    """One line in the wrapper's own log; never a reason to fail.

    Until this existed, a start that went wrong left nothing at all behind:
    Claude Code records that the server stopped, and the wrapper recorded
    neither what it was told about the pane it was in nor what the child did.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.unlink()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a") as handle:
            handle.write(f"{stamp} [{os.getpid()}] {line}\n")
    except OSError:
        pass


def main():
    present = " ".join(name for name in HERDR_VARS if os.environ.get(name)) or "none"
    note(f"start, herdr env: {present}")

    # Asked for the log, not as a gate. The workspace is needed by the two
    # calls that open a tab, and `ask_extension` says so plainly when it is
    # missing; everything else works without it. Exiting here instead meant
    # that a Herdr socket which happened to be busy for a moment cost the
    # session its browser tooling until someone pressed /mcp — the same
    # mistake as exiting because the browser was not running.
    try:
        workspace = bridge.current_workspace_id()
    except Exception as error:  # the socket, the app, anything under them
        workspace = None
        note(f"workspace: could not ask ({error})")
    else:
        note(f"workspace: {workspace or 'unresolved — tabs will say so, the rest works'}")

    # chrome-devtools-mcp is pointed at a local filtering proxy instead of at
    # Chromium directly, so that commands which would raise the Browsr window
    # (and steal the user's focus) never reach the browser. The proxy finds the
    # browser on each request and answers 503 with the reason while it is down;
    # chrome-devtools-mcp connects lazily and retries on its next tool call.
    # The MCP protocol runs over stdio, so the child inherits ours; the proxy
    # lives in daemon threads of this process and dies with it, which is why
    # this is not an exec.
    _server, proxy_url = cdp_filter.start_proxy(cdp_filter.resolve_with_bridge)
    child = subprocess.Popen([
        "npx",
        f"chrome-devtools-mcp@{MCP_VERSION}",
        f"--browser-url={proxy_url}",
    ])
    note(f"child {child.pid}: chrome-devtools-mcp@{MCP_VERSION} on {proxy_url}")

    def forward(signum, _frame):
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        try:
            code = child.wait()
        except KeyboardInterrupt:
            continue
        if code < 0:
            note(f"child {child.pid} killed by signal {-code}")
        else:
            note(f"child {child.pid} exited {code}")
        return code


if __name__ == "__main__":
    sys.exit(main())
