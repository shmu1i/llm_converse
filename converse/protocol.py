"""Line-delimited JSON request/response protocol over a Unix socket.

Every client request is a single JSON object on one line. The daemon replies
with one or more JSON objects, one per line, then closes the connection
(except for `tail`, which streams indefinitely until the client disconnects).
"""

import json
from typing import Any


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line.decode("utf-8"))


# Operation names
OP_NEW = "new"
OP_RENAME = "rename"
OP_LIST = "list"
OP_GET = "get"
OP_JOIN = "join"
OP_WHO = "who"
OP_SEND = "send"
OP_TAIL = "tail"
OP_HISTORY = "history"
OP_PING = "ping"
