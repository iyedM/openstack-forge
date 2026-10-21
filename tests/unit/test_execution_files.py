"""Tests for the managed_block, ini_set and comment_out_lines file primitives."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from openstack_forge.execution import (
    DISABLED_LINE_PREFIX,
    MASK,
    FileFormatError,
    RootEscapeError,
    UnsafePathError,
    comment_out_lines,
    disable_lines_and_set_block,
    fsutil,
    ini_set,
    ini_set_many,
    managed_block,
    uncomment_lines,
)
from openstack_forge.execution import files as files_module

BLOCK = "openstack-forge hosts"
BEGIN, END = f"# BEGIN {BLOCK}", f"# END {BLOCK}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    (directory / "etc").mkdir(parents=True)
    return directory


def _write(root: Path, target: str, text: str, mode: int | None = None) -> Path:
    path = root / target.lstrip("/")
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_mtime_ns, info.st_ino


def _forbid_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("an unchanged edit must not write")

    monkeypatch.setattr(files_module, "atomic_write_text", fail)
    monkeypatch.setattr(files_module, "backup_once", fail)


# --- managed_block --------------------------------------------------------------------


def test_block_creates_missing_file(root: Path) -> None:
    change = managed_block(root, "/etc/hosts", BLOCK, "192.0.2.11 ctl-01\n")
    path = root / "etc/hosts"
    assert change.changed and change.created
    assert change.backup is None  # nothing existed to back up
    assert path.read_text() == f"{BEGIN}\n192.0.2.11 ctl-01\n{END}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not (root / "etc/hosts.orig").exists()


def test_block_is_appended_and_other_content_preserved(root: Path) -> None:
    _write(root, "/etc/hosts", "127.0.0.1 localhost\n# keep me\n::1 ip6-localhost\n")
    managed_block(root, "/etc/hosts", BLOCK, "192.0.2.11 ctl-01\n192.0.2.21 cmp-01\n")
    assert (root / "etc/hosts").read_text() == (
        "127.0.0.1 localhost\n# keep me\n::1 ip6-localhost\n"
        f"{BEGIN}\n192.0.2.11 ctl-01\n192.0.2.21 cmp-01\n{END}\n"
    )


def test_block_is_replaced_in_place(root: Path) -> None:
    _write(root, "/etc/hosts", f"before\n{BEGIN}\nold line\n{END}\nafter\n")
    change = managed_block(root, "/etc/hosts", BLOCK, "new line\n")
    assert change.changed
    assert (root / "etc/hosts").read_text() == f"before\n{BEGIN}\nnew line\n{END}\nafter\n"


def test_block_markers_may_be_indented(root: Path) -> None:
    _write(root, "/etc/hosts", f"  {BEGIN}\nold\n  {END}\n")
    managed_block(root, "/etc/hosts", BLOCK, "new\n")
    assert (root / "etc/hosts").read_text() == f"{BEGIN}\nnew\n{END}\n"


def test_block_is_idempotent(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "/etc/hosts", "127.0.0.1 localhost\n")
    first = managed_block(root, "/etc/hosts", BLOCK, "192.0.2.11 ctl-01\n")
    before = _snapshot(root / "etc/hosts")
    _forbid_writes(monkeypatch)
    second = managed_block(root, "/etc/hosts", BLOCK, "192.0.2.11 ctl-01\n")
    assert first.changed
    assert (second.changed, second.diff, second.backup) == (False, "", None)
    assert _snapshot(root / "etc/hosts") == before


def test_other_blocks_are_left_alone(root: Path) -> None:
    other = "# BEGIN someone-else\nx\n# END someone-else\n"
    _write(root, "/etc/hosts", other)
    managed_block(root, "/etc/hosts", BLOCK, "y\n")
    assert (root / "etc/hosts").read_text() == f"{other}{BEGIN}\ny\n{END}\n"


def test_empty_block_content(root: Path) -> None:
    managed_block(root, "/etc/hosts", BLOCK, "")
    assert (root / "etc/hosts").read_text() == f"{BEGIN}\n{END}\n"


def test_custom_comment_prefix(root: Path) -> None:
    managed_block(root, "/etc/app.conf", "forge", "a\n", comment=";")
    assert (root / "etc/app.conf").read_text() == "; BEGIN forge\na\n; END forge\n"


def test_block_after_file_without_trailing_newline(root: Path) -> None:
    _write(root, "/etc/hosts", "127.0.0.1 localhost")
    change = managed_block(root, "/etc/hosts", BLOCK, "x\n")
    assert (root / "etc/hosts").read_text() == f"127.0.0.1 localhost\n{BEGIN}\nx\n{END}\n"
    assert "\\ No newline at end of file" in change.diff


@pytest.mark.parametrize(
    "text",
    [
        f"{BEGIN}\nno end\n",
        f"no begin\n{END}\n",
        f"{END}\nreversed\n{BEGIN}\n",
        f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}\n",
    ],
)
def test_broken_markers_are_refused(root: Path, text: str) -> None:
    path = _write(root, "/etc/hosts", text)
    with pytest.raises(FileFormatError, match="broken 'openstack-forge hosts' markers"):
        managed_block(root, "/etc/hosts", BLOCK, "new\n")
    assert path.read_text() == text


def test_content_may_not_contain_markers(root: Path) -> None:
    with pytest.raises(FileFormatError, match="must not contain"):
        managed_block(root, "/etc/hosts", BLOCK, f"a\n{END}\nb\n")


@pytest.mark.parametrize("name", ["", "two\nlines"])
def test_invalid_block_name(root: Path, name: str) -> None:
    with pytest.raises(ValueError):
        managed_block(root, "/etc/hosts", name, "x\n")


# --- ini_set ----------------------------------------------------------------------

NOVA = """\
# Sample nova.conf
[DEFAULT]
# Print debugging output
#debug = false
debug = false

[database]
# The SQLAlchemy connection string
#connection = <None>
#max_retries = 10

[api]
# Authentication strategy
auth_strategy = noauth
"""


def test_ini_replaces_active_key_only_in_its_section(root: Path) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "api", "auth_strategy", "keystone")
    assert (root / "etc/nova.conf").read_text() == NOVA.replace(
        "auth_strategy = noauth", "auth_strategy = keystone"
    )


def test_ini_active_key_wins_over_commented_default(root: Path) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "DEFAULT", "debug", "true")
    text = (root / "etc/nova.conf").read_text()
    assert "#debug = false\ndebug = true\n" in text  # the comment is kept
    assert text.count("debug = true") == 1


def test_ini_replaces_commented_default(root: Path) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "database", "connection", "mysql+pymysql://nova@ctl/nova")
    assert (root / "etc/nova.conf").read_text() == NOVA.replace(
        "#connection = <None>", "connection = mysql+pymysql://nova@ctl/nova"
    )


def test_ini_commented_default_with_semicolon(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\n; workers = 1\n")
    ini_set(root, "/etc/app.ini", "main", "workers", "4")
    assert (root / "etc/app.ini").read_text() == "[main]\nworkers = 4\n"


def test_ini_commented_default_in_another_section_is_not_used(root: Path) -> None:
    _write(root, "/etc/app.ini", "[a]\n#key = 1\n\n[b]\nother = x\n")
    ini_set(root, "/etc/app.ini", "b", "key", "2")
    assert (root / "etc/app.ini").read_text() == "[a]\n#key = 1\n\n[b]\nother = x\nkey = 2\n"


def test_ini_prose_comment_mentioning_the_key_is_not_replaced(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\n# connection is required\n")
    ini_set(root, "/etc/app.ini", "main", "connection", "x")
    assert (root / "etc/app.ini").read_text() == (
        "[main]\n# connection is required\nconnection = x\n"
    )


def test_ini_appends_after_the_last_line_of_the_section(root: Path) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "database", "pool_size", "5")
    assert (root / "etc/nova.conf").read_text() == NOVA.replace(
        "#max_retries = 10\n", "#max_retries = 10\npool_size = 5\n"
    )


def test_ini_creates_missing_section(root: Path) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "keystone_authtoken", "auth_type", "password")
    assert (root / "etc/nova.conf").read_text() == (
        NOVA + "\n[keystone_authtoken]\nauth_type = password\n"
    )


def test_ini_creates_missing_file(root: Path) -> None:
    change = ini_set(root, "/etc/new.conf", "main", "key", "value")
    assert change.created
    assert (root / "etc/new.conf").read_text() == "[main]\nkey = value\n"


def test_ini_never_creates_duplicates(root: Path) -> None:
    for value in ("1", "2", "3"):
        ini_set(root, "/etc/app.ini", "main", "workers", value)
    assert (root / "etc/app.ini").read_text() == "[main]\nworkers = 3\n"


def test_ini_existing_duplicates_all_get_the_value(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\nkey = a\nother = x\nkey = b\n")
    ini_set(root, "/etc/app.ini", "main", "key", "c")
    assert (root / "etc/app.ini").read_text() == "[main]\nkey = c\nother = x\nkey = c\n"


def test_ini_repeated_section_headers(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\na = 1\n[other]\nz = 0\n[main]\nb = 2\n")
    ini_set(root, "/etc/app.ini", "main", "a", "10")
    ini_set(root, "/etc/app.ini", "main", "c", "3")
    assert (root / "etc/app.ini").read_text() == (
        "[main]\na = 10\n[other]\nz = 0\n[main]\nb = 2\nc = 3\n"
    )


@pytest.mark.parametrize("line", ["key=old", "  key   =   old", "key =", "key"])
def test_ini_matches_key_spacing_variants_and_bare_flags(root: Path, line: str) -> None:
    _write(root, "/etc/my.cnf", f"[mysqld]\n{line}\nnext = 1\n")
    ini_set(root, "/etc/my.cnf", "mysqld", "key", "new")
    assert (root / "etc/my.cnf").read_text() == "[mysqld]\nkey = new\nnext = 1\n"


def test_ini_does_not_match_a_longer_key(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\nkey_extra = 1\n")
    ini_set(root, "/etc/app.ini", "main", "key", "2")
    assert (root / "etc/app.ini").read_text() == "[main]\nkey_extra = 1\nkey = 2\n"


def test_ini_is_idempotent(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "/etc/nova.conf", NOVA)
    ini_set(root, "/etc/nova.conf", "database", "connection", "x")
    before = _snapshot(root / "etc/nova.conf")
    _forbid_writes(monkeypatch)
    change = ini_set(root, "/etc/nova.conf", "database", "connection", "x")
    assert (change.changed, change.diff) == (False, "")
    assert _snapshot(root / "etc/nova.conf") == before


def test_ini_keeps_a_missing_final_newline(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\nkey = 1\n[end]\nlast = 0")
    ini_set(root, "/etc/app.ini", "main", "key", "2")
    assert (root / "etc/app.ini").read_text() == "[main]\nkey = 2\n[end]\nlast = 0"


def test_ini_refuses_multi_line_values(root: Path) -> None:
    text = "[main]\nkey = first\n  continued\n"
    path = _write(root, "/etc/app.ini", text)
    with pytest.raises(FileFormatError, match="multi-line value"):
        ini_set(root, "/etc/app.ini", "main", "key", "x")
    assert path.read_text() == text


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("main", "a=b", "x"),
        ("main", "#key", "x"),
        ("main", " key", "x"),
        ("main", "", "x"),
        ("main", "key", "two\nlines"),
        ("ma]in", "key", "x"),
        ("", "key", "x"),
    ],
)
def test_ini_rejects_invalid_arguments(root: Path, section: str, key: str, value: str) -> None:
    with pytest.raises(ValueError):
        ini_set(root, "/etc/app.ini", section, key, value)
    assert not (root / "etc/app.ini").exists()


def test_ini_empty_value(root: Path) -> None:
    ini_set(root, "/etc/app.ini", "main", "key", "")
    assert (root / "etc/app.ini").read_text() == "[main]\nkey = \n"


# --- comment_out_lines & uncomment_lines -----------------------------------------------


def test_comment_out_lines_matches_and_preserves_everything_else(root: Path) -> None:
    initial = (
        "127.0.0.1 localhost\n"
        "127.0.1.1 ctl-01.example.local ctl-01\n"
        "127.0.1.1 other-host\n"
        "# 127.0.1.1 ctl-01 already commented\n"
        "::1 ip6-localhost\n"
    )
    _write(root, "/etc/hosts", initial)
    change = comment_out_lines(root, "/etc/hosts", r"^\s*127\.0\.1\.1\s+.*\bctl-01\b")
    assert change.changed and not change.created
    assert change.backup == "/etc/hosts.orig"
    assert (root / "etc/hosts").read_text() == (
        "127.0.0.1 localhost\n"
        f"{DISABLED_LINE_PREFIX}127.0.1.1 ctl-01.example.local ctl-01\n"
        "127.0.1.1 other-host\n"
        "# 127.0.1.1 ctl-01 already commented\n"
        "::1 ip6-localhost\n"
    )
    assert f"+{DISABLED_LINE_PREFIX}127.0.1.1 ctl-01.example.local ctl-01" in change.diff


def test_comment_out_lines_is_idempotent(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(root, "/etc/hosts", "127.0.0.1 localhost\n127.0.1.1 ctl-01\n")
    pattern = re.compile(r"^\s*127\.0\.1\.1\s+.*\bctl-01\b")
    first = comment_out_lines(root, "/etc/hosts", pattern)
    before = _snapshot(root / "etc/hosts")
    _forbid_writes(monkeypatch)
    second = comment_out_lines(root, "/etc/hosts", pattern)
    assert first.changed
    assert (second.changed, second.diff, second.backup) == (False, "", None)
    assert _snapshot(root / "etc/hosts") == before


def test_comment_out_lines_missing_file_is_a_noop(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_writes(monkeypatch)
    change = comment_out_lines(root, "/etc/hosts", r"127\.0\.1\.1")
    assert (change.changed, change.created, change.diff, change.backup) == (False, False, "", None)
    assert not (root / "etc/hosts").exists()


def test_comment_out_lines_preserves_missing_final_newline(root: Path) -> None:
    _write(root, "/etc/hosts", "127.0.0.1 localhost\n127.0.1.1 ctl-01")
    comment_out_lines(root, "/etc/hosts", r"127\.0\.1\.1")
    assert (root / "etc/hosts").read_text() == (
        f"127.0.0.1 localhost\n{DISABLED_LINE_PREFIX}127.0.1.1 ctl-01"
    )


def test_comment_out_lines_redacts_secrets_in_diff(root: Path) -> None:
    _write(root, "/etc/app.conf", "admin_password = TOPSECRET\n")
    change = comment_out_lines(root, "/etc/app.conf", r"^admin_password\b")
    assert "TOPSECRET" not in change.diff
    assert MASK in change.diff
    assert (root / "etc/app.conf").read_text() == (
        f"{DISABLED_LINE_PREFIX}admin_password = TOPSECRET\n"
    )


def test_comment_out_lines_dry_run_writes_nothing(root: Path) -> None:
    path = _write(root, "/etc/hosts", "127.0.1.1 ctl-01\n")
    before = _snapshot(path)
    change = comment_out_lines(root, "/etc/hosts", r"127\.0\.1\.1", dry_run=True)
    assert change.changed and DISABLED_LINE_PREFIX in change.diff
    assert change.backup is None
    assert _snapshot(path) == before
    assert not (root / "etc/hosts.orig").exists()


@pytest.mark.parametrize(
    ("pattern", "prefix"),
    [
        ("[unclosed", DISABLED_LINE_PREFIX),
        (r"127\.0\.1\.1", ""),
        (r"127\.0\.1\.1", "# two\nlines: "),
        (r"127\.0\.1\.1", "not-a-comment: "),
    ],
)
def test_comment_out_lines_rejects_invalid_arguments(root: Path, pattern: str, prefix: str) -> None:
    with pytest.raises(ValueError):
        comment_out_lines(root, "/etc/hosts", pattern, prefix=prefix)


def test_uncomment_lines_reverses_comment_out_lines_idempotently(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "127.0.0.1 localhost\n127.0.1.1 ctl-01\n# ordinary comment\n"
    _write(root, "/etc/hosts", original)
    comment_out_lines(root, "/etc/hosts", r"127\.0\.1\.1")
    restored = uncomment_lines(root, "/etc/hosts", r"127\.0\.1\.1")
    assert restored.changed
    assert (root / "etc/hosts").read_text() == original
    before = _snapshot(root / "etc/hosts")
    _forbid_writes(monkeypatch)
    again = uncomment_lines(root, "/etc/hosts", r"127\.0\.1\.1")
    assert not again.changed and _snapshot(root / "etc/hosts") == before


# --- file format assumptions ----------------------------------------------------------


@pytest.mark.parametrize("edit", ["block", "ini", "comment"])
def test_crlf_files_are_refused(root: Path, edit: str) -> None:
    path = root / "etc/app.ini"
    path.write_bytes(b"[main]\r\nkey = 1\r\n")
    with pytest.raises(FileFormatError, match="CRLF"):
        if edit == "block":
            managed_block(root, "/etc/app.ini", BLOCK, "x\n")
        elif edit == "ini":
            ini_set(root, "/etc/app.ini", "main", "key", "2")
        else:
            comment_out_lines(root, "/etc/app.ini", r"^key")
    assert path.read_bytes() == b"[main]\r\nkey = 1\r\n"


def test_non_utf8_files_are_refused(root: Path) -> None:
    (root / "etc/hosts").write_bytes(b"caf\xe9\n")
    with pytest.raises(FileFormatError, match="UTF-8"):
        managed_block(root, "/etc/hosts", BLOCK, "x\n")


# --- diffs ------------------------------------------------------------------------------


def test_diff_content(root: Path) -> None:
    _write(root, "/etc/app.ini", "[main]\nkey = old\n")
    change = ini_set(root, "/etc/app.ini", "main", "key", "new")
    assert change.diff == (
        "--- a/etc/app.ini\n+++ b/etc/app.ini\n@@ -1,2 +1,2 @@\n [main]\n-key = old\n+key = new\n"
    )


def test_diff_is_redacted_but_file_has_the_real_value(root: Path) -> None:
    change = ini_set(root, "/etc/nova.conf", "keystone_authtoken", "password", "NOVA_PASS")
    url = ini_set(root, "/etc/nova.conf", "database", "connection", "mysql://nova:DBPASS@ctl/nova")
    assert "NOVA_PASS" not in change.diff and f"+password = {MASK}" in change.diff
    assert "DBPASS" not in url.diff and f"mysql://nova:{MASK}@ctl/nova" in url.diff
    text = (root / "etc/nova.conf").read_text()
    assert "password = NOVA_PASS" in text and "nova:DBPASS@" in text


def test_dry_run_reports_without_writing(root: Path) -> None:
    path = _write(root, "/etc/hosts", "127.0.0.1 localhost\n")
    before = _snapshot(path)
    change = managed_block(root, "/etc/hosts", BLOCK, "x\n", dry_run=True)
    assert change.changed and f"+{BEGIN}" in change.diff
    assert change.backup is None
    assert _snapshot(path) == before
    assert not (root / "etc/hosts.orig").exists()
    missing = ini_set(root, "/etc/new.conf", "main", "k", "v", dry_run=True)
    assert missing.created and not (root / "etc/new.conf").exists()


# --- backups ----------------------------------------------------------------------------


def test_backup_is_created_once_before_the_first_modification(root: Path) -> None:
    original = "[main]\nkey = original\n"
    _write(root, "/etc/app.ini", original, mode=0o640)
    first = ini_set(root, "/etc/app.ini", "main", "key", "first")
    second = ini_set(root, "/etc/app.ini", "main", "key", "second")
    backup = root / "etc/app.ini.orig"
    assert first.backup == "/etc/app.ini.orig"
    assert second.backup is None
    assert backup.read_text() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640


def test_existing_backup_is_never_overwritten(root: Path) -> None:
    _write(root, "/etc/hosts", "current\n")
    _write(root, "/etc/hosts.orig", "older backup\n")
    change = managed_block(root, "/etc/hosts", BLOCK, "x\n")
    assert change.backup is None
    assert (root / "etc/hosts.orig").read_text() == "older backup\n"


def test_no_temporary_files_are_left_behind(root: Path) -> None:
    _write(root, "/etc/hosts", "a\n")
    managed_block(root, "/etc/hosts", BLOCK, "x\n")
    managed_block(root, "/etc/hosts", BLOCK, "y\n")
    assert sorted(p.name for p in (root / "etc").iterdir()) == ["hosts", "hosts.orig"]


# --- atomic writes, permissions and ownership -------------------------------------------


def test_permissions_are_preserved(root: Path) -> None:
    path = _write(root, "/etc/secret.conf", "[main]\nkey = 1\n", mode=0o640)
    ini_set(root, "/etc/secret.conf", "main", "key", "2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_supplementary_group_ownership_is_preserved_without_root(root: Path) -> None:
    egid = os.getegid()
    supplementary_groups = [gid for gid in os.getgroups() if gid != egid]
    if not supplementary_groups:
        pytest.skip("Current user belongs to a single group; cannot test group preservation")

    path = _write(root, "/etc/group_owned.conf", "[main]\nkey = 1\n", mode=0o640)
    target_gid = None
    for candidate in supplementary_groups:
        try:
            os.chown(path, -1, candidate)
        except PermissionError:
            continue
        target_gid = candidate
        break
    if target_gid is None:
        pytest.skip("Could not chgrp file to any supplementary group in this environment")

    assert path.stat().st_gid == target_gid
    ini_set(root, "/etc/group_owned.conf", "main", "key", "2")
    after = path.stat()
    assert after.st_uid == os.geteuid()
    assert after.st_gid == target_gid
    assert stat.S_IMODE(after.st_mode) == 0o640


def test_ownership_change_is_refused_and_never_silent(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(root, "/etc/owned.conf", "[main]\nkey = 1\n", mode=0o640)
    real_stat = path.stat()
    fake_existing = os.stat_result(
        (
            real_stat.st_mode,
            real_stat.st_ino,
            real_stat.st_dev,
            real_stat.st_nlink,
            os.geteuid() + 1000,
            os.getegid() + 1000,
            real_stat.st_size,
            real_stat.st_atime,
            real_stat.st_mtime,
            real_stat.st_ctime,
        )
    )

    def deny_chown(target: str, uid: int, gid: int) -> None:
        raise PermissionError(1, "Operation not permitted", target)

    monkeypatch.setattr(fsutil.os, "chown", deny_chown)
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: fake_existing)
    with pytest.raises(UnsafePathError, match="Cannot keep the owner"):
        fsutil.atomic_write_text(path, "[main]\nkey = 2\n", mode=0o640)
    assert path.read_text() == "[main]\nkey = 1\n"
    assert sorted(p.name for p in (root / "etc").iterdir()) == ["owned.conf"]


def test_new_file_mode_is_configurable(root: Path) -> None:
    ini_set(root, "/etc/private.conf", "main", "key", "1", new_file_mode=0o600)
    assert stat.S_IMODE((root / "etc/private.conf").stat().st_mode) == 0o600


def test_write_is_atomic_when_interrupted(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SimulatedCrash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise SimulatedCrash

    path = _write(root, "/etc/app.ini", "[main]\nkey = 1\n")
    monkeypatch.setattr(fsutil.os, "replace", crash)
    with pytest.raises(SimulatedCrash):
        ini_set(root, "/etc/app.ini", "main", "key", "2")
    assert path.read_text() == "[main]\nkey = 1\n"
    assert sorted(p.name for p in (root / "etc").iterdir()) == ["app.ini", "app.ini.orig"]


# --- unsafe targets ------------------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["../outside.txt", "/etc/../../outside.txt", "etc/../../outside.txt", "/", ""]
)
def test_targets_cannot_escape_the_root(root: Path, target: str) -> None:
    with pytest.raises(RootEscapeError):
        managed_block(root, target, BLOCK, "x\n")
    with pytest.raises(RootEscapeError):
        comment_out_lines(root, target, r"x")
    assert not (root.parent / "outside.txt").exists()


def test_symlinked_directory_leading_outside_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "etc/linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RootEscapeError, match="resolves outside"):
        ini_set(root, "/etc/linked/app.ini", "main", "key", "1")
    assert list(outside.iterdir()) == []


def test_symlinked_file_is_refused(root: Path) -> None:
    real = _write(root, "/etc/real.conf", "a\n")
    (root / "etc/link.conf").symlink_to(real)
    with pytest.raises(UnsafePathError, match="symlink"):
        managed_block(root, "/etc/link.conf", BLOCK, "x\n")
    assert real.read_text() == "a\n"


def test_directory_target_is_refused(root: Path) -> None:
    with pytest.raises(UnsafePathError, match="not a regular file"):
        managed_block(root, "/etc", BLOCK, "x\n")


def test_missing_parent_directory_is_not_created(root: Path) -> None:
    with pytest.raises(UnsafePathError, match="parent directory does not exist"):
        ini_set(root, "/etc/chrony/chrony.conf", "main", "key", "1")
    assert not (root / "etc/chrony").exists()


def test_missing_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RootEscapeError, match="does not exist"):
        managed_block(tmp_path / "absent", "/etc/hosts", BLOCK, "x\n")


def test_relative_and_absolute_targets_are_equivalent(root: Path) -> None:
    managed_block(root, "etc/hosts", BLOCK, "x\n")
    change = managed_block(root, "/etc/hosts", BLOCK, "x\n")
    assert change.path == "/etc/hosts" and not change.changed
    assert os.listdir(root / "etc") == ["hosts"]


# --- ini_set_many --------------------------------------------------------------------------

MYSQLD = [
    ("mysqld", "bind-address", "192.0.2.11"),
    ("mysqld", "default-storage-engine", "innodb"),
    ("mysqld", "max_connections", "4096"),
]


def test_ini_set_many_creates_a_new_file_without_a_backup(root: Path) -> None:
    change = ini_set_many(root, "/etc/app.cnf", MYSQLD)
    assert (change.changed, change.created, change.backup) == (True, True, None)
    assert (root / "etc/app.cnf").read_text() == (
        "[mysqld]\nbind-address = 192.0.2.11\ndefault-storage-engine = innodb\n"
        "max_connections = 4096\n"
    )
    assert sorted(path.name for path in (root / "etc").iterdir()) == ["app.cnf"]


def test_separate_ini_set_calls_would_back_up_a_partial_file(root: Path) -> None:
    # Why ini_set_many exists: the second ini_set call backs up the 1-key file it just made.
    for section, key, value in MYSQLD[:2]:
        ini_set(root, "/etc/app.cnf", section, key, value)
    assert (root / "etc/app.cnf.orig").read_text() == "[mysqld]\nbind-address = 192.0.2.11\n"


def test_ini_set_many_backs_up_the_original_content_once(root: Path) -> None:
    original = "# managed elsewhere\n[client]\nport = 3306\n\n[mysqld]\nmax_connections = 100\n"
    _write(root, "/etc/app.cnf", original, mode=0o640)
    change = ini_set_many(root, "/etc/app.cnf", MYSQLD)
    second = ini_set_many(root, "/etc/app.cnf", [("mysqld", "max_connections", "8192")])
    assert (change.created, change.backup, second.backup) == (False, "/etc/app.cnf.orig", None)
    assert (root / "etc/app.cnf.orig").read_text() == original
    assert stat.S_IMODE((root / "etc/app.cnf").stat().st_mode) == 0o640
    assert (root / "etc/app.cnf").read_text() == (
        "# managed elsewhere\n[client]\nport = 3306\n\n[mysqld]\nmax_connections = 8192\n"
        "bind-address = 192.0.2.11\ndefault-storage-engine = innodb\n"
    )


def test_ini_set_many_is_idempotent(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ini_set_many(root, "/etc/app.cnf", MYSQLD)
    before = _snapshot(root / "etc/app.cnf")
    _forbid_writes(monkeypatch)
    change = ini_set_many(root, "/etc/app.cnf", MYSQLD)
    assert (change.changed, change.diff, change.backup, change.created) == (False, "", None, False)
    assert _snapshot(root / "etc/app.cnf") == before


def test_ini_set_many_dry_run_shows_one_combined_diff(root: Path) -> None:
    _write(root, "/etc/app.cnf", "[mysqld]\nmax_connections = 100\n")
    change = ini_set_many(root, "/etc/app.cnf", MYSQLD, dry_run=True)
    assert change.diff == (
        "--- a/etc/app.cnf\n"
        "+++ b/etc/app.cnf\n"
        "@@ -1,2 +1,4 @@\n"
        " [mysqld]\n"
        "-max_connections = 100\n"
        "+max_connections = 4096\n"
        "+bind-address = 192.0.2.11\n"
        "+default-storage-engine = innodb\n"
    )
    assert (root / "etc/app.cnf").read_text() == "[mysqld]\nmax_connections = 100\n"
    assert not (root / "etc/app.cnf.orig").exists()


def test_ini_set_many_dry_run_on_a_missing_file(root: Path) -> None:
    change = ini_set_many(root, "/etc/new.cnf", MYSQLD, dry_run=True)
    assert change.changed and change.created and change.backup is None
    assert change.diff.startswith("--- a/etc/new.cnf\n+++ b/etc/new.cnf\n@@ -0,0 +1,4 @@\n")
    assert not (root / "etc/new.cnf").exists()


def test_ini_set_many_preserves_other_sections_and_comments(root: Path) -> None:
    text = (
        "[client]\n# keep this comment\nuser = app\n\n[mysqld]\n#max_connections = 151\n"
        "\n[mysqldump]\nquick\n"
    )
    _write(root, "/etc/app.cnf", text)
    ini_set_many(root, "/etc/app.cnf", [*MYSQLD, ("mysqldump", "max_allowed_packet", "16M")])
    assert (root / "etc/app.cnf").read_text() == (
        "[client]\n# keep this comment\nuser = app\n\n[mysqld]\nmax_connections = 4096\n"
        "bind-address = 192.0.2.11\ndefault-storage-engine = innodb\n\n[mysqldump]\nquick\n"
        "max_allowed_packet = 16M\n"
    )


def test_ini_set_many_applies_settings_in_order(root: Path) -> None:
    ini_set_many(root, "/etc/app.cnf", [("s", "k", "1"), ("s", "k", "2")])
    assert (root / "etc/app.cnf").read_text() == "[s]\nk = 2\n"


@pytest.mark.parametrize(
    "settings",
    [[], [("mysqld", "key")], [("mysqld", "a=b", "x")], [("mysqld", "k", 5)], ["not a tuple"]],
)
def test_ini_set_many_validates_every_setting_first(root: Path, settings) -> None:
    _write(root, "/etc/app.cnf", "[mysqld]\n")
    with pytest.raises(ValueError):
        ini_set_many(root, "/etc/app.cnf", [("mysqld", "ok", "1"), *settings] if settings else [])
    assert (root / "etc/app.cnf").read_text() == "[mysqld]\n"


def test_ini_set_many_never_creates_the_parent_directory(root: Path) -> None:
    with pytest.raises(UnsafePathError, match="parent directory does not exist"):
        ini_set_many(root, "/etc/mysql/mariadb.conf.d/99-openstack.cnf", MYSQLD)
    assert not (root / "etc/mysql").exists()


def test_ini_set_many_diff_is_redacted(root: Path) -> None:
    change = ini_set_many(root, "/etc/app.cnf", [("client", "password", "hunter2")])
    assert "hunter2" not in change.diff and f"password = {MASK}" in change.diff
    assert "password = hunter2" in (root / "etc/app.cnf").read_text()


# --- disable_lines_and_set_block -----------------------------------------------------------

LISTEN = re.compile(r"^\s*-l\b")


def test_disable_and_set_block_is_one_atomic_edit(root: Path, monkeypatch) -> None:
    path = _write(root, "/etc/app.conf", "-m 64\n-l 127.0.0.1\n")
    writes: list[str] = []
    real_write = files_module.atomic_write_text

    def counting_write(target, text, *, mode):
        writes.append(text)
        real_write(target, text, mode=mode)

    monkeypatch.setattr(files_module, "atomic_write_text", counting_write)
    change = disable_lines_and_set_block(root, "/etc/app.conf", LISTEN, "forge", "-l 192.0.2.11\n")
    expected = (
        f"-m 64\n{DISABLED_LINE_PREFIX}-l 127.0.0.1\n# BEGIN forge\n-l 192.0.2.11\n# END forge\n"
    )
    assert writes == [expected]  # a single write, never a state without any -l line
    assert path.read_text() == expected
    assert change.backup == "/etc/app.conf.orig"
    assert (root / "etc/app.conf.orig").read_text() == "-m 64\n-l 127.0.0.1\n"


def test_disable_and_set_block_is_idempotent_and_spares_its_block(root, monkeypatch) -> None:
    _write(root, "/etc/app.conf", "-l 127.0.0.1\n")
    disable_lines_and_set_block(root, "/etc/app.conf", LISTEN, "forge", "-l 192.0.2.11\n")
    before = _snapshot(root / "etc/app.conf")
    _forbid_writes(monkeypatch)
    change = disable_lines_and_set_block(root, "/etc/app.conf", LISTEN, "forge", "-l 192.0.2.11\n")
    assert (change.changed, change.diff) == (False, "")
    assert _snapshot(root / "etc/app.conf") == before


def test_disable_and_set_block_leaves_a_missing_file_absent(root: Path) -> None:
    change = disable_lines_and_set_block(root, "/etc/absent.conf", LISTEN, "forge", "-l x\n")
    assert (change.changed, change.created) == (False, False)
    assert not (root / "etc/absent.conf").exists()


def test_disable_and_set_block_dry_run_has_one_diff(root: Path) -> None:
    path = _write(root, "/etc/app.conf", "-l 127.0.0.1\n")
    change = disable_lines_and_set_block(
        root, "/etc/app.conf", LISTEN, "forge", "-l 192.0.2.11\n", dry_run=True
    )
    assert f"-{'-l 127.0.0.1'}" in change.diff and "+-l 192.0.2.11" in change.diff
    assert f"+{DISABLED_LINE_PREFIX}-l 127.0.0.1" in change.diff
    assert path.read_text() == "-l 127.0.0.1\n"
    assert not (root / "etc/app.conf.orig").exists()
