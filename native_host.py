#!/usr/bin/python3
"""Chromium native-messaging host and local Unix-socket bridge for Herdr."""

import json
import os
import queue
import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge  # noqa: E402  (same directory, and only for its Herdr helpers)

STATE_DIR = Path.home() / ".local/state/herdr-dev-browser"
SOCKET_PATH = STATE_DIR / "control.sock"
MAX_MESSAGE = 1024 * 1024

write_lock = threading.Lock()
pending_lock = threading.Lock()
pending = {}
stopping = threading.Event()


def read_native_message():
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise RuntimeError("truncated native-message header")
    length = struct.unpack("<I", header)[0]
    if length > MAX_MESSAGE:
        raise RuntimeError("native message is too large")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        raise RuntimeError("truncated native-message body")
    return json.loads(payload)


def write_native_message(message):
    payload = json.dumps(message, separators=(",", ":")).encode()
    with write_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(payload)))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def reconcile_workspaces():
    """Send the extension the whole workspace list, down the pipe it opened.

    Chromium stops an idle extension and starts it again on the next event, and
    nothing in the plugin runs when that happens — so a browser left running for
    days keeps whatever strip it drifted into. The extension announcing itself
    is the one moment that is guaranteed to follow every such restart.
    """
    workspaces = bridge.herdr_workspaces()
    if not workspaces:
        return
    write_native_message(bridge.workspace_set_message(workspaces))


# Five times a second, over a connection that is already open: a switch is
# followed before the eye notices, and it costs no process and no new socket.
FOCUS_POLL_SECONDS = 0.2


def focus_step(last_workspace_id, link):
    """Tell the extension which workspace is in front, but only when it changes.

    Herdr does not call a plugin when the workspace is switched in the UI —
    measured on Herdr 0.9.1: a switch through the API reaches the plugin within
    the same second, six switches made with the mouse reached it never, though
    the server recorded every one. So the browser cannot wait to be told. It
    asks, once a second, and speaks only when the answer is different: at most
    one message per switch, which is less traffic than being told would be.
    """
    focused = link.focused_workspace()
    if not focused or focused["workspace_id"] == last_workspace_id:
        return last_workspace_id
    write_native_message({
        "type": "workspace",
        "event": "focused",
        "session_id": bridge.herdr_session_id(),
        "workspace_id": focused["workspace_id"],
        "label": focused.get("label") or focused["workspace_id"],
    })
    return focused["workspace_id"]


def watch_focus():
    link = bridge.HerdrLink()
    last = None
    try:
        while not stopping.is_set():
            try:
                last = focus_step(last, link)
            except Exception as error:
                print(f"Herdr focus watch failed: {error}", file=sys.stderr)
            stopping.wait(FOCUS_POLL_SECONDS)
    finally:
        link.close()


def native_reader():
    try:
        while not stopping.is_set():
            message = read_native_message()
            if message is None:
                break
            if message.get("type") == "ready":
                threading.Thread(target=reconcile_workspaces, daemon=True).start()
                continue
            if message.get("type") == "response" and message.get("request_id"):
                with pending_lock:
                    target = pending.get(message["request_id"])
                if target:
                    target.put(message)
    except Exception as error:
        print(f"Herdr native bridge input failed: {error}", file=sys.stderr)
    finally:
        stopping.set()


def handle_client(connection):
    try:
        raw = b""
        while b"\n" not in raw and len(raw) <= MAX_MESSAGE:
            chunk = connection.recv(65536)
            if not chunk:
                break
            raw += chunk
        message = json.loads(raw.split(b"\n", 1)[0])
        request_id = message.get("request_id")
        response_queue = None
        if request_id:
            response_queue = queue.Queue(maxsize=1)
            with pending_lock:
                pending[request_id] = response_queue
        write_native_message(message)
        if response_queue:
            try:
                response = response_queue.get(timeout=8)
            except queue.Empty:
                response = {"ok": False, "error": "Chromium extension timed out"}
            finally:
                with pending_lock:
                    pending.pop(request_id, None)
        else:
            response = {"ok": True}
        connection.sendall(json.dumps(response).encode() + b"\n")
    except Exception as error:
        try:
            connection.sendall(json.dumps({"ok": False, "error": str(error)}).encode() + b"\n")
        except OSError:
            pass
    finally:
        connection.close()


def remove_socket_if_ours(inode):
    """Tidy up only the socket this process actually created.

    Hosts overlap: the extension reconnects, Chromium starts a new one, and it
    binds the same path before the old one has noticed its pipe is closed. An
    unconditional unlink here deletes the live host's socket and takes the whole
    bridge down with it.
    """
    try:
        if SOCKET_PATH.stat().st_ino == inode:
            SOCKET_PATH.unlink()
    except (FileNotFoundError, OSError):
        pass


def socket_server():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)
    inode = SOCKET_PATH.stat().st_ino
    server.listen(16)
    server.settimeout(0.5)
    try:
        while not stopping.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(connection,), daemon=True).start()
    finally:
        server.close()
        remove_socket_if_ours(inode)


def main():
    server_thread = threading.Thread(target=socket_server, daemon=True)
    server_thread.start()
    threading.Thread(target=watch_focus, daemon=True).start()
    native_reader()
    server_thread.join(timeout=1)


if __name__ == "__main__":
    main()

