import re
import secrets
import string

ALPHABET = string.ascii_lowercase + string.digits
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")


def short(n: int = 6) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def is_safe_name(s: str) -> bool:
    return bool(SAFE_NAME.match(s))


def make_user_id(name: str | None) -> str:
    suffix = short(4)
    if name and is_safe_name(name):
        return f"{name}-{suffix}"
    return suffix
