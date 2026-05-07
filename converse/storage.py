import sqlite3
import time
from pathlib import Path

from . import ids

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    joined_at   REAL NOT NULL,
    PRIMARY KEY (id, session_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS messages_session_idx
    ON messages(session_id, id);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class Ambiguous(Exception):
    pass


class Storage:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def create_session(self, name: str | None = None) -> dict:
        sid = ids.short(8)
        # extremely unlikely collision, but loop defensively
        for _ in range(5):
            try:
                self.conn.execute(
                    "INSERT INTO sessions (id, name, created_at) VALUES (?, ?, ?)",
                    (sid, name, time.time()),
                )
                break
            except sqlite3.IntegrityError:
                sid = ids.short(8)
        return self.get_session(sid)

    def resolve_session(self, prefix: str) -> str:
        """Resolve a session id from an exact match or unambiguous prefix.

        Exact match wins even if other ids share the prefix (git-sha style).
        Raises NotFound for zero matches, Ambiguous for >1 prefix matches.
        """
        if not prefix:
            raise NotFound("no such session: ''")
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (prefix,)
        ).fetchone()
        if row:
            return row["id"]
        rows = self.conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ORDER BY id LIMIT 6",
            (prefix + "%",),
        ).fetchall()
        if not rows:
            raise NotFound(f"no such session: {prefix}")
        if len(rows) > 1:
            ids = ", ".join(r["id"] for r in rows[:5])
            extra = "..." if len(rows) > 5 else ""
            raise Ambiguous(f"ambiguous session prefix {prefix!r}: matches {ids}{extra}")
        return rows[0]["id"]

    def get_session(self, session_id: str) -> dict:
        row = self.conn.execute(
            "SELECT id, name, created_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"no such session: {session_id}")
        return dict(row)

    def rename_session(self, session_id: str, new_name: str | None) -> dict:
        self.get_session(session_id)
        self.conn.execute(
            "UPDATE sessions SET name = ? WHERE id = ?",
            (new_name, session_id),
        )
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                s.id, s.name, s.created_at,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
                (SELECT MAX(created_at) FROM messages m WHERE m.session_id = s.id) AS last_message_at,
                (SELECT COUNT(*) FROM users u WHERE u.session_id = s.id) AS user_count
            FROM sessions s
            ORDER BY COALESCE(
                (SELECT MAX(created_at) FROM messages m WHERE m.session_id = s.id),
                s.created_at
            ) DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def add_user(self, session_id: str, name: str | None = None) -> dict:
        self.get_session(session_id)
        for _ in range(5):
            uid = ids.make_user_id(name)
            try:
                self.conn.execute(
                    "INSERT INTO users (id, session_id, joined_at) VALUES (?, ?, ?)",
                    (uid, session_id, time.time()),
                )
                return {"id": uid, "session_id": session_id}
            except sqlite3.IntegrityError:
                continue
        raise Conflict("could not allocate unique user id")

    def user_exists(self, session_id: str, user_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM users WHERE session_id = ? AND id = ?",
            (session_id, user_id),
        ).fetchone()
        return bool(row)

    def list_users(self, session_id: str) -> list[dict]:
        self.get_session(session_id)
        rows = self.conn.execute(
            "SELECT id, joined_at FROM users WHERE session_id = ? ORDER BY joined_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_message(self, session_id: str, user_id: str, text: str) -> dict:
        if not self.user_exists(session_id, user_id):
            raise NotFound(f"user {user_id} is not a member of session {session_id}")
        ts = time.time()
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, text, ts),
        )
        return {
            "id": cur.lastrowid,
            "session_id": session_id,
            "user_id": user_id,
            "text": text,
            "created_at": ts,
        }

    def history(self, session_id: str, since_id: int | None = None, limit: int | None = None) -> list[dict]:
        self.get_session(session_id)
        sql = "SELECT id, session_id, user_id, text, created_at FROM messages WHERE session_id = ?"
        args: list = [session_id]
        if since_id is not None:
            sql += " AND id > ?"
            args.append(since_id)
        sql += " ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
