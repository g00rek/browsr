#!/usr/bin/python3
"""Herdr plugin entry point: setup, launch, hooks, and localhost routing."""

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
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
PANEL_PANES = STATE_DIR / "panel-panes.json"
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
        raise RuntimeError("Chromium is not on PATH")
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
            raise RuntimeError("The Chromium bridge did not answer")
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


class HerdrLink:
    """One connection to Herdr, held open and reused.

    The browser has to ask Herdr which workspace is in front several times a
    second, because a switch made in the UI reaches a plugin by no other route:
    it fires no plugin command and emits nothing on the event stream — both
    measured on Herdr 0.9.1. Spawning the CLI that often would be absurd, so
    this keeps one socket and reconnects on its own when Herdr restarts.
    """

    def __init__(self, path=None):
        self.path = path or herdr_session_id()
        self.socket = None
        self.buffer = b""
        self.counter = 0

    def close(self):
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
        self.socket = None
        self.buffer = b""

    def _connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(self.path)
        self.socket = client
        self.buffer = b""

    def _exchange(self, method, params):
        if self.socket is None:
            self._connect()
        self.counter += 1
        request_id = f"browsr-{self.counter}"
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.socket.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                # Anything that is not the answer to this request — a
                # subscription event, a late reply — is not ours to interpret.
                if message.get("id") == request_id:
                    return message.get("result")
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("Herdr closed the connection")
            self.buffer += chunk

    def request(self, method, params=None):
        """The result, or None when Herdr cannot be reached at all."""
        for attempt in (1, 2):
            try:
                return self._exchange(method, params)
            except (OSError, ValueError, ConnectionError, json.JSONDecodeError):
                self.close()
                if attempt == 2:
                    return None
        return None

    def workspaces(self):
        result = self.request("workspace.list")
        return (result or {}).get("workspaces") or []

    def focused_workspace(self):
        return next((item for item in self.workspaces() if item.get("focused")), None)


def herdr_call(*argv):
    """Run the Herdr CLI and hand back its output."""
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not herdr:
        raise RuntimeError("Herdr is not on PATH")
    return subprocess.run(
        [herdr, *argv], capture_output=True, text=True, timeout=10, check=True,
    ).stdout


def plugin_context():
    try:
        return json.loads(os.environ["HERDR_PLUGIN_CONTEXT_JSON"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return {}


def current_workspace_id():
    return os.environ.get("HERDR_WORKSPACE_ID") or plugin_context().get("workspace_id")


def current_tab_id():
    """A pane belongs to a tab, so the panel is tracked per tab, not per workspace."""
    return os.environ.get("HERDR_TAB_ID") or plugin_context().get("tab_id")


def focused_pane_id():
    """Where the cursor is, so the key can tell "go there" from "close it"."""
    context = plugin_context()
    if context.get("focused_pane_id"):
        return context["focused_pane_id"]
    try:
        for pane in json.loads(herdr_call("pane", "list"))["result"]["panes"]:
            if pane.get("focused"):
                return pane["pane_id"]
    except (OSError, KeyError, ValueError, RuntimeError,
            subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return None


def live_pane_ids():
    try:
        return {
            pane["pane_id"]
            for pane in json.loads(herdr_call("pane", "list"))["result"]["panes"]
        }
    except (OSError, KeyError, ValueError, RuntimeError,
            subprocess.SubprocessError, json.JSONDecodeError):
        return set()


def read_panel_panes():
    try:
        return json.loads(PANEL_PANES.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def record_panel_pane(tab_id, pane_id):
    """The panel says where it is, so one key can also reach or close it."""
    panes = read_panel_panes()
    panes[tab_id] = pane_id
    atomic_json(PANEL_PANES, panes)


def forget_panel_pane(tab_id):
    panes = read_panel_panes()
    if panes.pop(tab_id, None) is not None:
        atomic_json(PANEL_PANES, panes)


def toggle_panel():
    """One key, three outcomes — the convention the other Herdr sidebars follow.

    Not here          -> open it beside the current pane and go there.
    Here, cursor away -> go to it. Pressing the key while working elsewhere
                         means "show me", never "throw it away".
    Here, cursor in it-> close it.

    Tracked per tab, because that is what a pane belongs to.
    """
    tab = current_tab_id()
    pane_id = read_panel_panes().get(tab) if tab else None
    # A panel killed without tidying up leaves its id behind; Herdr is the
    # authority on whether that pane is still there.
    if pane_id and pane_id in live_pane_ids():
        if pane_id == focused_pane_id():
            herdr_call("plugin", "pane", "close", pane_id)
            forget_panel_pane(tab)
        else:
            herdr_call("plugin", "pane", "focus", pane_id)
        return
    if tab:
        forget_panel_pane(tab)
    herdr_call(
        "plugin", "pane", "open",
        "--plugin", "g00rek.browsr", "--entrypoint", "panel",
        "--placement", "split", "--direction", "right", "--focus",
    )


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
    # Hashed contents rather than size and modification time: a branch switch
    # rewrites every timestamp without changing a byte, and the panel then
    # announced stale code and asked for a restart that was not needed.
    digest = hashlib.sha256()
    for path in sorted(EXTENSION_DIR.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(EXTENSION_DIR)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def browser_pids(proc_root=Path("/proc")):
    """The Browsr browser process, and only it.

    Browsr runs the same binary as the browser the user lives in, so the
    profile directory is the only thing separating them — and every renderer
    and utility process carries that too, which is why `--type=` is excluded.
    """
    marker = f"--user-data-dir={PROFILE_DIR}"
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = entry.joinpath("cmdline").read_bytes()
        except OSError:
            continue
        # Chromium rewrites its own argv into a single space-separated blob, so
        # the NUL separators every other process leaves behind are simply not
        # there. Flattening first and splitting on whitespace reads both shapes,
        # and comparing whole fields keeps a longer profile path ending in the
        # same text from matching.
        fields = raw.replace(b"\0", b" ").decode(errors="replace").split()
        if any(field.startswith("--type=") for field in fields):
            continue
        if marker in fields:
            found.append(int(entry.name))
    return sorted(found)


def quit_browser():
    """Ask Browsr to close. Returns how many processes were asked."""
    pids = browser_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return len(pids)


def extension_is_stale():
    """Do the extension files differ from what the running browser was given?

    Chromium starts its own compiled copy of the service worker, so this is the
    only way to tell that the browser is running code the repository no longer
    has. It is the difference that went unnoticed for days.
    """
    try:
        return EXTENSION_STAMP.read_text() != extension_fingerprint()
    except OSError:
        return True


def browser_status():
    """Everything the panel puts on screen, gathered without starting anything."""
    workspaces = herdr_workspaces() or []
    strip = None
    if bridge_alive():
        try:
            answer = socket_request({
                "type": "status",
                "request_id": uuid.uuid4().hex,
                "session_id": herdr_session_id(),
            }, timeout=8)
            strip = answer.get("result") if answer.get("ok") else None
        except (OSError, RuntimeError, json.JSONDecodeError):
            strip = None
    return {
        "browser": browser_alive(),
        "bridge": bridge_alive(),
        "extension_stale": extension_is_stale(),
        "workspaces": [
            workspace.get("label") or workspace["workspace_id"]
            for workspace in workspaces
        ],
        "strip": strip,
    }


def print_status():
    print(json.dumps(browser_status(), ensure_ascii=False))


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
                            "Browsr is running but its extension is not "
                            "answering; reload it in chrome://extensions"
                        )
                else:
                    spawn_browser(chromium)
                    if not wait:
                        return
                    if not wait_for_bridge():
                        raise RuntimeError(
                            "Chromium started but the Herdr extension never reached the bridge"
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
        raise RuntimeError(f"Unsupported Herdr event: {event_name}")
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
        raise RuntimeError("This handler only takes localhost addresses")
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
        raise RuntimeError(response.get("error", "Chromium did not open the address"))


def open_url(raw_url):
    if not is_local_url(raw_url):
        raise RuntimeError("Browsr only automates localhost addresses")
    workspace_id = os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace_id:
        raise RuntimeError("No HERDR_WORKSPACE_ID; run this from a Herdr pane")
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
        raise RuntimeError(response.get("error", "Chromium did not open the address"))


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
        raise RuntimeError(response.get("error", "Could not read the workspace tabs"))
    print(json.dumps(response.get("result") or {"workspace_id": workspace_id, "tabs": []}))


def devtools_endpoint():
    ensure_browser(wait=True, show=False)
    if not devtools_alive(DEVTOOLS_PORT):
        raise RuntimeError(
            f"Browsr is not answering on port {DEVTOOLS_PORT}; see "
            f"{STATE_DIR / 'chromium.log'}"
        )
    print(f"http://127.0.0.1:{DEVTOOLS_PORT}")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "open-url":
        open_url(sys.argv[2])
        return
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "setup", "launch", "sync", "status", "panel", "hook", "open-link",
        "workspace-tabs", "mcp-endpoint",
    }:
        raise RuntimeError(
            "Usage: bridge.py setup|launch|sync|status|panel|hook|open-link"
            "|open-url URL|workspace-tabs|mcp-endpoint"
        )
    {
        "setup": setup,
        "sync": sync_workspaces,
        "status": print_status,
        "panel": toggle_panel,
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
