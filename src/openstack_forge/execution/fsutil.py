"""Filesystem helpers that confine every write to a root directory.

A *target path* is the path on the managed system (``/etc/hosts``). It is always mapped
under an explicit *root* directory, which is ``/`` only when a caller deliberately says
so; the tests always use a temporary directory.
"""

from __future__ import annotations

import difflib
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from openstack_forge.execution.errors import FileFormatError, RootEscapeError, UnsafePathError
from openstack_forge.execution.redaction import redact_text

BACKUP_SUFFIX = ".orig"


def resolve_target(root: Path, target: str | PurePosixPath) -> tuple[Path, str]:
    """Map ``target`` under ``root``; return (real filesystem path, display path).

    ``..`` components are rejected outright, and symlinks along the way may not lead
    outside ``root``. The display path is the normalised target, e.g. ``/etc/hosts``.
    """
    try:
        root_dir = Path(root).resolve(strict=True)
    except FileNotFoundError as exc:
        raise RootEscapeError(f"Root {root} does not exist") from exc
    if not root_dir.is_dir():
        raise RootEscapeError(f"Root {root} is not a directory")
    pure = PurePosixPath(target)
    parts = pure.parts[1:] if pure.is_absolute() else pure.parts
    if not parts:
        raise RootEscapeError(f"Target path {str(target)!r} does not name a file")
    if ".." in parts:
        raise RootEscapeError(f"Target path {str(target)!r} contains '..'")
    resolved = root_dir.joinpath(*parts).resolve(strict=False)
    if not resolved.is_relative_to(root_dir):
        raise RootEscapeError(f"Target path {str(target)!r} resolves outside the root directory")
    return root_dir.joinpath(*parts), "/" + "/".join(parts)


def _has_symlink_component(path: Path) -> bool:
    """True if ``path`` or its immediate parent directory is a symbolic link."""
    return os.path.islink(path) or os.path.islink(path.parent)


def read_text_file(path: Path, display: str) -> str | None:
    """Read a UTF-8, LF-only text file; return None if it does not exist."""
    if _has_symlink_component(path) or path != path.resolve(strict=False):
        raise UnsafePathError(f"Refusing to modify {display}: it is a symlink")
    if not path.exists():
        if not path.parent.is_dir():
            raise UnsafePathError(f"Cannot create {display}: its parent directory does not exist")
        return None
    if not path.is_file():
        raise UnsafePathError(f"Refusing to modify {display}: it is not a regular file")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileFormatError(f"{display} is not valid UTF-8 text") from exc
    if "\x00" in text:
        raise FileFormatError(f"{display} contains NUL bytes; binary files are not supported")
    if "\r" in text:
        raise FileFormatError(
            f"{display} contains carriage returns (CRLF); only LF line endings are supported"
        )
    return text


def backup_once(path: Path) -> Path | None:
    """Copy ``path`` to ``<path>.orig`` unless that backup already exists.

    The copy is written to a temporary file and hard-linked into place, so the backup
    either appears complete or not at all, and an existing ``.orig`` is never replaced.
    Returns the backup path if this call created it.
    """
    if _has_symlink_component(path):
        raise UnsafePathError(f"Refusing to back up {path}: it is a symlink")
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if os.path.islink(backup):
        raise UnsafePathError(f"Refusing to use backup {backup}: it is a symlink")
    if os.path.lexists(backup) or not path.exists():
        return None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".orig-tmp", dir=path.parent)
    os.close(fd)
    try:
        shutil.copy2(path, temporary)
        try:
            os.link(temporary, backup)
        except FileExistsError:
            return None
    finally:
        os.unlink(temporary)
    _fsync_directory(path.parent)
    return backup


def atomic_write_text(path: Path, text: str, *, mode: int) -> None:
    """Replace ``path`` with ``text`` via a temporary file in the same directory.

    ``mode`` sets the permission bits; owner and group (uid/gid) of an existing file are
    preserved when the process is allowed to keep them, otherwise the write is refused.
    """
    if _has_symlink_component(path):
        raise UnsafePathError(f"Refusing to write {path}: it is a symlink")
    existing = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if existing is not None:
            _keep_ownership(temporary, existing, path)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise
    _fsync_directory(path.parent)


def file_mode(path: Path, default: int) -> int:
    """Permission bits of ``path``, or ``default`` if it does not exist."""
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else default


def unified_diff(old: str, new: str, display: str) -> str:
    """A redacted unified diff from ``old`` to ``new``, labelled with the target path."""
    lines = difflib.unified_diff(
        _diff_lines(old), _diff_lines(new), fromfile=f"a{display}", tofile=f"b{display}"
    )
    return redact_text("".join(lines))


def _diff_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = [line + "\n" for line in text.split("\n")]
    if text.endswith("\n"):
        lines.pop()
    else:
        lines[-1] += "\\ No newline at end of file\n"
    return lines


def _keep_ownership(temporary: str, existing: os.stat_result, path: Path) -> None:
    """Preserve ``existing`` uid and gid on ``temporary``, or raise :class:`UnsafePathError`.

    When the owner already matches the current process, only the group is changed so that
    a non-root user can still update a file whose group belongs to ``os.getgroups()``.
    If ownership cannot be kept (for example, the file is owned by another user or a
    group the process does not belong to), the write is refused rather than silently
    changing the file's owner or group.
    """
    current = os.stat(temporary)
    if (current.st_uid, current.st_gid) == (existing.st_uid, existing.st_gid):
        return
    target_uid = -1 if current.st_uid == existing.st_uid else existing.st_uid
    target_gid = -1 if current.st_gid == existing.st_gid else existing.st_gid
    try:
        os.chown(temporary, target_uid, target_gid)
    except PermissionError as exc:
        raise UnsafePathError(
            f"Cannot keep the owner of {path.name} ({existing.st_uid}:{existing.st_gid})"
        ) from exc
    updated = os.stat(temporary)
    if (updated.st_uid, updated.st_gid) != (existing.st_uid, existing.st_gid):
        raise UnsafePathError(
            f"Cannot keep the owner of {path.name} ({existing.st_uid}:{existing.st_gid})"
        )


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
