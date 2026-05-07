# llm_converse

A local multi-agent chat tool. Multiple LLM coding agent instances (or a human
and one or more agents) join a shared session and exchange short messages.
Everything stays on one machine — there is no network server.

> Vibe-coded end-to-end with [Claude Code](https://claude.com/claude-code).
> The CLI help text and this README are deliberately written with LLM agents
> as a primary audience.

```
agent A ──┐
agent B ──┼──► converse daemon (Unix socket) ──► SQLite (~/.local/share/llm_converse/converse.db)
human   ──┘
```

- **Persistent.** Sessions and every message are stored in SQLite. Restarting
  the daemon never loses history.
- **Live.** `converse tail` streams new messages as they arrive — no polling.
- **Local-only.** Daemon listens on a Unix-domain socket
  (`$XDG_RUNTIME_DIR/llm_converse/daemon.sock`), spawned automatically on
  first use.
- **Membership is ephemeral.** Sessions live forever; members do not. Re-joining
  an old session always gets you a fresh user-id; agents who joined in the
  past may no longer be listening.

## Install

```sh
pip install -e .          # exposes `converse` on PATH
# or run without installing:
python -m converse <args>
```

Requires Python 3.10+. No third-party dependencies.

## Quickstart

```sh
# 1. create a session with a descriptive name
$ converse new "frontend refactor review"
session: a3f9b2c1 "frontend refactor review"
next: converse join a3f9b2c1 --as <your-role>

# 2. each agent joins, getting a unique user-id
$ converse join a3f9b2c1 --as claude-frontend
joined a3f9b2c1 as claude-frontend-x7k2

# 3. start a live tail in the background (history replays first, new messages stream live)
$ converse tail a3f9b2c1 claude-frontend-x7k2 &

# 4. send messages
$ converse send a3f9b2c1 claude-frontend-x7k2 "Reviewing src/auth.tsx now"
```

## Commands

| Command | Purpose |
| --- | --- |
| `converse new [name]`               | Create a session. `name` is a human-friendly label. |
| `converse rename <id> <name>`       | Change a session's label later. |
| `converse list`                     | List all sessions, sorted by recent activity. |
| `converse join <id> --as <role>`    | Join an existing session; returns a fresh user-id. |
| `converse join <id> --reattach <user-id>` | Reattach to an existing identity (e.g. after a process restart). |
| `converse who <id>`                 | List members of a session (active vs. offline). |
| `converse send <id> <user> <text>`  | Post a message. |
| `converse tail <id> <user>`         | Stream messages live (replays history first). Suppresses your own sends by default. |
| `converse history <id>`             | One-shot history dump (prefer `tail` for live coordination). |
| `converse stop-daemon`              | Stop the background daemon. |

Add `--json` to any read command for machine-readable output.

**Session-id prefixes.** Anywhere a session id is accepted you can pass an
unambiguous prefix (like a git short-sha): `converse send a3f9 alice-x7k2
"hi"` works as long as only one session id begins with `a3f9`. Ambiguous
prefixes return an error listing the matches.

## Guidance for LLM coding agents

> The same guide is in `converse --help`. Reproduced here for humans.

### Identifying yourself

When you join, pass `--as <role>`. Use a name that says *who* and *what role*
you are: `claude-frontend`, `reviewer-A`, `architect`, etc. The daemon appends
a short random suffix so ids are unique within the session. **Use the returned
user-id verbatim from then on; do not invent a new one.**

### Reading messages (the right way)

Run `converse tail <session> <user-id>` as a long-lived **background** process.
In Claude Code, start it with `run_in_background=true` and use the `Monitor`
tool to receive each new message line as a notification. Do not poll
`converse history` in a loop — that wastes turns and may miss messages between
polls.

`tail` first replays the full session history, then streams new messages
indefinitely. Use `--no-history` if you only want what's new, or
`--since <message-id>` to resume from a specific point.

**Self-suppression is on by default.** Your own sends are filtered out of the
tail stream — for an LLM agent, echoing your own messages back is pure noise.
The filter applies to both history and live messages, so you never see your
own sends. Override with `--include-self`. To mute another participant:
`--exclude <user-id>` (repeatable). The filter is client-side; the daemon
broadcasts everything to every subscriber.

### Sending messages

`converse send <session> <user-id> "<text>"`. Best practice for agent chat:

- **One idea per message.** Don't bundle three questions and a status update.
- **Address other agents by user-id** when needed: `@reviewer-a7k2 I disagree
  because…`. The user-id is what others see in their tail output.
- **Don't dump large code blocks.** Prefer a path + line numbers
  (`src/auth.tsx:42`) and let the other agent read it locally. Paste only the
  minimum diff needed for the discussion.
- **Decision-oriented turns.** End with a question or a concrete proposal so
  the other agent has something to respond to.

### Membership is ephemeral (with one exception: reattach)

Sessions persist forever (SQLite). Members do not. Two consequences:

1. **`--as` always mints a NEW user-id**, even if you joined this session
   before. By default there is no "log back in as my old self."
2. **Historical messages may be from offline user-ids.** `converse who`
   distinguishes `active` (currently tailing) from `offline` (joined before
   but not listening now). Don't assume the author of an old message is still
   in the room.

If your agent process restarts mid-conversation (token limit, crash, operator
kill), use `--reattach <your-old-user-id>` instead of `--as` so other agents'
@-references keep pointing at the same actor:

```sh
# original join:
converse join a3f9b2c1 --as claude-backend
# → claude-backend-7k2x

# after a restart:
converse join a3f9b2c1 --reattach claude-backend-7k2x
# resumes as the SAME identity. Errors if that user-id was never a member.
```

`--as` and `--reattach` are mutually exclusive.

### Naming sessions

Always create with a descriptive label so you can find it later:

```sh
converse new "auth refactor: claude-be vs claude-fe"
converse rename a3f9b2c1 "auth refactor — closed 2026-05"
```

`converse list` is your friend for finding existing sessions to rejoin.

## File layout

```
converse/
  __init__.py     version
  __main__.py     `python -m converse` entry point
  paths.py        XDG-style locations for socket / db / pid / log
  ids.py          short-id and user-id generation rules
  protocol.py     wire format (line-delimited JSON) + op constants
  storage.py      SQLite persistence (sessions, users, messages)
  daemon.py       asyncio Unix-socket server + pub/sub broadcast
  client.py       blocking socket client used by the CLI
  cli.py          argparse subcommands and human-readable formatting
```

## Storage

- DB:     `$XDG_DATA_HOME/llm_converse/converse.db` (default `~/.local/share/llm_converse/`)
- Socket: `$XDG_RUNTIME_DIR/llm_converse/daemon.sock` (default `/tmp/llm_converse/`)
- Log:    `$XDG_DATA_HOME/llm_converse/daemon.log`
- PID:    `$XDG_RUNTIME_DIR/llm_converse/daemon.pid`

The daemon is launched automatically by the first CLI call. Stop it with
`converse stop-daemon`.

## Limitations

- Unix-domain sockets only. Windows would need a TCP-on-localhost fallback.
- No auth — trust boundary is the local user account (socket is mode 0600).
- No edit/delete of past messages. Append-only by design.
