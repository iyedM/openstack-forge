"""Mask secrets in text and structured data before it is stored, shown or logged.

Redaction is deliberately over-eager: a false positive hides a harmless value, a false
negative leaks a credential.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "********"

# Minimum length accepted by register_secret() so short or empty strings can never mask
# ordinary words in diffs, logs or error messages by accident.
MIN_REDACTED_SECRET_LENGTH = 8

# Exact secret values registered at runtime (for example when SecretStore loads or creates
# secrets). Any occurrence of a registered value in text passed to redact_text() is
# replaced with MASK regardless of surrounding context.
_REGISTERED_SECRETS: set[str] = set()

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
# SQL: IDENTIFIED BY 'password' or IDENTIFIED BY "password"
_IDENTIFIED_BY_RE = re.compile(
    r"""(?P<prefix>\bIDENTIFIED\s+BY\s+)(?P<quote>['"])(?P<password>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""",
    re.IGNORECASE,
)


def _validate_registered_secret(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Secret value to register must be a string, got {type(value).__name__}")
    if len(value) < MIN_REDACTED_SECRET_LENGTH:
        raise ValueError(
            f"Refusing to register a secret shorter than {MIN_REDACTED_SECRET_LENGTH} characters "
            f"(got length {len(value)})"
        )
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ValueError(
            "Refusing to register a secret containing whitespace or non-printable characters"
        )
    return value


def register_secret(value: str) -> None:
    """Register one exact secret value to be masked by :func:`redact_text`.

    Rejects non-strings, strings shorter than :data:`MIN_REDACTED_SECRET_LENGTH` (8), and
    strings containing whitespace or control characters so common words or line breaks can
    never be redacted by accident.
    """
    _REGISTERED_SECRETS.add(_validate_registered_secret(value))


def register_secrets(values: Iterable[str]) -> None:
    """Register multiple exact secret values atomically."""
    checked = [_validate_registered_secret(value) for value in values]
    _REGISTERED_SECRETS.update(checked)


def clear_registered_secrets() -> None:
    """Remove all registered exact secret values (used by tests for isolation)."""
    _REGISTERED_SECRETS.clear()


def registered_secret_count() -> int:
    """Return the number of distinct exact secret values currently registered."""
    return len(_REGISTERED_SECRETS)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    """Return ``text`` with registered secrets and secret patterns replaced by :data:`MASK`."""
    if _REGISTERED_SECRETS:
        for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, MASK)
    text = _URL_CREDENTIALS_RE.sub(lambda m: f"{m['prefix']}{MASK}", text)
    text = _IDENTIFIED_BY_RE.sub(lambda m: f"{m['prefix']}{m['quote']}{MASK}{m['quote']}", text)
    text = _FLAG_RE.sub(lambda m: f"{m['flag']}{m['sep']}{MASK}", text)
    return _ASSIGNMENT_RE.sub(_mask_assignment, text)


def redact_data(value: Any) -> Any:
    """Recursively redact a JSON-like structure: sensitive keys and every string value."""
    if isinstance(value, Mapping):
        return {
            (redact_text(key) if isinstance(key, str) else key): (
                MASK if isinstance(key, str) and is_sensitive_key(key) else redact_data(item)
            )
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
