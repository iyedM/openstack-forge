"""Idempotent text-file edits: managed blocks, INI keys and commenting out lines.

All primitives take a target path (e.g. ``/etc/hosts``) that is resolved under an
injectable root directory, return a :class:`FileChange` with a redacted unified diff,
back the file up to ``<file>.orig`` before its first modification, and write
atomically. Calling them again with the same arguments changes and writes nothing.

Files must be UTF-8 with LF line endings; files containing CR are refused.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from openstack_forge.execution.errors import FileFormatError
from openstack_forge.execution.fsutil import (
    atomic_write_text,
    backup_once,
    file_mode,
    read_text_file,
    resolve_target,
    unified_diff,
)

DEFAULT_FILE_MODE = 0o644
DISABLED_LINE_PREFIX = "# openstack-forge disabled: "

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
# An active "key = value" line, or a bare "key" flag line (as used in MariaDB files).
_ACTIVE_KEY_RE = re.compile(r"^\s*(?P<key>[^#;\[\s=][^=]*?)\s*(?:=|$)")


@dataclass(frozen=True)
class FileChange:
    """Outcome of a file primitive. ``diff`` is redacted and empty when nothing changed."""

    path: str
    changed: bool
    diff: str
    backup: str | None = None
    created: bool = False


def managed_block(
    root: Path,
    target: str | PurePosixPath,
    name: str,
    content: str,
    *,
    comment: str = "#",
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Insert or replace the block between ``<comment> BEGIN <name>`` and ``... END <name>``.

    Everything outside the markers is preserved. A missing block is appended at the end,
    and a missing file is created. Duplicate, unpaired or misordered markers are refused.
    """
    edit = _managed_block_editor(name, content, comment=comment)
    return _edit_file(root, target, edit, dry_run=dry_run, new_file_mode=new_file_mode)


def _managed_block_editor(
    name: str,
    content: str,
    *,
    comment: str = "#",
) -> Callable[[list[str], str], list[str]]:
    _require_single_line("block name", name)
    if "\x00" in content:
        raise FileFormatError("Block content contains NUL bytes")
    begin, end = f"{comment} BEGIN {name}", f"{comment} END {name}"
    body = content.split("\n")
    if content.endswith("\n"):
        body.pop()
    if any("\r" in line for line in body):
        raise FileFormatError("Block content contains carriage returns")
    if any(line.strip() in (begin, end) for line in body):
        raise FileFormatError(f"Block content must not contain the {name!r} markers")
    block = [begin, *body, end] if content else [begin, end]

    def edit(lines: list[str], display: str) -> list[str]:
        begins = [i for i, line in enumerate(lines) if line.strip() == begin]
        ends = [i for i, line in enumerate(lines) if line.strip() == end]
        if not begins and not ends:
            return [*lines, *block]
        if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
            raise FileFormatError(
                f"{display} has broken {name!r} markers "
                f"({len(begins)} BEGIN, {len(ends)} END); fix them by hand"
            )
        return [*lines[: begins[0]], *block, *lines[ends[0] + 1 :]]

    return edit


def comment_out_lines(
    root: Path,
    target: str | PurePosixPath,
    pattern: str | re.Pattern[str],
    *,
    prefix: str = DISABLED_LINE_PREFIX,
    exclude_block: str | None = None,
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Comment out active lines matching ``pattern`` by prepending ``prefix``.

    Lines that already start with ``prefix`` or are already ``#`` comments are not
    modified, so repeated calls are idempotent. Lines inside the ``exclude_block``
    managed block (markers included) are never touched. A missing target file is left
    absent.
    """
    edit = _comment_out_editor(pattern, prefix=prefix, exclude_block=exclude_block)
    return _edit_file(
        root,
        target,
        edit,
        dry_run=dry_run,
        new_file_mode=new_file_mode,
        create_if_missing=False,
    )


def _comment_out_editor(
    pattern: str | re.Pattern[str],
    *,
    prefix: str = DISABLED_LINE_PREFIX,
    exclude_block: str | None = None,
) -> Callable[[list[str], str], list[str]]:
    _validate_comment_prefix(prefix)
    regex = _compile_line_pattern(pattern)
    if exclude_block is not None:
        _require_single_line("block name", exclude_block)

    def edit(lines: list[str], _display: str) -> list[str]:
        protected = _block_line_indexes(lines, exclude_block) if exclude_block else set()
        result: list[str] = []
        for index, line in enumerate(lines):
            if index in protected or line.startswith(prefix) or line.lstrip().startswith("#"):
                result.append(line)
            elif regex.search(line):
                result.append(f"{prefix}{line}")
            else:
                result.append(line)
        return result

    return edit


def disable_lines_and_set_block(
    root: Path,
    target: str | PurePosixPath,
    pattern: str | re.Pattern[str],
    name: str,
    content: str,
    *,
    comment: str = "#",
    prefix: str = DISABLED_LINE_PREFIX,
    dry_run: bool = False,
) -> FileChange:
    """Comment out active lines matching ``pattern`` and set a managed block, atomically.

    This is :func:`comment_out_lines` (with ``exclude_block=name``) followed by
    :func:`managed_block`, done in a single edit: one write, one ``.orig`` backup, one
    diff, and no intermediate state where the old lines are disabled but the block is not
    there yet. The file must already exist; a missing file is left absent (unchanged).
    """
    disable = _comment_out_editor(pattern, prefix=prefix, exclude_block=name)
    block = _managed_block_editor(name, content, comment=comment)

    def edit(lines: list[str], display: str) -> list[str]:
        return block(disable(lines, display), display)

    return _edit_file(
        root,
        target,
        edit,
        dry_run=dry_run,
        new_file_mode=DEFAULT_FILE_MODE,
        create_if_missing=False,
    )


def uncomment_lines(
    root: Path,
    target: str | PurePosixPath,
    pattern: str | re.Pattern[str] | None = None,
    *,
    prefix: str = DISABLED_LINE_PREFIX,
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Restore lines previously commented out with :func:`comment_out_lines`.

    When ``pattern`` is given, only disabled lines whose uncommented content matches
    ``pattern`` are restored; when ``pattern`` is ``None``, every line starting with
    ``prefix`` is restored. A missing target file is left absent.
    """
    edit = _uncomment_editor(pattern, prefix=prefix)
    return _edit_file(
        root,
        target,
        edit,
        dry_run=dry_run,
        new_file_mode=new_file_mode,
        create_if_missing=False,
    )


def _uncomment_editor(
    pattern: str | re.Pattern[str] | None = None,
    *,
    prefix: str = DISABLED_LINE_PREFIX,
) -> Callable[[list[str], str], list[str]]:
    _validate_comment_prefix(prefix)
    regex = _compile_line_pattern(pattern) if pattern is not None else None

    def edit(lines: list[str], _display: str) -> list[str]:
        result: list[str] = []
        for line in lines:
            if line.startswith(prefix):
                restored = line[len(prefix) :]
                if regex is None or regex.search(restored):
                    result.append(restored)
                    continue
            result.append(line)
        return result

    return edit


def _block_line_indexes(lines: list[str], name: str, comment: str = "#") -> set[int]:
    """Indexes of the lines of the ``name`` managed block, markers included."""
    begin, end = f"{comment} BEGIN {name}", f"{comment} END {name}"
    protected: set[int] = set()
    inside = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == begin:
            inside = True
        if inside:
            protected.add(index)
        if stripped == end:
            inside = False
    return protected


def ini_set(
    root: Path,
    target: str | PurePosixPath,
    section: str,
    key: str,
    value: str,
    *,
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Set ``key = value`` in ``[section]`` without reformatting the rest of the file.

    In order of preference: replace every active ``key`` line in the section (so no
    duplicate can override the value), else replace the first commented default
    (``#key = ...``), else append after the section's last non-blank line, else append a
    new section. Comments, blank lines and ordering are kept.

    To set several keys in one file, use :func:`ini_set_many`: separate calls would each
    write (and back up) the file, and dry runs would not see each other's edits.
    """
    edit = _ini_set_editor(section, key, value)
    return _edit_file(root, target, edit, dry_run=dry_run, new_file_mode=new_file_mode)


def ini_set_many(
    root: Path,
    target: str | PurePosixPath,
    settings: Iterable[tuple[str, str, str]],
    *,
    comment_out: Iterable[tuple[str, str]] = (),
    prefix: str = DISABLED_LINE_PREFIX,
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Apply ``(section, key, value)`` settings in order, in a single edit of ``target``.

    When ``comment_out`` is given, each ``(section, key)`` pair has its active occurrences
    in ``[section]`` commented out with ``prefix`` before ``settings`` are applied, in the
    same single atomic write and unified diff.

    Each setting follows the rules of :func:`ini_set`. All settings are validated before
    anything is read or written. The result has one combined redacted diff; a file that
    did not exist is created (``created=True``) with no ``.orig`` backup; an existing file
    that changes gets one ``<file>.orig`` backup of its content before this call; when
    every key already has its value, nothing is written. The parent directory must exist.
    """
    edit = _ini_set_many_editor(settings, comment_out=comment_out, prefix=prefix)
    return _edit_file(root, target, edit, dry_run=dry_run, new_file_mode=new_file_mode)


def ini_comment_out_keys(
    root: Path,
    target: str | PurePosixPath,
    keys: Iterable[tuple[str, str]],
    *,
    prefix: str = DISABLED_LINE_PREFIX,
    dry_run: bool = False,
    new_file_mode: int = DEFAULT_FILE_MODE,
) -> FileChange:
    """Comment out active ``key`` lines inside ``[section]`` for each ``(section, key)``.

    Lines already commented out (starting with ``#`` or ``;``) are not touched, so repeated
    calls are idempotent. A missing target file is left absent.
    """
    editors = _build_comment_out_key_editors(keys, prefix=prefix)
    if not editors:
        raise ValueError("ini_comment_out_keys needs at least one (section, key) pair")

    def edit(lines: list[str], display: str) -> list[str]:
        for editor in editors:
            lines = editor(lines, display)
        return lines

    return _edit_file(
        root,
        target,
        edit,
        dry_run=dry_run,
        new_file_mode=new_file_mode,
        create_if_missing=False,
    )


def _build_comment_out_key_editors(
    keys: Iterable[tuple[str, str]],
    *,
    prefix: str = DISABLED_LINE_PREFIX,
) -> list[Callable[[list[str], str], list[str]]]:
    _validate_comment_prefix(prefix)
    editors: list[Callable[[list[str], str], list[str]]] = []
    for item in keys:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"Each entry must be a (section, key) tuple: {item!r}")
        if not all(isinstance(part, str) for part in item):
            raise ValueError(f"Section and key must be strings: {item!r}")
        editors.append(_ini_comment_out_key_editor(*item, prefix=prefix))
    return editors


def _ini_comment_out_key_editor(
    section: str,
    key: str,
    *,
    prefix: str = DISABLED_LINE_PREFIX,
) -> Callable[[list[str], str], list[str]]:
    """Pure line editor commenting out active ``key`` lines in ``[section]``."""
    _validate_comment_prefix(prefix)
    _require_single_line("section", section)
    _require_single_line("key", key)
    if "]" in section or "[" in section:
        raise ValueError(f"Invalid section name {section!r}")
    if key != key.strip() or "=" in key or key[:1] in ("#", ";", "["):
        raise ValueError(f"Invalid key {key!r}")

    def edit(lines: list[str], display: str) -> list[str]:
        spans = _section_spans(lines, section)
        if not spans:
            return lines
        active = {
            index
            for start, stop in spans
            for index in range(start, stop)
            if _active_key(lines[index]) == key
        }
        if not active:
            return lines
        for index in sorted(active):
            _refuse_continuation(lines, index, display, key)
        return [f"{prefix}{line}" if index in active else line for index, line in enumerate(lines)]

    return edit


def _ini_set_many_editor(
    settings: Iterable[tuple[str, str, str]],
    *,
    comment_out: Iterable[tuple[str, str]] = (),
    prefix: str = DISABLED_LINE_PREFIX,
) -> Callable[[list[str], str], list[str]]:
    """Compose comment-out editors and :func:`_ini_set_editor` for every setting."""
    comment_editors = _build_comment_out_key_editors(comment_out, prefix=prefix)
    editors: list[Callable[[list[str], str], list[str]]] = []
    for setting in settings:
        if not isinstance(setting, tuple) or len(setting) != 3:
            raise ValueError(f"Each setting must be a (section, key, value) tuple: {setting!r}")
        if not all(isinstance(part, str) for part in setting):
            raise ValueError(f"Section, key and value must be strings: {setting!r}")
        editors.append(_ini_set_editor(*setting))
    if not editors:
        raise ValueError("ini_set_many needs at least one setting")
    all_editors = [*comment_editors, *editors]

    def edit(lines: list[str], display: str) -> list[str]:
        for editor in all_editors:
            lines = editor(lines, display)
        return lines

    return edit


def _ini_set_editor(section: str, key: str, value: str) -> Callable[[list[str], str], list[str]]:
    """Pure line editor setting ``key = value`` in ``[section]`` (see :func:`ini_set`)."""
    _require_single_line("section", section)
    _require_single_line("key", key)
    _require_single_line("value", value, allow_empty=True)
    if "]" in section or "[" in section:
        raise ValueError(f"Invalid section name {section!r}")
    if key != key.strip() or "=" in key or key[:1] in ("#", ";", "["):
        raise ValueError(f"Invalid key {key!r}")
    setting = f"{key} = {value}"
    commented_re = re.compile(rf"^\s*[#;]+\s*{re.escape(key)}\s*=")

    def edit(lines: list[str], display: str) -> list[str]:
        spans = _section_spans(lines, section)
        if not spans:
            separator = [""] if lines and lines[-1].strip() else []
            return [*lines, *separator, f"[{section}]", setting]
        active = [
            index
            for start, stop in spans
            for index in range(start, stop)
            if _active_key(lines[index]) == key
        ]
        if active:
            for index in active:
                _refuse_continuation(lines, index, display, key)
            return [setting if index in active else line for index, line in enumerate(lines)]
        commented = [
            index
            for start, stop in spans
            for index in range(start, stop)
            if commented_re.match(lines[index])
        ]
        if commented:
            return [*lines[: commented[0]], setting, *lines[commented[0] + 1 :]]
        header, stop = spans[-1][0] - 1, spans[-1][1]
        last = max([i for i in range(header + 1, stop) if lines[i].strip()], default=header)
        return [*lines[: last + 1], setting, *lines[last + 1 :]]

    return edit


def _edit_file(
    root: Path,
    target: str | PurePosixPath,
    edit: Callable[[list[str], str], list[str]],
    *,
    dry_run: bool,
    new_file_mode: int,
    create_if_missing: bool = True,
) -> FileChange:
    path, display = resolve_target(root, target)
    old = read_text_file(path, display)
    if old is None and not create_if_missing:
        return FileChange(path=display, changed=False, diff="")
    created = old is None
    new = apply_line_edit(old or "", display, edit)
    if new == old:
        return FileChange(path=display, changed=False, diff="")

    diff = unified_diff(old or "", new, display)
    if dry_run:
        return FileChange(path=display, changed=True, diff=diff, created=created)
    backup = backup_once(path)
    atomic_write_text(path, new, mode=file_mode(path, new_file_mode))
    return FileChange(
        path=display,
        changed=True,
        diff=diff,
        backup=f"{display}.orig" if backup else None,
        created=created,
    )


def apply_line_edit(
    text: str,
    display: str,
    edit: Callable[[list[str], str], list[str]],
) -> str:
    """Apply a line-list transformation to ``text`` while preserving newline conventions."""
    lines, trailing_newline = _split_lines(text)
    new_lines = edit(lines, display)
    # Appending past the end always yields a terminated file; otherwise keep the original.
    if len(new_lines) > len(lines) and new_lines[: len(lines)] == lines:
        trailing_newline = True
    return "\n".join(new_lines) + ("\n" if trailing_newline and new_lines else "")


def _split_lines(text: str) -> tuple[list[str], bool]:
    if not text:
        return [], True
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
        return lines, True
    return lines, False


def _section_spans(lines: list[str], section: str) -> list[tuple[int, int]]:
    """(first body line, end) for every ``[section]`` header; repeated sections merge."""
    headers = [
        (index, match["name"].strip())
        for index, line in enumerate(lines)
        if (match := _SECTION_RE.match(line))
    ]
    spans = []
    for position, (index, name) in enumerate(headers):
        if name == section:
            stop = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
            spans.append((index + 1, stop))
    return spans


def _active_key(line: str) -> str | None:
    if _SECTION_RE.match(line):
        return None
    match = _ACTIVE_KEY_RE.match(line)
    return match["key"].strip() if match else None


def _refuse_continuation(lines: list[str], index: int, display: str, key: str) -> None:
    following = lines[index + 1] if index + 1 < len(lines) else ""
    if following[:1] in (" ", "\t") and following.strip() and following.lstrip()[0] not in "#;":
        raise FileFormatError(
            f"{display}: {key!r} has a multi-line value (line {index + 2}); edit it by hand"
        )


def _validate_comment_prefix(prefix: str) -> None:
    _require_single_line("prefix", prefix)
    if not prefix.lstrip().startswith("#"):
        raise ValueError(f"The prefix must start with '#', got {prefix!r}")


def _compile_line_pattern(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    if isinstance(pattern, str):
        _require_single_line("pattern", pattern)
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from exc
    _require_single_line("pattern", pattern.pattern)
    return pattern


def _require_single_line(label: str, text: str, *, allow_empty: bool = False) -> None:
    if not allow_empty and not text.strip():
        raise ValueError(f"The {label} must not be empty")
    if "\x00" in text:
        raise ValueError(f"The {label} must not contain NUL bytes")
    if "\n" in text or "\r" in text:
        raise ValueError(f"The {label} must be a single line")
