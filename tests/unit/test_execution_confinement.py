"""Prove that the execution building blocks write nothing outside the root directory.

A CPython audit hook records every filesystem-modifying event raised while the engine
runs real file primitives; each recorded path must lie inside the root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.cli import main
from openstack_forge.config import load_config
from openstack_forge.execution import (
    Engine,
    FunctionStep,
    HostsFileStep,
    RootEscapeError,
    StateStore,
    UnsafePathError,
    VerifyResult,
    comment_out_lines,
    ini_set,
    managed_block,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"

# Audit events that create, modify or remove filesystem entries, and the argument
# positions holding the affected path(s).
_WRITE_EVENTS: dict[str, tuple[int, ...]] = {
    "os.rename": (0, 1),  # also raised by os.replace
    "os.link": (0, 1),
    "os.symlink": (1,),
    "os.remove": (0,),  # also raised by os.unlink
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.setxattr": (0,),
    "os.removexattr": (0,),
    "shutil.copyfile": (1,),
    "shutil.copymode": (1,),
    "shutil.copystat": (1,),
    "tempfile.mkstemp": (0,),
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

_recording: list[tuple[str, str]] | None = None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _recording is None:
        return
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(flags, int) and flags & _WRITE_FLAGS) or (
            isinstance(mode, str) and any(c in mode for c in "wax+")
        )
        if writes and not isinstance(path, int):
            _recording.append((event, os.fsdecode(path)))
        return
    for position in _WRITE_EVENTS.get(event, ()):
        if position < len(args) and isinstance(args[position], (str, bytes, os.PathLike)):
            _recording.append((event, os.fsdecode(args[position])))


sys.addaudithook(_audit)  # cannot be removed; it is inert unless _recording is set


def _inside(path: str, root: Path) -> bool:
    return Path(os.path.abspath(path)).resolve().is_relative_to(root.resolve())


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_nothing_outside_the_root_is_touched(tmp_path: Path) -> None:
    global _recording
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n127.0.1.1 forge-controller-01\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hosts").write_text("do not touch\n")
    (root / "etc/escape").symlink_to(outside, target_is_directory=True)
    before_outside = _tree(outside)
    config = load_config(EXAMPLE_CONFIG)

    def hosts(dry: bool):
        return managed_block(root, "/etc/hosts", "forge", "192.0.2.11 ctl-01\n", dry_run=dry)

    def conf(dry: bool):
        return ini_set(root, "/etc/app.ini", "main", "key", "value", dry_run=dry)

    def comment_line(dry: bool):
        return comment_out_lines(root, "/etc/app.ini", r"^key\b", dry_run=dry)

    steps = [
        FunctionStep(
            name,
            check_fn=lambda change=change: not change(True).changed,
            apply_fn=lambda change=change: change(False),
            verify_fn=lambda change=change: VerifyResult(not change(True).changed),
        )
        for name, change in (("hosts", hosts), ("conf", conf), ("comment", comment_line))
    ]
    escapes = ["../outside/hosts", "/etc/../../outside/hosts", "/etc/escape/hosts"]
    # An absolute path to the outside file is a *target* path: it is mapped under the
    # root, where its parent directory does not exist, so nothing is created anywhere.
    host_path_of_outside_file = str(outside / "hosts")

    _recording = []
    try:
        engine = Engine(StateStore(root / "var/lib/openstack-forge/state.json"))
        (root / "var/lib/openstack-forge").mkdir(parents=True)
        first = engine.run(steps)
        second = engine.run(steps)
        hosts_step = HostsFileStep(root, config, "forge-controller-01")
        third = engine.run([hosts_step])
        rc = main(
            [
                "apply",
                "--config",
                str(EXAMPLE_CONFIG),
                "--step",
                "hosts-file",
                "--node",
                "forge-compute-01",
                "--root",
                str(root),
                "--yes",
            ]
        )
        rc_secrets_init = main(["secrets", "init", "--root", str(root)])
        rc_secrets_show = main(["secrets", "show", "ADMIN_PASS", "--root", str(root)])
        for target in escapes:
            with pytest.raises(RootEscapeError):
                managed_block(root, target, "forge", "x\n")
            with pytest.raises(RootEscapeError):
                comment_out_lines(root, target, r"x")
        with pytest.raises(UnsafePathError, match="parent directory does not exist"):
            managed_block(root, host_path_of_outside_file, "forge", "x\n")
        recorded, _recording = _recording, None
    finally:
        _recording = None

    assert (
        first.ok
        and second.ok
        and third.ok
        and rc == 0
        and rc_secrets_init == 0
        and rc_secrets_show == 0
    )
    assert recorded, "the audit hook saw no write at all; the test would prove nothing"
    escaped = [(event, path) for event, path in recorded if not _inside(path, root)]
    assert escaped == []
    assert _tree(outside) == before_outside


def test_absolute_host_paths_are_mapped_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    change = managed_block(root, "/etc/hosts", "forge", "x\n")
    assert change.path == "/etc/hosts"
    assert (root / "etc/hosts").exists()
    assert "forge" not in Path("/etc/hosts").read_text(errors="replace")


def test_only_apply_imports_execution_lazily_while_read_only_commands_never_do(
    tmp_path: Path,
) -> None:
    code = (
        "import sys\n"
        "from openstack_forge.cli import main\n"
        "import openstack_forge.orchestration\n"
        "import openstack_forge.diagnostics\n"
        "import openstack_forge.config\n"
        "def loaded():\n"
        "    return sorted(m for m in sys.modules if m.startswith('openstack_forge.execution'))\n"
        "assert loaded() == [], f'imported on module load: {loaded()}'\n"
        f"cfg = {str(EXAMPLE_CONFIG)!r}\n"
        "assert main(['config', 'validate', cfg]) == 0\n"
        "assert loaded() == [], f'imported by config validate: {loaded()}'\n"
        "assert main(['plan', '--config', cfg]) == 0\n"
        "assert loaded() == [], f'imported by plan: {loaded()}'\n"
        "main(['doctor', '--config', cfg, '--node', 'no-such-node'])\n"
        "assert loaded() == [], f'imported by doctor: {loaded()}'\n"
        f"root = {str(tmp_path)!r}\n"
        "assert main(['apply', '--config', cfg, '--step', 'hosts-file', "
        "'--node', 'forge-controller-01', '--root', root]) == 0\n"
        "assert 'openstack_forge.execution' in loaded()\n"
        "print('OK')\n"
    )
    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )
    assert result.stdout.strip().endswith("OK")


@pytest.mark.parametrize("command", ["run", "install", "execute"])
def test_unimplemented_deployment_commands_do_not_exist(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([command])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
