import asyncio
import contextlib
import logging
import os
import signal
import sys
import time

from . import paths, protocol
from .storage import Ambiguous, Conflict, NotFound, Storage

LEAVE_DEBOUNCE_SECS = 2.0

LEASE_TTL_DEFAULT = 60.0
LEASE_TTL_MAX = 3600.0

log = logging.getLogger("converse.daemon")


class Daemon:
    def __init__(self) -> None:
        self.storage = Storage(paths.db_path())
        # session_id -> set[Subscriber]
        self.subscribers: dict[str, set["Subscriber"]] = {}
        # (session_id, user_id) -> pending leave task. Cancelled when the
        # same user-id resubscribes within LEAVE_DEBOUNCE_SECS, so a clean
        # process restart (--reattach) doesn't broadcast spurious leave/join.
        self.pending_leaves: dict[tuple[str, str], asyncio.Task] = {}

    # ----- subscriber bookkeeping -----

    def _subscribe(self, sub: "Subscriber") -> bool:
        """Add subscriber. Returns True if this is the first active subscriber for the user-id."""
        was_active = bool(sub.user_id) and sub.user_id in self.active_users(sub.session_id)
        self.subscribers.setdefault(sub.session_id, set()).add(sub)
        return bool(sub.user_id) and not was_active

    def _unsubscribe(self, sub: "Subscriber") -> bool:
        """Remove subscriber. Returns True if this was the last active subscriber for the user-id."""
        bucket = self.subscribers.get(sub.session_id)
        if bucket:
            bucket.discard(sub)
            if not bucket:
                self.subscribers.pop(sub.session_id, None)
        return bool(sub.user_id) and sub.user_id not in self.active_users(sub.session_id)

    def active_users(self, session_id: str) -> set[str]:
        return {s.user_id for s in self.subscribers.get(session_id, ()) if s.user_id}

    async def _broadcast(self, session_id: str, message: dict) -> None:
        for sub in list(self.subscribers.get(session_id, ())):
            await sub.queue.put(message)

    async def _broadcast_to_others(
        self, session_id: str, exclude_sub: "Subscriber", message: dict,
    ) -> None:
        for sub in list(self.subscribers.get(session_id, ())):
            if sub is exclude_sub:
                continue
            await sub.queue.put(message)

    async def _announce_join(self, session_id: str, user_id: str) -> None:
        """Broadcast a join event for user_id to peers if it's not already active.

        Called from OP_JOIN so peers see joins even when the joining client never
        tails (e.g. the GUI human-join flow). Idempotent for reattach-while-active
        and silent for clean reattach within the leave debounce window.
        """
        if user_id in self.active_users(session_id):
            return
        key = (session_id, user_id)
        pending = self.pending_leaves.pop(key, None)
        if pending is not None and not pending.done():
            pending.cancel()
            return
        await self._broadcast(session_id, {
            "event": "join",
            "session": session_id,
            "user_id": user_id,
            "ts": time.time(),
        })

    async def _debounced_leave(
        self, session_id: str, user_id: str, key: tuple[str, str],
    ) -> None:
        try:
            await asyncio.sleep(LEAVE_DEBOUNCE_SECS)
        except asyncio.CancelledError:
            return
        self.pending_leaves.pop(key, None)
        if user_id in self.active_users(session_id):
            return
        await self._broadcast(session_id, {
            "event": "leave",
            "session": session_id,
            "user_id": user_id,
            "ts": time.time(),
        })

    # ----- request handling -----

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = protocol.decode(line)
            except Exception as e:
                await self._send(writer, {"error": f"bad request: {e}"})
                return
            op = req.get("op")
            try:
                if op == protocol.OP_PING:
                    await self._send(writer, {"ok": True})
                elif op == protocol.OP_NEW:
                    sess = self.storage.create_session(req.get("name"))
                    await self._send(writer, {"session": sess})
                elif op == protocol.OP_RENAME:
                    sid = self.storage.resolve_session(req["session"])
                    sess = self.storage.rename_session(sid, req.get("name"))
                    await self._send(writer, {"session": sess})
                elif op == protocol.OP_LIST:
                    sessions = self.storage.list_sessions()
                    for s in sessions:
                        s["active_users"] = sorted(self.active_users(s["id"]))
                    await self._send(writer, {"sessions": sessions})
                elif op == protocol.OP_GET:
                    sid = self.storage.resolve_session(req["session"])
                    sess = self.storage.get_session(sid)
                    sess["active_users"] = sorted(self.active_users(sid))
                    await self._send(writer, {"session": sess})
                elif op == protocol.OP_JOIN:
                    sid = self.storage.resolve_session(req["session"])
                    reattach = req.get("reattach")
                    if reattach:
                        if not self.storage.user_exists(sid, reattach):
                            raise NotFound(
                                f"user {reattach} does not exist in session {sid} "
                                "(use --as <role> to create a new identity instead)"
                            )
                        await self._send(writer, {"user": {
                            "id": reattach, "session_id": sid, "reattached": True,
                        }})
                        await self._announce_join(sid, reattach)
                    else:
                        user = self.storage.add_user(sid, req.get("name"))
                        await self._send(writer, {"user": user})
                        await self._announce_join(sid, user["id"])
                elif op == protocol.OP_WHO:
                    sid = self.storage.resolve_session(req["session"])
                    users = self.storage.list_users(sid)
                    active = self.active_users(sid)
                    for u in users:
                        u["active"] = u["id"] in active
                    await self._send(writer, {"users": users})
                elif op == protocol.OP_SEND:
                    sid = self.storage.resolve_session(req["session"])
                    msg = self.storage.add_message(sid, req["user"], req["text"])
                    await self._send(writer, {"ok": True, "message": msg})
                    await self._broadcast(sid, msg)
                elif op == protocol.OP_HISTORY:
                    sid = self.storage.resolve_session(req["session"])
                    msgs = self.storage.history(
                        sid,
                        since_id=req.get("since"),
                        limit=req.get("limit"),
                    )
                    await self._send(writer, {"messages": msgs})
                elif op == protocol.OP_LEASE_ACQUIRE:
                    sid = self.storage.resolve_session(req["session"])
                    user_id = req["user"]
                    resource = req["resource"]
                    ttl_raw = req.get("ttl")
                    ttl = LEASE_TTL_DEFAULT if ttl_raw is None else float(ttl_raw)
                    if ttl <= 0 or ttl > LEASE_TTL_MAX:
                        await self._send(writer, {
                            "error": f"ttl out of range (0, {LEASE_TTL_MAX}]",
                        })
                    else:
                        status, lease = self.storage.acquire_lease(sid, user_id, resource, ttl)
                        await self._send(writer, {
                            "acquired": status == "acquired",
                            "lease": lease,
                        })
                        if status == "acquired":
                            await self._broadcast(sid, {
                                "event": "claim",
                                "session": sid,
                                "user_id": user_id,
                                "resource": resource,
                                "expires_at": lease["expires_at"],
                                "ttl": ttl,
                                "ts": time.time(),
                            })
                elif op == protocol.OP_LEASE_RELEASE:
                    sid = self.storage.resolve_session(req["session"])
                    user_id = req["user"]
                    resource = req["resource"]
                    released = self.storage.release_lease(sid, user_id, resource)
                    await self._send(writer, {"released": released})
                    if released:
                        await self._broadcast(sid, {
                            "event": "release",
                            "session": sid,
                            "user_id": user_id,
                            "resource": resource,
                            "ts": time.time(),
                        })
                elif op == protocol.OP_LEASES:
                    sid = self.storage.resolve_session(req["session"])
                    leases = self.storage.list_leases(sid)
                    await self._send(writer, {"leases": leases})
                elif op == protocol.OP_TAIL:
                    await self._handle_tail(reader, writer, req)
                else:
                    await self._send(writer, {"error": f"unknown op: {op!r}"})
            except NotFound as e:
                await self._send(writer, {"error": str(e), "kind": "not_found"})
            except Ambiguous as e:
                await self._send(writer, {"error": str(e), "kind": "ambiguous"})
            except Conflict as e:
                await self._send(writer, {"error": str(e), "kind": "conflict"})
            except KeyError as e:
                await self._send(writer, {"error": f"missing field: {e.args[0]}"})
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("handler error")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle_tail(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req: dict,
    ) -> None:
        session_id = self.storage.resolve_session(req["session"])
        user_id = req.get("user")
        if user_id and not self.storage.user_exists(session_id, user_id):
            await self._send(writer, {"error": f"user {user_id} is not a member of session {session_id}", "kind": "not_found"})
            return

        sub = Subscriber(session_id=session_id, user_id=user_id or "")
        fresh = self._subscribe(sub)
        # announce attached
        await self._send(writer, {"attached": True, "session": session_id, "user": user_id})

        # synthetic join events for currently-active peers, so the new
        # subscriber has the room roster without needing to call `who`.
        now = time.time()
        for peer_uid in sorted(self.active_users(session_id)):
            if peer_uid != (user_id or ""):
                await self._send(writer, {
                    "event": "join",
                    "session": session_id,
                    "user_id": peer_uid,
                    "ts": now,
                })

        # Cancel any pending leave-debounce for our user-id (silent reattach
        # within LEAVE_DEBOUNCE_SECS). The join broadcast itself lives in
        # OP_JOIN (_announce_join) — tail-time announcement would double-
        # broadcast in the common join-then-tail flow. Cancellation has to
        # stay here too so a tail-only reattach (e.g. `converse tail` after
        # a process restart, no fresh OP_JOIN) doesn't emit a stale leave.
        if fresh and user_id:
            pending = self.pending_leaves.pop((session_id, user_id), None)
            if pending is not None and not pending.done():
                pending.cancel()

        # replay history unless since was set explicitly to skip
        since = req.get("since", 0)
        if since is not None:
            for m in self.storage.history(session_id, since_id=since):
                await self._send(writer, m)

        # detect client disconnect
        async def watch_disconnect() -> None:
            try:
                while True:
                    chunk = await reader.read(1024)
                    if not chunk:
                        break
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                await sub.queue.put(None)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            while True:
                msg = await sub.queue.get()
                if msg is None:
                    break
                await self._send(writer, msg)
        finally:
            last = self._unsubscribe(sub)
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
            if last and user_id:
                key = (session_id, user_id)
                prev = self.pending_leaves.get(key)
                if prev is not None and not prev.done():
                    prev.cancel()
                self.pending_leaves[key] = asyncio.create_task(
                    self._debounced_leave(session_id, user_id, key)
                )

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(protocol.encode(obj))
        await writer.drain()


class Subscriber:
    __slots__ = ("session_id", "user_id", "queue")

    def __init__(self, session_id: str, user_id: str) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.queue: asyncio.Queue = asyncio.Queue()


async def serve() -> None:
    sock = paths.socket_path()
    if sock.exists():
        sock.unlink()
    daemon = Daemon()
    server = await asyncio.start_unix_server(daemon.handle, path=str(sock))
    os.chmod(sock, 0o600)
    log.info("daemon listening on %s", sock)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))

    try:
        async with server:
            await stop
    finally:
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        log.info("daemon stopped")


def run() -> None:
    logging.basicConfig(
        filename=str(paths.log_path()),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # write pid
    pid = paths.pid_path()
    try:
        pid.write_text(str(os.getpid()))
    except OSError:
        pass
    try:
        asyncio.run(serve())
    finally:
        with contextlib.suppress(FileNotFoundError):
            pid.unlink()


if __name__ == "__main__":
    run()
    sys.exit(0)
