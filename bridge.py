#!/usr/bin/python3
"""Herdr plugin entry point: setup, launch, hooks, and localhost routing."""

import contextlib
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".local/state/herdr-dev-browser"
DATA_DIR = Path.home() / ".local/share/herdr-dev-browser"
PROFILE_DIR = DATA_DIR / "chromium"
SOCKET_PATH = STATE_DIR / "control.sock"
LAUNCH_LOCK = STATE_DIR / "launch.lock"
EXTENSION_DIR = ROOT / "extension"
EXTENSION_STAMP = STATE_DIR / "extension.stamp"
DEFAULT_HERDR_SOCKET = Path.home() / ".config/herdr/herdr.sock"
# How long the extension gets to answer once Chromium is up. It is a service
# worker, so Chromium may have to start it first.
BRIDGE_WAIT_SECONDS = 15
_HERDR_SOCKET_CACHE = None
HOST_NAME = "dev.herdr.browser"
EXTENSION_ID = "lnknfooimknfekkpecbjnkjcjhdjmekj"
# A FIXED DevTools port, and the reason is other people's long-running sessions.
#
# Chromium was launched with `--remote-debugging-port=0`, so every start picked a
# new random port. The automation wrapper resolves that port ONCE, when it
# starts, and hands it to the client as `--browserUrl`; so the moment Browsr is
# restarted, every agent session already running is pointed at a dead port and
# its browser tools fail with "Could not connect to Chrome" until the whole
# client is reconnected — which looks like a broken integration rather than a
# moved port. A constant survives the restart.
#
# Deliberately not 9222: that is the port the user's own daily Chrome answers on
# for the same protocol, and the two must never be confused for each other.
#
# This number is also the only source of truth, which the first version of this
# change got wrong. `DevToolsActivePort` in the profile looks like a safety net
# but is not one: measured against the installed Chromium 152, that file is
# written only when the port is left to Chromium (`--remote-debugging-port=0`).
# Pinning the port stopped it being refreshed, so it kept serving the number
# from before the change and every client built on it failed to connect.
DEVTOOLS_PORT = 39222


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # One temp name per writer. Several callers reach setup() within the same
    # second after a reboot, and with a shared temp name the first one to finish
    # renames the file out from under the others, which then fail outright.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def setup():
    chromium = shutil.which("chromium")
    if not chromium:
        raise RuntimeError("Nie znaleziono Chromium w PATH")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": HOST_NAME,
        "description": "Native bridge between Herdr and its dedicated Chromium profile",
        "path": str(ROOT / "native_host.py"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    destinations = [
        PROFILE_DIR / "NativeMessagingHosts" / f"{HOST_NAME}.json",
        Path.home() / ".config/chromium/NativeMessagingHosts" / f"{HOST_NAME}.json",
    ]
    for destination in destinations:
        atomic_json(destination, manifest)
    atomic_json(STATE_DIR / "config.json", {
        "chromium": chromium,
        "profile": str(PROFILE_DIR),
        "extension": str(EXTENSION_DIR),
    })
    return chromium


def socket_request(message, timeout=2):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        raw = b""
        while b"\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
        if not raw:
            raise RuntimeError("Most Chromium nie odpowiedział")
        return json.loads(raw.split(b"\n", 1)[0])
    finally:
        client.close()


def devtools_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as answer:
            return answer.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def browser_alive():
    """Is Chromium itself up? Separate from whether the extension answers."""
    return devtools_alive(DEVTOOLS_PORT)


@contextlib.contextmanager
def launch_lock():
    """Serialise the decision to start Chromium across every caller.

    After a reboot the Herdr plugin, the workspace hooks and every agent's MCP
    server all reach for the browser within the same second. Without this each
    of them sees a bridge that has not come up yet and starts Chromium, and
    every start after the first only adds an empty window.
    """
    LAUNCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LAUNCH_LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def herdr_session_id():
    """Which Herdr server a workspace id belongs to.

    Two Herdr servers can each call a workspace `w14`, so the socket path is
    part of a workspace's identity. Falling back to a placeholder when the
    variable is missing is not free: a caller started outside a Herdr pane — an
    MCP server, a hook run by hand — then lands in a namespace of its own and is
    handed a second tab group for every workspace the user has.
    """
    from_environment = os.environ.get("HERDR_SOCKET_PATH")
    if from_environment:
        return from_environment
    global _HERDR_SOCKET_CACHE
    if _HERDR_SOCKET_CACHE:
        return _HERDR_SOCKET_CACHE
    resolved = str(DEFAULT_HERDR_SOCKET)
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if herdr:
        try:
            status = subprocess.run(
                [herdr, "status", "server"], capture_output=True, text=True,
                timeout=5, check=True,
            )
            for line in status.stdout.splitlines():
                name, separator, value = line.partition(":")
                if separator and name.strip() == "socket" and value.strip():
                    resolved = value.strip()
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    _HERDR_SOCKET_CACHE = resolved
    return resolved


def bridge_alive():
    try:
        return socket_request({"type": "ping"}, timeout=0.25).get("ok") is True
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False


def show_window():
    hyprctl = shutil.which("hyprctl")
    if not hyprctl:
        return False
    try:
        result = subprocess.run(
            [hyprctl, "clients", "-j"], capture_output=True, text=True,
            timeout=3, check=True,
        )
        expected_arg = f"--user-data-dir={PROFILE_DIR}"
        for client in json.loads(result.stdout):
            pid = int(client.get("pid", 0))
            if client.get("class") != "chromium" or pid <= 0:
                continue
            command = (Path(f"/proc/{pid}/cmdline").read_bytes()
                       .replace(b"\0", b" ").decode(errors="replace"))
            if expected_arg not in command:
                continue
            address = str(client.get("address", ""))
            int(address, 16)
            focus = subprocess.run(
                [hyprctl, "eval", f'hl.dsp.window.focus("address:{address}")'],
                capture_output=True, text=True, timeout=3,
            )
            return focus.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return False
    return False


def herdr_workspaces():
    """Herdr's own workspace list, or None when it will not answer.

    None and an empty list are different answers and must stay different: the
    extension treats an empty list as "say nothing" precisely so that a Herdr
    that is down cannot be read as a user with no workspaces.
    """
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not herdr:
        return None
    try:
        output = subprocess.run(
            [herdr, "workspace", "list"], capture_output=True, text=True,
            timeout=8, check=True,
        )
        return json.loads(output.stdout)["result"]["workspaces"] or None
    except (OSError, KeyError, ValueError, subprocess.SubprocessError,
            json.JSONDecodeError):
        return None


def workspace_set_message(workspaces, session_id=None):
    return {
        "type": "workspace_set",
        "session_id": session_id or herdr_session_id(),
        "workspaces": [
            {
                "workspace_id": workspace["workspace_id"],
                "label": workspace.get("label") or workspace["workspace_id"],
            }
            for workspace in workspaces
        ],
    }


def sync_workspaces():
    workspaces = herdr_workspaces()
    if not workspaces:
        return
    try:
        session_id = herdr_session_id()
        # The whole set in one message, not a workspace at a time. A workspace
        # closed while the bridge was down leaves an event that is never resent,
        # so the strip could only ever grow; sending the full list lets the
        # extension take away what Herdr no longer has.
        message = workspace_set_message(workspaces, session_id)
        message["request_id"] = uuid.uuid4().hex
        socket_request(message, timeout=20)
        focused = next((item for item in workspaces if item.get("focused")), None)
        if focused:
            socket_request({
                "type": "workspace",
                "event": "focused",
                "request_id": uuid.uuid4().hex,
                "session_id": session_id,
                "workspace_id": focused["workspace_id"],
                "label": focused.get("label") or focused["workspace_id"],
            }, timeout=10)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return


def launch(wait=True):
    return ensure_browser(wait=wait, show=True, sync=True)


def extension_fingerprint():
    parts = []
    for path in sorted(EXTENSION_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            parts.append(f"{path.relative_to(EXTENSION_DIR)}:{stat.st_size}:{stat.st_mtime_ns}")
    return "\n".join(parts)


def refresh_extension_if_changed():
    """Make the extension Chromium runs match the extension on disk.

    Chromium keeps a compiled copy of an unpacked extension's service worker in
    the profile and starts that copy again on the next run. Restarting the
    browser does not refresh it and neither does raising the version in the
    manifest, so an edited extension can keep running its old code for days —
    the repository looks fixed while the browser is not. Dropping the profile's
    service-worker registration forces Chromium to read the files again.
    """
    fingerprint = extension_fingerprint()
    try:
        unchanged = EXTENSION_STAMP.read_text() == fingerprint
    except OSError:
        unchanged = False
    if unchanged:
        return False
    shutil.rmtree(PROFILE_DIR / "Default" / "Service Worker", ignore_errors=True)
    EXTENSION_STAMP.parent.mkdir(parents=True, exist_ok=True)
    EXTENSION_STAMP.write_text(fingerprint)
    return True


def spawn_browser(chromium):
    refresh_extension_if_changed()
    # A leftover from an older run can only mislead whoever reads it: with a
    # fixed port Chromium never writes this file again.
    (PROFILE_DIR / "DevToolsActivePort").unlink(missing_ok=True)
    log = (STATE_DIR / "chromium.log").open("wb", buffering=0)
    subprocess.Popen([
        chromium,
        f"--user-data-dir={PROFILE_DIR}",
        f"--load-extension={EXTENSION_DIR}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={DEVTOOLS_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "chrome://newtab/",
        "--enable-logging=stderr",
    ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()


def wait_for_bridge():
    deadline = time.monotonic() + BRIDGE_WAIT_SECONDS
    while True:
        if bridge_alive():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def ensure_browser(wait=True, show=False, sync=False):
    chromium = setup()
    if not bridge_alive():
        with launch_lock():
            # Whoever held the lock may have finished the job already.
            if not bridge_alive():
                if browser_alive():
                    # Chromium is up and only the extension is behind. Starting
                    # Chromium again starts nothing: the command line is handed
                    # to the copy already running, which answers it by opening
                    # one more empty window, and an extra window is what leaves
                    # the extension guessing which one holds the real strip.
                    if not wait_for_bridge():
                        raise RuntimeError(
                            "Browsr działa, ale rozszerzenie nie odpowiada; "
                            "przeładuj je w chrome://extensions"
                        )
                else:
                    spawn_browser(chromium)
                    if not wait:
                        return
                    if not wait_for_bridge():
                        raise RuntimeError(
                            "Chromium ruszył, ale rozszerzenie Herdr nie połączyło się z mostem"
                        )
                # The workspace list is re-sent only when the bridge has just
                # come up. Re-sending it on every call is what turned one bad
                # lookup into a fresh set of tab groups each time.
                sync = True
    if show:
        show_window()
    if sync:
        sync_workspaces()


def event_command():
    event_wrapper = json.loads(os.environ["HERDR_PLUGIN_EVENT_JSON"])
    context = json.loads(os.environ["HERDR_PLUGIN_CONTEXT_JSON"])
    event_name = event_wrapper.get("event", "")
    mapping = {
        "workspace_created": "created",
        "workspace_renamed": "renamed",
        "workspace_closed": "closed",
        "workspace_focused": "focused",
    }
    if event_name not in mapping:
        raise RuntimeError(f"Nieobsługiwane zdarzenie Herdr: {event_name}")
    return {
        "type": "workspace",
        "event": mapping[event_name],
        "session_id": herdr_session_id(),
        "workspace_id": context["workspace_id"],
        "label": context.get("workspace_label") or context["workspace_id"],
    }


def hook():
    if not bridge_alive():
        return
    command = event_command()
    try:
        # A switch is the one event that happens all day and cannot race with
        # the workspace it names, so it carries the whole list. Events are
        # fire-and-forget: one lost while the bridge was down is never resent,
        # and without this the strip stays wrong until the browser restarts.
        if command["event"] == "focused":
            workspaces = herdr_workspaces()
            if workspaces:
                socket_request(workspace_set_message(workspaces, command["session_id"]))
        socket_request(command)
    except (OSError, RuntimeError, json.JSONDecodeError):
        # The bridge can go down between the check above and the send. Switching
        # workspace must not fail in Herdr's face over it; the next switch, or
        # the extension coming back, puts the strip right.
        return


def is_local_url(raw):
    try:
        parsed = urlsplit(raw)
        return parsed.scheme.lower() in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def open_link():
    context = json.loads(os.environ["HERDR_PLUGIN_CONTEXT_JSON"])
    url = context.get("clicked_url", "")
    if not is_local_url(url):
        raise RuntimeError("Ten handler przyjmuje tylko adresy localhost")
    ensure_browser(wait=True, show=False)
    response = socket_request({
        "type": "open_url",
        "request_id": uuid.uuid4().hex,
        "session_id": herdr_session_id(),
        "workspace_id": context["workspace_id"],
        "workspace_label": context.get("workspace_label") or context["workspace_id"],
        "url": url,
        "focus": True,
    }, timeout=10)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Chromium nie otworzył adresu"))


def open_url(raw_url):
    if not is_local_url(raw_url):
        raise RuntimeError("Browsr automatyzuje tylko adresy localhost")
    workspace_id = os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace_id:
        raise RuntimeError("Brak HERDR_WORKSPACE_ID; uruchom polecenie z pane Herdr")
    ensure_browser(wait=True, show=False)
    response = socket_request({
        "type": "open_url",
        "request_id": uuid.uuid4().hex,
        "session_id": herdr_session_id(),
        "workspace_id": workspace_id,
        # No label on purpose. This path runs from an agent's shell, which knows
        # the workspace id but not what the user called it, and sending the id
        # as the label renamed the group under them.
        "url": raw_url,
        "focus": False,
    }, timeout=10)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Chromium nie otworzył adresu"))


def workspace_tabs():
    workspace_id = os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace_id:
        raise RuntimeError("Brak HERDR_WORKSPACE_ID; uruchom polecenie z pane Herdr")
    ensure_browser(wait=True, show=False)
    response = socket_request({
        "type": "workspace_tabs",
        "request_id": uuid.uuid4().hex,
        "session_id": herdr_session_id(),
        "workspace_id": workspace_id,
    }, timeout=10)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Nie udało się pobrać kart workspace"))
    print(json.dumps(response.get("result") or {"workspace_id": workspace_id, "tabs": []}))


def devtools_endpoint():
    ensure_browser(wait=True, show=False)
    if not devtools_alive(DEVTOOLS_PORT):
        raise RuntimeError(
            f"Browsr nie odpowiada na porcie {DEVTOOLS_PORT}; sprawdź "
            f"{STATE_DIR / 'chromium.log'}"
        )
    print(f"http://127.0.0.1:{DEVTOOLS_PORT}")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "open-url":
        open_url(sys.argv[2])
        return
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "setup", "launch", "sync", "hook", "open-link", "workspace-tabs", "mcp-endpoint"
    }:
        raise RuntimeError(
            "Użycie: bridge.py setup|launch|sync|hook|open-link|open-url URL"
            "|workspace-tabs|mcp-endpoint"
        )
    {
        "setup": setup,
        "sync": sync_workspaces,
        "launch": launch,
        "hook": hook,
        "open-link": open_link,
        "workspace-tabs": workspace_tabs,
        "mcp-endpoint": devtools_endpoint,
    }[sys.argv[1]]()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Browsr: {error}", file=sys.stderr)
        sys.exit(1)
