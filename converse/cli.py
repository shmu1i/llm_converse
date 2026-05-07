"""Command-line interface for `converse`.

Help text is intentionally written to be useful to LLM coding agents
(e.g. Claude Code instances) reading `--help` on the fly.
"""

import argparse
import datetime as dt
import json
import sys

from . import client, daemon, protocol

PROG = "converse"

EPILOG_AGENT_GUIDE = """\
GUIDE FOR LLM CODING AGENTS
---------------------------
This tool lets multiple LLM agent instances talk to each other locally. Use it
when a human operator tells you to coordinate, debate, or hand off work with
another agent. Treat it like a chat room: short, addressed, decision-oriented
turns.

Identifying yourself
  When you join, pass --as with a name that says WHO and WHICH ROLE you are,
  e.g. `--as claude-frontend` or `--as reviewer-A`. The daemon appends a short
  random suffix to keep ids unique. Use the returned user-id verbatim from then
  on; do not invent a new one.

Reading messages (the right way)
  Run `converse tail <session> <user-id>` as a long-lived background process.
  In Claude Code, start it with run_in_background=true and use the Monitor tool
  to receive each new message as a notification. Do not poll with `history` in
  a loop — that wastes turns and may miss messages.

  `tail` first replays the full session history, then streams new messages as
  they arrive (one JSON object per line). `--no-history` skips the replay.

Sending messages
  `converse send <session> <user-id> "<text>"`. Keep messages tight: one idea
  per message, address other agents by their user-id when needed
  (e.g. "@reviewer-a7k2 I disagree because..."). Avoid dumping large code
  blocks; link to a path or paste the minimum diff.

Membership is ephemeral
  Sessions persist forever (SQLite-backed); members do not. When you join an
  existing session, you get a NEW user-id even if you joined it before. The
  active-members list (`converse who`) only includes agents currently tailing.
  Historical messages may be from user-ids that are now offline — do not
  assume those agents are still listening.

Naming sessions
  Always create sessions with a descriptive name: `converse new "frontend
  refactor review"`. Rename later with `converse rename <id> "<new name>"`.
  Names help humans (and you) find old sessions in `converse list`.

Output
  Pass `--json` to any read command for machine-parseable output. Without it,
  output is human-readable lines suitable for showing the user.
"""


# ---------- formatting ----------

def _fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_message(m: dict) -> str:
    return f"[{_fmt_ts(m['created_at'])}] <{m['user_id']}> {m['text']}"


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


# ---------- subcommands ----------

def cmd_new(args: argparse.Namespace) -> int:
    resp = client.request({"op": protocol.OP_NEW, "name": args.name})
    sess = resp["session"]
    if args.json:
        _print_json(sess)
    else:
        label = f' "{sess["name"]}"' if sess.get("name") else ""
        print(f"session: {sess['id']}{label}")
        print("next: converse join", sess["id"], "--as <your-role>")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    resp = client.request({
        "op": protocol.OP_RENAME,
        "session": args.session,
        "name": args.name,
    })
    sess = resp["session"]
    if args.json:
        _print_json(sess)
    else:
        print(f'renamed {sess["id"]} -> "{sess.get("name") or ""}"')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    resp = client.request({"op": protocol.OP_LIST})
    sessions = resp["sessions"]
    if args.json:
        _print_json(sessions)
        return 0
    if not sessions:
        print("(no sessions yet — create one with `converse new \"<name>\"`)")
        return 0
    print(f"{'ID':<10} {'NAME':<28} {'MSGS':>5} {'ACTIVE':>6}  LAST ACTIVITY")
    for s in sessions:
        last = s.get("last_message_at") or s["created_at"]
        active = len(s.get("active_users") or [])
        name = (s.get("name") or "")[:28]
        print(f"{s['id']:<10} {name:<28} {s['message_count']:>5} {active:>6}  {_fmt_ts(last)}")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    resp = client.request({
        "op": protocol.OP_JOIN,
        "session": args.session,
        "name": args.name,
    })
    user = resp["user"]
    if args.json:
        _print_json(user)
    else:
        print(f"joined {user['session_id']} as {user['id']}")
        print("next: converse tail", user["session_id"], user["id"], "(run in background)")
    return 0


def cmd_who(args: argparse.Namespace) -> int:
    resp = client.request({"op": protocol.OP_WHO, "session": args.session})
    users = resp["users"]
    if args.json:
        _print_json(users)
        return 0
    if not users:
        print("(no members)")
        return 0
    for u in users:
        flag = "active" if u["active"] else "offline"
        print(f"{u['id']:<24} {flag:<8}  joined {_fmt_ts(u['joined_at'])}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = args.text if isinstance(args.text, str) else " ".join(args.text)
    if not text.strip():
        print("error: empty message", file=sys.stderr)
        return 2
    resp = client.request({
        "op": protocol.OP_SEND,
        "session": args.session,
        "user": args.user,
        "text": text,
    })
    if args.json:
        _print_json(resp["message"])
    else:
        print(f"sent #{resp['message']['id']}")
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    req = {"op": protocol.OP_TAIL, "session": args.session, "user": args.user}
    if args.no_history:
        req["since"] = None
    elif args.since is not None:
        req["since"] = args.since
    try:
        for obj in client.stream(req):
            if obj.get("attached"):
                if not args.json:
                    sys.stderr.write(f"# attached to {obj['session']} as {obj.get('user')}\n")
                    sys.stderr.flush()
                continue
            if args.json:
                print(json.dumps(obj), flush=True)
            else:
                print(_fmt_message(obj), flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    req = {"op": protocol.OP_HISTORY, "session": args.session}
    if args.since is not None:
        req["since"] = args.since
    if args.limit is not None:
        req["limit"] = args.limit
    resp = client.request(req)
    msgs = resp["messages"]
    if args.json:
        _print_json(msgs)
        return 0
    for m in msgs:
        print(_fmt_message(m))
    return 0


def cmd_stop(_: argparse.Namespace) -> int:
    if client.stop_daemon():
        print("daemon stopped")
        return 0
    print("no running daemon")
    return 0


# ---------- argparse wiring ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Local multi-agent chat. Multiple LLM coding agents (or humans) "
            "join a session by id and exchange messages. The daemon persists "
            "all messages to SQLite and broadcasts new messages live to every "
            "attached `tail` client."
        ),
        epilog=EPILOG_AGENT_GUIDE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = False

    sp = sub.add_parser(
        "new",
        help="create a new session",
        description=(
            "Create a new session. Pass a descriptive name so humans (and you) "
            "can find it later in `converse list`."
        ),
    )
    sp.add_argument("name", nargs="?", default=None, help='descriptive label, e.g. "frontend refactor review"')
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser(
        "rename",
        help="change a session's descriptive name",
    )
    sp.add_argument("session", help="session id")
    sp.add_argument("name", help="new descriptive name (use empty string to clear)")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser(
        "list",
        help="list all sessions (most recent first)",
        description=(
            "Show every session ever created, with message count, active-member "
            "count, and last activity. Use this to find an existing session to "
            "rejoin."
        ),
    )
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser(
        "join",
        help="join an existing session",
        description=(
            "Join a session by id and receive a NEW user identifier. You will "
            "get a fresh id every time you join, even if you joined this "
            "session before — membership is ephemeral and not deduplicated."
        ),
    )
    sp.add_argument("session", help="session id (from `converse list`)")
    sp.add_argument(
        "--as",
        dest="name",
        default=None,
        help="role/name prefix, e.g. `claude-backend`. A random suffix is appended.",
    )
    sp.set_defaults(func=cmd_join)

    sp = sub.add_parser(
        "who",
        help="list members of a session (active and offline)",
        description=(
            "List every user-id that has ever joined this session. `active` "
            "means an agent is currently tailing; `offline` means they joined "
            "before but are not listening right now."
        ),
    )
    sp.add_argument("session", help="session id")
    sp.set_defaults(func=cmd_who)

    sp = sub.add_parser(
        "send",
        help="send a message",
        description=(
            "Send a message to the session as <user>. Other agents tailing the "
            "session will see it immediately."
        ),
    )
    sp.add_argument("session", help="session id")
    sp.add_argument("user", help="your user id (returned by `converse join`)")
    sp.add_argument("text", nargs="+", help="message text")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser(
        "tail",
        help="stream messages live (run as a background process)",
        description=(
            "Attach to a session and stream every message live. Replays full "
            "history first, then streams new messages indefinitely until you "
            "kill the process. Designed to be run as a long-lived background "
            "process so an agent can react to incoming messages without "
            "polling."
        ),
    )
    sp.add_argument("session", help="session id")
    sp.add_argument("user", help="your user id (returned by `converse join`)")
    sp.add_argument("--no-history", action="store_true", help="skip history replay; only stream new messages")
    sp.add_argument("--since", type=int, default=None, help="replay messages with id > SINCE")
    sp.set_defaults(func=cmd_tail)

    sp = sub.add_parser(
        "history",
        help="print message history once and exit",
        description="One-shot history dump. Prefer `tail` for live coordination.",
    )
    sp.add_argument("session", help="session id")
    sp.add_argument("--since", type=int, default=None, help="only messages with id > SINCE")
    sp.add_argument("--limit", type=int, default=None, help="max messages to return")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("stop-daemon", help="stop the running daemon")
    sp.set_defaults(func=cmd_stop)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.daemon:
        daemon.run()
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except client.DaemonError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
