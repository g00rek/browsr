#!/usr/bin/python3
"""Herdr plugin entry point: setup, launch, hooks, and localhost routing."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".local/state/herdr-dev-browser"
DATA_DIR = Path.home() / ".local/share/herdr-dev-browser"
PROFILE_DIR = DATA_DIR / "chromium"
SOCKET_PATH = STATE_DIR / "control.sock"
HOST_NAME = "dev.herdr.browser"
EXTENSION_ID = "lnknfooimknfekkpecbjnkjcjhdjmekj"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
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
        "extension": str(ROOT / "extension"),
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


def sync_workspaces():
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not herdr:
        return
    try:
        output = subprocess.run(
            [herdr, "workspace", "list"], capture_output=True, text=True,
            timeout=8, check=True,
        )
        workspaces = json.loads(output.stdout)["result"]["workspaces"]
        session_id = os.environ.get("HERDR_SOCKET_PATH", "default")
        for workspace in workspaces:
            message = {
                "type": "workspace",
                "event": "created",
                "session_id": session_id,
                "workspace_id": workspace["workspace_id"],
                "label": workspace.get("label") or workspace["workspace_id"],
            }
            socket_request(message)
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
    except (OSError, KeyError, ValueError, subprocess.SubprocessError,
            json.JSONDecodeError):
        return


def launch(wait=True):
    return ensure_browser(wait=wait, show=True)


def ensure_browser(wait=True, show=False):
    chromium = setup()
    if bridge_alive():
        if show:
            show_window()
        sync_workspaces()
        return
    else:
        log_path = STATE_DIR / "chromium.log"
        log = log_path.open("wb", buffering=0)
        subprocess.Popen([
            chromium,
            f"--user-data-dir={PROFILE_DIR}",
            f"--load-extension={ROOT / 'extension'}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "chrome://newtab/",
            "--enable-logging=stderr",
        ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        log.close()
    if wait:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if bridge_alive():
                if show:
                    show_window()
                sync_workspaces()
                return
            time.sleep(0.1)
        raise RuntimeError("Chromium ruszył, ale rozszerzenie Herdr nie połączyło się z mostem")


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
        "session_id": os.environ.get("HERDR_SOCKET_PATH", "default"),
        "workspace_id": context["workspace_id"],
        "label": context.get("workspace_label") or context["workspace_id"],
    }


def hook():
    if not bridge_alive():
        return
    socket_request(event_command())


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
        "session_id": os.environ.get("HERDR_SOCKET_PATH", "default"),
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
        "session_id": os.environ.get("HERDR_SOCKET_PATH", "default"),
        "workspace_id": workspace_id,
        "workspace_label": workspace_id,
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
        "session_id": os.environ.get("HERDR_SOCKET_PATH", "default"),
        "workspace_id": workspace_id,
    }, timeout=10)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Nie udało się pobrać kart workspace"))
    print(json.dumps(response.get("result") or {"workspace_id": workspace_id, "tabs": []}))


def devtools_endpoint():
    ensure_browser(wait=True, show=False)
    active_port = PROFILE_DIR / "DevToolsActivePort"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            port = int(active_port.read_text().splitlines()[0])
            print(f"http://127.0.0.1:{port}")
            return
        except (FileNotFoundError, ValueError, IndexError):
            time.sleep(0.1)
    raise RuntimeError("Browsr nie udostępnił endpointu DevTools")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "open-url":
        open_url(sys.argv[2])
        return
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "setup", "launch", "hook", "open-link", "workspace-tabs", "mcp-endpoint"
    }:
        raise RuntimeError(
            "Użycie: bridge.py setup|launch|hook|open-link|open-url URL|workspace-tabs|mcp-endpoint"
        )
    {
        "setup": setup,
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
