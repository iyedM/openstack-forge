"""Build a wheel and check that a non-editable install can validate configurations.

The wheel is built offline from a copy of the project, so the source tree is not
modified and no network access is needed (setuptools and wheel are dev dependencies).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_FILES_IN_WHEEL = (
    "openstack_forge/config/schemas/cluster.schema.json",
    "openstack_forge/orchestration/data/steps.json",
    "openstack_forge/orchestration/schemas/steps.schema.json",
)


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")
    workdir = tmp_path_factory.mktemp("build")
    project = workdir / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, project / name)
    shutil.copytree(
        REPO_ROOT / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    dist = workdir / "dist"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(project),
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--quiet",
            "--wheel-dir",
            str(dist),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (built,) = dist.glob("openstack_forge-*.whl")
    return built


@pytest.mark.parametrize("data_file", DATA_FILES_IN_WHEEL)
def test_wheel_contains_data_file(wheel: Path, data_file: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        assert data_file in archive.namelist()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["config", "validate"], "Configuration is valid"),
        (["plan", "--config"], "Summary: 28 step(s) on 5 node(s)"),
    ],
)
def test_installed_wheel_runs_command(
    wheel: Path, tmp_path: Path, command: list[str], expected: str
) -> None:
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    # PYTHONPATH precedes the editable install, so the extracted wheel is what gets imported;
    # running from tmp_path keeps the source tree off sys.path.
    env = {**os.environ, "PYTHONPATH": str(site)}
    script = (
        "import sys, openstack_forge.config.schema as s; "
        "print(s.__file__); "
        "from openstack_forge.cli import main; "
        "sys.exit(main(sys.argv[1:]))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            *command,
            str(REPO_ROOT / "examples" / "cluster.example.yaml"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    imported_from, *output = result.stdout.splitlines()
    assert Path(imported_from).is_relative_to(site)
    assert expected in "\n".join(output)
