"""Local HTTP + SSE GUI for browsing and watching converse sessions.

Run with `python -m converse.gui` (default http://127.0.0.1:8765/).

Architecture
  - http.server.ThreadingHTTPServer (stdlib only, matches the project's
    zero-dep posture).
  - One `python -m converse --json tail <session> <viewer>` subprocess per
    *active* session — lazy-spawned on the first browser SSE client and
    torn down when the last client disconnects. Browser tabs share the
    same tail; only one synthetic `gui-viewer-XXXX` ever appears in the
    room regardless of how many tabs are open.
  - SSE clients subscribe via an in-process queue.Queue; the tail reader
    thread normalizes each line and broadcasts to every subscriber.

Wire format
  Every SSE event is a single JSON object with a `type` discriminator:
    {type:'message', id, user_id, text, created_at}
    {type:'join'   , user_id, created_at}
    {type:'leave'  , user_id, created_at}
    {type:'roster' , active:[user_id, ...], created_at}    # sent first
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import client as _client
from . import protocol as _protocol

STATIC_DIR = Path(__file__).parent / "gui_static"
VIEWER_ROLE = "gui-viewer"
HEARTBEAT_SECS = 15
QUEUE_MAX = 1024

_STREAMS_LOCK = threading.Lock()
_STREAMS: dict[str, "SessionStream"] = {}


# ---------- per-session tail multiplexer ----------

class SessionStream:
    """Owns one tail subprocess for a session and fans out to N SSE clients."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.viewer_id: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stopping = False

    def _start(self) -> None:
        resp = _client.request({
            "op": _protocol.OP_JOIN,
            "session": self.session_id,
            "name": VIEWER_ROLE,
        })
        self.viewer_id = resp["user"]["id"]
        # --no-history: every SSE subscriber gets history injected by
        # subscribe() (uniform path; otherwise only the first tab would see
        # backlog, every later tab would open near-empty).
        cmd = [
            sys.executable, "-m", "converse",
            "--json", "tail", self.session_id, self.viewer_id,
            "--no-history",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = self._normalize(obj)
            if evt is not None:
                self._broadcast(evt)
        # Tail subprocess exited — signal EOF to all subscribers.
        with self.lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def _normalize(self, obj: dict) -> Optional[dict]:
        if obj.get("attached"):
            return None
        evt = obj.get("event")
        if evt in ("join", "leave"):
            uid = obj.get("user_id") or ""
            # Hide the synthetic gui-viewer identity from the room view.
            if uid.startswith(f"{VIEWER_ROLE}-"):
                return None
            return {
                "type": evt,
                "user_id": uid,
                "created_at": time.time(),
            }
        if "id" in obj and "text" in obj:
            return {
                "type": "message",
                "id": obj["id"],
                "user_id": obj.get("user_id"),
                "text": obj.get("text", ""),
                "created_at": obj.get("created_at", time.time()),
            }
        return None

    def _broadcast(self, evt: dict) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass  # drop for a slow client; live stream is best-effort

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        # Hold the lock across the snapshot RPCs so any concurrent broadcasts
        # from the tail thread queue *after* roster + history. Brief pause
        # (one local UDS RTT). Frontend dedupes by message id, so the rare
        # overlap between an in-flight live event and OP_HISTORY is harmless.
        with self.lock:
            first = len(self.subscribers) == 0
            try:
                wresp = _client.request({"op": _protocol.OP_WHO, "session": self.session_id})
                active = [
                    u["id"] for u in wresp.get("users", [])
                    if u.get("active") and not u["id"].startswith(f"{VIEWER_ROLE}-")
                ]
            except _client.DaemonError:
                active = []
            try:
                q.put_nowait({"type": "roster", "active": active, "created_at": time.time()})
            except queue.Full:
                pass
            try:
                hresp = _client.request({"op": _protocol.OP_HISTORY, "session": self.session_id})
                for m in hresp.get("messages", []):
                    try:
                        q.put_nowait({
                            "type": "message",
                            "id": m["id"],
                            "user_id": m.get("user_id"),
                            "text": m.get("text", ""),
                            "created_at": m.get("created_at", time.time()),
                        })
                    except queue.Full:
                        break
            except _client.DaemonError:
                pass
            self.subscribers.append(q)
        if first:
            self._start()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass
            empty = len(self.subscribers) == 0
        if empty:
            self._stop()

    def _stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        with _STREAMS_LOCK:
            if _STREAMS.get(self.session_id) is self:
                _STREAMS.pop(self.session_id, None)


def _get_or_create_stream(session_id: str) -> SessionStream:
    with _STREAMS_LOCK:
        s = _STREAMS.get(session_id)
        if s is None:
            s = SessionStream(session_id)
            _STREAMS[session_id] = s
        return s


# ---------- HTTP handler ----------

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class GUIHandler(BaseHTTPRequestHandler):
    server_version = "converse-gui/0.1"

    def log_message(self, fmt, *args):  # quiet by default
        return

    # --- response helpers ---

    def _send(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _serve_static(self, name: str) -> None:
        # Resolve safely under STATIC_DIR.
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(403, b"forbidden")
            return
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            self._send(404, b"not found")
            return
        ctype = _STATIC_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, data, ctype=ctype)

    def _read_json_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return None
        if length > 1024 * 1024:  # 1 MiB cap (a single send can be large)
            self._send_json(413, {"error": "body too large"})
            return None
        try:
            raw = self.rfile.read(length)
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(obj, dict):
            self._send_json(400, {"error": "JSON object expected"})
            return None
        return obj

    # --- routing ---

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path.startswith("/session/"):
            # Same HTML for every session id; the JS reads the id from the URL.
            self._serve_static("session.html")
            return

        if path == "/api/sessions":
            try:
                resp = _client.request({"op": _protocol.OP_LIST})
                sessions = resp.get("sessions", [])
                # Hide the GUI's own bookkeeping viewer-id from picker cards.
                for s in sessions:
                    au = s.get("active_users") or []
                    s["active_users"] = [u for u in au if not u.startswith(f"{VIEWER_ROLE}-")]
                self._send_json(200, sessions)
            except _client.DaemonError as e:
                self._send_json(500, {"error": str(e)})
            return

        if path.startswith("/api/sessions/") and path.endswith("/who"):
            sid = path[len("/api/sessions/"):-len("/who")]
            try:
                resp = _client.request({"op": _protocol.OP_WHO, "session": sid})
                users = [u for u in resp.get("users", []) if not u["id"].startswith(f"{VIEWER_ROLE}-")]
                self._send_json(200, users)
            except _client.DaemonError as e:
                self._send_json(404, {"error": str(e)})
            return

        if path.startswith("/api/sessions/") and path.endswith("/stream"):
            sid = path[len("/api/sessions/"):-len("/stream")]
            self._handle_sse(sid)
            return

        self._send(404, b"not found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if path.startswith("/api/sessions/") and path.endswith("/join"):
            sid = path[len("/api/sessions/"):-len("/join")]
            body = self._read_json_body()
            if body is None:
                return
            name = body.get("name")
            reattach = body.get("reattach")
            if (name and reattach) or (not name and not reattach):
                self._send_json(400, {"error": "exactly one of `name` or `reattach` is required"})
                return
            req: dict = {"op": _protocol.OP_JOIN, "session": sid}
            if reattach:
                req["reattach"] = reattach
            else:
                req["name"] = name
            try:
                resp = _client.request(req)
                self._send_json(200, resp["user"])
            except _client.DaemonError as e:
                # Most daemon errors here are caller-fixable (no such session,
                # reattach to a never-joined id) — surface as 400. Connectivity
                # failures look the same shape; client just sees the message.
                self._send_json(400, {"error": str(e)})
            return

        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            sid = path[len("/api/sessions/"):-len("/messages")]
            body = self._read_json_body()
            if body is None:
                return
            user = body.get("user")
            text = body.get("text")
            if not isinstance(user, str) or not user:
                self._send_json(400, {"error": "`user` is required"})
                return
            if not isinstance(text, str) or not text.strip():
                self._send_json(400, {"error": "`text` must be a non-empty string"})
                return
            try:
                resp = _client.request({
                    "op": _protocol.OP_SEND,
                    "session": sid,
                    "user": user,
                    "text": text,
                })
                self._send_json(201, resp["message"])
            except _client.DaemonError as e:
                self._send_json(400, {"error": str(e)})
            return

        self._send(404, b"not found")

    # --- SSE ---

    def _handle_sse(self, session_id: str) -> None:
        try:
            stream = _get_or_create_stream(session_id)
            q = stream.subscribe()
        except _client.DaemonError as e:
            self._send_json(404, {"error": str(e)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while True:
                try:
                    evt = q.get(timeout=HEARTBEAT_SECS)
                except queue.Empty:
                    # Heartbeat (SSE comment) keeps the connection live.
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                if evt is None:  # tail subprocess died
                    break
                payload = json.dumps(evt).encode("utf-8")
                try:
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            stream.unsubscribe(q)


# ---------- entry point ----------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="converse.gui",
        description="Local HTTP+SSE GUI for converse: pick a session, watch it stream live in a chat-style UI.",
    )
    p.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    p.add_argument("--bind", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    args = p.parse_args(argv)

    server = ThreadingHTTPServer((args.bind, args.port), GUIHandler)
    server.daemon_threads = True
    print(f"converse gui at http://{args.bind}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with _STREAMS_LOCK:
            streams = list(_STREAMS.values())
        for s in streams:
            s._stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
