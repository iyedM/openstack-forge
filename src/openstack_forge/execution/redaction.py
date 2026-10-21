"""Mask secrets in text and structured data before it is stored, shown or logged.

Redaction is deliberately over-eager: a false positive hides a harmless value, a false
negative leaks a credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "********"

# Key names whose values are secrets, compared case-insensitively as substrings.
SENSITIVE_KEY_PARTS: tuple[str, ...] = ("password", "passwd", "secret", "token")

_KEY = r"[A-Za-z0-9_.-]*(?:password|passwd|secret|token)[A-Za-z0-9_.-]*"

# key = value / key: value / "key": "value"; a quoted value is masked up to its closing
# quote, an unquoted one up to the end of the line.
_ASSIGNMENT_RE = re.compile(
    rf"""(?P<key>["']?{_KEY}["']?\s*[:=][ \t]*)
         (?:(?P<quote>["'])(?P<quoted>(?:\\.|(?!(?P=quote)).)*)(?P=quote)
           |(?P<bare>[^\n]+))""",
    re.IGNORECASE | re.VERBOSE,
)
# --os-password VALUE / --token=VALUE
_FLAG_RE = re.compile(rf"(?P<flag>--{_KEY})(?P<sep>[ \t]+|=)(?P<value>\S+)", re.IGNORECASE)
# scheme://user:password@host
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]*:)(?P<password>[^\s@/]+)(?=@)"
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    """Return ``text`` with secret values replaced by :data:`MASK`."""
    text = _URL_CREDENTIALS_RE.sub(lambda m: f"{m['prefix']}{MASK}", text)
    text = _FLAG_RE.sub(lambda m: f"{m['flag']}{m['sep']}{MASK}", text)
    return _ASSIGNMENT_RE.sub(_mask_assignment, text)


def redact_data(value: Any) -> Any:
    """Recursively redact a JSON-like structure: sensitive keys and every string value."""
    if isinstance(value, Mapping):
        return {
            key: MASK if isinstance(key, str) and is_sensitive_key(key) else redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _mask_assignment(match: re.Match[str]) -> str:
    if match["quote"]:
        return f"{match['key']}{match['quote']}{MASK}{match['quote']}"
    return f"{match['key']}{MASK}"
