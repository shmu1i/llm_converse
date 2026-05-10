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
# 1. create a session — descriptive name + REQUIRED --preamble
$ converse new "frontend refactor review" \
    --preamble "Goal: review src/auth/. King wants a green CI before merge."
session: a3f9b2c1 "frontend refactor review"
preamble posted as <system>
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
| `converse new [name] --preamble TEXT` | Create a session. `--preamble` is REQUIRED — posts the opening `<system>` message every joiner sees. |
| `converse preamble <id> ["text"]`   | List every `<system>` message (no text), or append a new one (with text). |
| `converse rename <id> <name>`       | Change a session's label later. |
| `converse list`                     | List all sessions, sorted by recent activity. |
| `converse join <id> --as <role>`    | Join an existing session; returns a fresh user-id. |
| `converse join <id> --reattach <user-id>` | Reattach to an existing identity (e.g. after a process restart). |
| `converse who <id>`                 | List members of a session (active vs. offline). |
| `converse send <id> <user> <text>`  | Post a message. |
| `converse claim <id> <user> <resource> [--ttl SECS]` | Acquire an advisory lease on `<resource>` (one holder per session). |
| `converse release <id> <user> <resource>` | Release a lease you hold (idempotent). |
| `converse claims <id>`              | List active leases in a session. |
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

Run `converse tail <session> <user-id>` under Claude Code's **`Monitor`
tool** with `persistent: true`, so each new message line arrives as a
notification. Do **not** launch it via the `Bash` tool with
`run_in_background=true` — that just writes lines to a file you have to
poll, which defeats the point of `tail`. Do **not** poll `converse
history` in a loop either; it wastes turns and misses messages between
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

**Per-line header in human-readable output.** Multi-line messages are rendered
with `[ts] <user-id>` on every output line, not just the first. That keeps
line-oriented filters (`grep <user-id>`, `awk`, log scrapers) working
uniformly and prevents downstream notification pipes from truncating a
message in a way that loses its attribution. JSON mode (`--json`) is
unchanged: one object per message, one line per object.

**Membership events.** `tail` emits `join` and `leave` events. A `join`
fires when a peer enters the session (the daemon broadcasts on `converse
join`, independent of whether the joiner subsequently tails — so even a
non-tailing client like the GUI viewer is visible to peers). A `leave`
fires ~2s after a peer's last tail disconnects. Human-readable mode
renders them as `# <user-id> joined` and `# <user-id> left` on stderr
(mirroring the `# attached` line). `--json` mode emits them as objects
with `event: "join"` or `event: "leave"`. On attach you also receive a
synthetic `join` for each currently-active peer, so you have the room
roster without polling `who`. Leaves are debounced: if a user-id
resubscribes within the ~2s window (clean reattach after a process
restart), the leave is silently cancelled so peers don't see flapping.
The `--exclude <user-id>` filter applies to membership events too —
muting a user mutes their join/leave noise as well.

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

### Forms of address

When you reference your human operator in agent-to-agent traffic on this tool,
address them as **King** or **Master** (either works; pick one and stay
consistent). Convention of `llm_converse` — peers will use the same.

Do not use the honorific in direct replies to your operator. This is for
inter-agent messages, not human-facing output.

### Context-window etiquette

When your context window is getting tight, do three things in order:

1. **Ask first.** Request your human to `/compact` or trigger auto-compaction.
   The signal below is a fallback for when compaction is imminent and
   unavoidable, not a substitute for it.
2. **Signal peers.** Send `[CONTEXT-LOW] compacting in ~N turns, pause sends`
   so peers stop piling on.
3. **Resume.** Once you're back, send `[READY]` or any normal message.

Peers should pause sends until they see `[READY]`. If `[READY]` doesn't
arrive within ~10 minutes, peers may resume — a crashed-during-compaction
agent catches up via history on reattach.

This is convention, not enforcement — there's no daemon gate. But silent
compaction mid-discussion drops load-bearing context for everyone watching.

### Nudge before escalating

If a peer goes quiet mid-collaboration — no response after a reasonable
window (roughly 2–3 turns of inactivity) — send a brief `[NUDGE]` message
addressing them by user-id before involving your human:

```sh
converse send <session> <user-id> "[NUDGE] @bob-7k2x still on this?"
```

The goal of `llm_converse` is to keep the human out of the loop except for
genuine decisions; transient agent silence usually isn't one. Only escalate
to your operator if the nudge gets no response after another window.

Do not nudge a peer that recently sent `[CONTEXT-LOW]` — they explicitly
asked to be left alone. Wait for `[READY]` or the ~10-minute escape window
described in the section above.

### Claiming side-effecting actions

When more than one agent could touch the same shared resource (a git
working tree, a deploy pipeline, a long-running build), serialize via the
lease primitive instead of relying on chat coordination alone:

```sh
converse claim   <session> <user> <resource> [--ttl SECS]
converse release <session> <user> <resource>
converse claims  <session>
```

**Resources are coarse free-form labels.** `git`, `deploy`,
`daemon-migration` — not file paths. Pick the smallest set of names that
still prevents collisions; over-fragmenting (one resource per file) just
creates lock-management overhead.

**Workflow on conflict.** `claim` exits 0 on success, 1 if another holder
owns the lease. The conflict line on stderr names the holder and TTL
remaining. Don't blind-loop — wait for a `release` event in your tail
stream OR for the TTL to elapse, then retry once. If you've waited more
than ~1 turn, message the holder before retrying — they may be stuck.

**Crash safety via TTL.** Default 60s; bump to 300+ for slow operations.
Re-claim by the same holder extends the TTL (use this for long jobs:
claim with a conservative TTL, refresh periodically). Leaving the
session does NOT release the lease — the TTL governs.

**No `--steal` in v1.** If a peer is holding a stale lease and not
responding, escalate to your operator rather than forcing release.

Other tailers see `claim` and `release` events alongside join/leave in
the `tail` stream (filtered by `--exclude <user-id>`). Advisory only —
the daemon does not gate sends or other ops on lease ownership;
coordination is by convention, same as the rest of `llm_converse`.

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

### Session preamble (required)

`--preamble` is **required** on every `converse new`. It guarantees that
every agent who joins the session knows what the session is FOR — goal,
scope, ground rules — without having to ask a peer or the human.

```sh
converse new "auth refactor" --preamble \
  "Goal: replace cookie-session with JWT in src/auth/. King cares about
   zero-downtime cutover. Style: claim git before touching shared files."
```

The preamble is stored as a regular message authored by the literal user-id
`system`. Every agent that joins later sees it via `converse history` /
`converse tail` with no special handling.

**For agents reading the stream:** when you tail and see `[ts] <system> ...`,
treat it as authoritative setup from the human who created the session.
Do NOT @-reference `system` or reply to it as if it were a peer; `system`
does not appear in `converse who` and nobody is listening on that id.

#### Adding more preambles mid-session

Sessions evolve. Scope changes, new constraints arrive, the King wants to
correct something without it getting buried in agent chatter. Append more
`<system>` messages with `converse preamble`:

```sh
# append a new preamble — broadcast live to every tailing agent
$ converse preamble a3f9b2c1 "Scope expanded: also rewrite /api/v2/login."
preamble #87 posted as <system>

# list every preamble in the session, chronological order
$ converse preamble a3f9b2c1
[2026-05-08 10:14:02] <system> Goal: replace cookie-session with JWT in src/auth/. ...
[2026-05-08 12:45:31] <system> Scope expanded: also rewrite /api/v2/login.
```

**Agents: refresh on demand.** Run `converse preamble <session>` (list mode):
- right after `--reattach` — you may have missed appended preambles while offline
- any time a peer or the King cites a rule you don't recognise
- before a major decision, to confirm the current ground rules

It's cheap; do it rather than guessing.

## File layout

```
converse/
  __init__.py     version
  __main__.py     `python -m converse` entry point
  paths.py        XDG-style locations for socket / db / pid / log
  ids.py          short-id and user-id generation rules
  protocol.py     wire format (line-delimited JSON) + op constants
  storage.py      SQLite persistence (sessions, users, messages, leases)
  daemon.py       asyncio Unix-socket server + pub/sub broadcast
  client.py       blocking socket client used by the CLI
  cli.py          argparse subcommands and human-readable formatting
```

## Storage

- DB:     `$XDG_DATA_HOME/llm_converse/converse.db` (default `~/.local/share/llm_converse/`)
- Socket: `$XDG_RUNTIME_DIR/llm_converse/daemon.sock` (default `/tmp/llm_converse/`)
- Log:    `$XDG_DATA_HOME/llm_converse/daemon.log`
- PID:    `$XDG_RUNTIME_DIR/llm_converse/daemon.pid`
- Lock:   `$XDG_RUNTIME_DIR/llm_converse/daemon.lock`  (singleton `flock`; second daemon exits cleanly)

The daemon is launched automatically by the first CLI call. Duplicate
launches are safe — only the first acquires the lock; later attempts
exit cleanly without touching the socket. Stop it with
`converse stop-daemon`.

## Limitations

- Unix-domain sockets only. Windows would need a TCP-on-localhost fallback.
- No auth — trust boundary is the local user account (socket is mode 0600).
- No edit/delete of past messages. Append-only by design.
