"""Tests for the openstack-packages step (fake backend, temporary roots, no root, no network).

The executor used by step tests gets root "/" with a fake backend and a fake uid 0, so
mutating specs pass the safety check while nothing is executed. The ``apt-cache policy``
outputs in ``tests/fixtures/apt/`` are SYNTHETIC, modeled on real output read on an Ubuntu
24.04 development machine (see that directory's README); the Ubuntu Cloud Archive suite
naming they use is an assumption still to be confirmed on a real host.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from openstack_forge.cli import main
from openstack_forge.config import load_config, releases
from openstack_forge.execution import (
    ADD_APT_REPOSITORY_FAILURE_HINT,
    APT_FAILURE_HINT,
    DEFAULT_AUDIT_TARGET,
    DEFAULT_REGISTRY,
    DEFAULT_STATE_TARGET,
    IMPLEMENTED_STEPS,
    AuditLog,
    CommandExecutor,
    Engine,
    ExecutionError,
    MutationRefusedError,
    OpenStackPackagesStep,
    Outcome,
    Phase,
    PreconditionError,
    ProcessOutput,
    StateStore,
    StepStatus,
    build_step,
    cloud_archive_pocket,
    cloud_archive_sources,
    cloud_archive_suite,
    describe_cloud_archive_suite,
    parse_apt_policy,
)
from openstack_forge.orchestration import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = REPO_ROOT / "tests" / "fixtures" / "valid_cluster.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "cluster.example.yaml"
APT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "apt"

DPKG_CLIENT = ("/usr/bin/dpkg-query", "--status", "python3-openstackclient")
DPKG_SPC = ("/usr/bin/dpkg-query", "--status", "software-properties-common")
POLICY = ("/usr/bin/apt-cache", "policy", "python3-openstackclient")
ADD_REPO = ("/usr/bin/add-apt-repository", "--yes", "--cloud", "cloud-archive:epoxy")
INSTALL = ("/usr/bin/apt-get", "install", "--yes", "python3-openstackclient")

UNCONFIRMED = (
    "The repository check is unconfirmed on a real host: it parses 'apt-cache policy' and "
    "assumes the Ubuntu Cloud Archive suite is named '<series>-updates/epoxy'."
)


def policy_fixture(name: str) -> str:
    """A synthetic ``apt-cache policy`` output, modeled on real output (see README)."""
    return (APT_FIXTURES / f"policy_{name}.txt").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _forbid_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError(f"no real process or network access in these tests: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


class FakeAptHost:
    """Fake backend simulating dpkg-query, apt-cache, add-apt-repository and apt-get."""

    def __init__(
        self,
        *,
        client: str | None = None,  # None, "noble" or "uca": where the client came from
        pocket_enabled: bool = False,
        software_properties: bool = True,
    ) -> None:
        self.client = client
        self.pocket_enabled = pocket_enabled
        self.software_properties = software_properties
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[tuple[str, ...], ProcessOutput] = {}

    def run(
        self, argv: tuple[str, ...], *, env: Mapping[str, str], timeout: float
    ) -> ProcessOutput:
        self.calls.append(argv)
        if argv in self.failures:
            return self.failures[argv]
        if argv == DPKG_CLIENT:
            return self._dpkg("python3-openstackclient", self.client is not None)
        if argv == DPKG_SPC:
            return self._dpkg("software-properties-common", self.software_properties)
        if argv == POLICY:
            return ProcessOutput(0, self._policy(), "")
        if argv == ADD_REPO:
            self.pocket_enabled = True
            return ProcessOutput(0, "Repository: 'deb ... noble-updates/epoxy main'\n", "")
        if argv == INSTALL:
            self.client = "uca" if self.pocket_enabled else "noble"
            return ProcessOutput(0, "Setting up python3-openstackclient ...\n", "")
        raise AssertionError(f"unexpected argv {argv!r}")

    @staticmethod
    def _dpkg(package: str, installed: bool) -> ProcessOutput:
        if installed:
            return ProcessOutput(0, f"Package: {package}\nStatus: install ok installed\n", "")
        return ProcessOutput(1, "", f"dpkg-query: package '{package}' is not installed\n")

    def _policy(self) -> str:
        if self.pocket_enabled:
            return policy_fixture(
                "client_uca_installed" if self.client else "client_uca_not_installed"
            )
        if self.client:
            return policy_fixture("client_noble_installed")
        return policy_fixture("client_noble_not_installed")


@pytest.fixture
def config() -> dict:
    return load_config(FIXTURE_CONFIG)


def _node(config: Mapping, role: str = "controller") -> Mapping:
    return next(node for node in config["nodes"] if node["role"] == role)


def _executor(tmp_path: Path, backend, *, root: Path = Path("/")) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY, root=root, audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=backend, euid=lambda: 0,
    )  # fmt: skip


def _step(tmp_path: Path, config: Mapping, host: FakeAptHost, **kw) -> OpenStackPackagesStep:
    return OpenStackPackagesStep(tmp_path, config, _node(config), _executor(tmp_path, host, **kw))


def _run(tmp_path: Path, config: Mapping, host: FakeAptHost, **kw):
    store = StateStore(tmp_path / "state.json")
    return Engine(store).run([_step(tmp_path, config, host, **kw)]), store


# --- parsing apt-cache policy ------------------------------------------------------------


def test_parse_policy_of_the_cloud_archive_fixture() -> None:
    policy = parse_apt_policy(policy_fixture("client_uca_installed"))
    assert policy.package == "python3-openstackclient"
    assert (policy.installed, policy.candidate) == ("7.4.0-0ubuntu1~cloud0",) * 2
    assert [
        (s.version, s.priority, s.uri, s.suite, s.component, s.architecture) for s in policy.sources
    ] == [
        (
            "7.4.0-0ubuntu1~cloud0",
            500,
            "http://ubuntu-cloud.archive.canonical.com/ubuntu",
            "noble-updates/epoxy",
            "main",
            "amd64",
        ),
        ("6.6.0-0ubuntu2", 500, "http://archive.ubuntu.com/ubuntu", "noble", "main", "amd64"),
    ]  # the /var/lib/dpkg/status line is not a source


def test_parse_policy_none_and_empty_output() -> None:
    policy = parse_apt_policy(policy_fixture("client_noble_not_installed"))
    assert (policy.installed, policy.candidate) == (None, "6.6.0-0ubuntu2")
    empty = parse_apt_policy("")  # stdout of apt-cache policy for an unknown package
    assert (empty.package, empty.installed, empty.candidate, empty.sources) == (
        None, None, None, (),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("fixture", "enabled"),
    [
        ("client_uca_installed", True),
        ("client_uca_not_installed", True),
        ("client_noble_installed", False),  # Ubuntu 24.04's own package: not the pocket
        ("client_noble_not_installed", False),
        ("client_other_pockets", False),  # epoxy-proposed and dalmatian only
    ],
)
def test_cloud_archive_sources_by_fixture(fixture: str, enabled: bool) -> None:
    sources = cloud_archive_sources(
        parse_apt_policy(policy_fixture(fixture)), "cloud-archive:epoxy"
    )
    assert bool(sources) is enabled
    assert all(source.suite == "noble-updates/epoxy" for source in sources)


def test_cloud_archive_pocket_variants() -> None:
    other = parse_apt_policy(policy_fixture("client_other_pockets"))
    assert [s.suite for s in cloud_archive_sources(other, "cloud-archive:epoxy-proposed")] == [
        "noble-proposed/epoxy"
    ]
    assert [s.suite for s in cloud_archive_sources(other, "cloud-archive:dalmatian")] == [
        "noble-updates/dalmatian"
    ]
    # add-apt-repository(1): "If not specified, the pocket defaults to -updates."
    assert cloud_archive_suite("cloud-archive:epoxy") == ("epoxy", "updates")
    assert cloud_archive_suite("cloud-archive:epoxy-updates") == ("epoxy", "updates")
    assert describe_cloud_archive_suite("cloud-archive:epoxy-proposed") == "<series>-proposed/epoxy"


def test_suite_must_match_exactly() -> None:
    text = policy_fixture("client_uca_installed").replace(
        "noble-updates/epoxy/main", "noble-updates/epoxy-extra/main"
    )
    assert cloud_archive_sources(parse_apt_policy(text), "cloud-archive:epoxy") == ()


# --- the pocket comes from the release table --------------------------------------------------


def test_pocket_is_derived_from_the_release_table(
    config: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert cloud_archive_pocket(config) == "cloud-archive:epoxy"
    monkeypatch.setattr(
        releases,
        "SUPPORTED_RELEASES",
        (releases.SupportedRelease("24.04", "2025.1", "testcodename"),),
    )
    step = _step(tmp_path, config, FakeAptHost())
    assert step.pocket == "cloud-archive:testcodename"
    assert step.dry_run_preview().commands[0].argv(DEFAULT_REGISTRY) == (
        "/usr/bin/add-apt-repository", "--yes", "--cloud", "cloud-archive:testcodename",
    )  # fmt: skip


def test_unsupported_release_pair_is_refused(config: dict, tmp_path: Path) -> None:
    config["cluster"]["openstack_release"] = "2024.2"  # bypasses validation on purpose
    with pytest.raises(ExecutionError, match="not in the supported release table"):
        _step(tmp_path, config, FakeAptHost())


# --- check() ----------------------------------------------------------------------------------


def test_check_is_false_when_the_package_is_installed_but_the_pocket_is_missing(
    config: dict, tmp_path: Path
) -> None:
    # The trap: Ubuntu 24.04 ships python3-openstackclient in its own archive.
    host = FakeAptHost(client="noble", pocket_enabled=False)
    assert _step(tmp_path, config, host).check() is False
    assert host.calls == [DPKG_CLIENT, POLICY]


def test_check_is_false_when_the_pocket_is_enabled_but_the_package_is_missing(
    config: dict, tmp_path: Path
) -> None:
    host = FakeAptHost(client=None, pocket_enabled=True)
    assert _step(tmp_path, config, host).check() is False
    assert host.calls == [DPKG_CLIENT]  # no need to look at the repository


def test_check_is_true_when_both_are_present(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost(client="uca", pocket_enabled=True)
    assert _step(tmp_path, config, host).check() is True
    assert host.calls == [DPKG_CLIENT, POLICY]


def test_check_is_false_for_other_cloud_archive_pockets(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost(client="noble")
    host.failures[POLICY] = ProcessOutput(0, policy_fixture("client_other_pockets"), "")
    assert _step(tmp_path, config, host).check() is False


def test_check_is_false_when_apt_cache_knows_no_such_package(config: dict, tmp_path) -> None:
    host = FakeAptHost(client="uca", pocket_enabled=True)
    host.failures[POLICY] = ProcessOutput(0, "", "N: Unable to locate package x\n")
    assert _step(tmp_path, config, host).check() is False


# --- apply() through the engine ---------------------------------------------------------------


def test_fresh_host_apply_sequence_and_exact_argv(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost()
    report, store = _run(tmp_path, config, host)

    assert report.ok, report.failure
    assert [o.outcome for o in report.outcomes] == [Outcome.APPLIED]
    assert host.calls == [
        DPKG_CLIENT,  # check(): not installed, so the repository is not even inspected
        DPKG_SPC,  # apply(): precondition
        ("/usr/bin/add-apt-repository", "--yes", "--cloud", "cloud-archive:epoxy"),
        ("/usr/bin/apt-get", "install", "--yes", "python3-openstackclient"),
        DPKG_CLIENT,  # verify()
        POLICY,
    ]
    # add-apt-repository updates the package lists itself: no separate apt-get update.
    assert ("/usr/bin/apt-get", "update") not in host.calls
    assert store.get("openstack-packages").status is StepStatus.DONE


def test_package_from_ubuntu_archive_without_pocket_is_applied(config, tmp_path) -> None:
    host = FakeAptHost(client="noble", pocket_enabled=False)
    report, _ = _run(tmp_path, config, host)
    assert report.ok, report.failure
    assert host.calls == [DPKG_CLIENT, POLICY, DPKG_SPC, ADD_REPO, INSTALL, DPKG_CLIENT, POLICY]
    assert host.client == "uca"


def test_done_host_is_skipped_without_mutating_commands(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost(client="uca", pocket_enabled=True)
    report, _ = _run(tmp_path, config, host)
    assert [o.outcome for o in report.outcomes] == [Outcome.SKIPPED]
    assert host.calls == [DPKG_CLIENT, POLICY]


def test_no_upgrade_reboot_or_other_package(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost()
    _run(tmp_path, config, host)
    words = {word for argv in host.calls for word in argv}
    assert not words & {"upgrade", "dist-upgrade", "full-upgrade", "reboot", "/usr/sbin/reboot"}
    installs = [argv for argv in host.calls if argv[:2] == ("/usr/bin/apt-get", "install")]
    assert installs == [INSTALL]


def test_missing_software_properties_common_fails_clearly(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost(software_properties=False)
    report, store = _run(tmp_path, config, host)

    failure = report.failure
    assert (failure.phase, failure.message) == (
        Phase.APPLY, f"apply() raised {PreconditionError.__name__}",
    )  # fmt: skip
    assert "software-properties-common is not installed" in failure.details
    assert "/usr/bin/add-apt-repository" in failure.details
    assert "does not install it" in failure.details
    assert host.calls == [DPKG_CLIENT, DPKG_SPC]  # nothing installed, silently or not
    assert store.get("openstack-packages").status is StepStatus.FAILED


def test_apt_install_failure_carries_the_apt_hint(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost()
    host.failures[INSTALL] = ProcessOutput(
        100, "", "E: Could not get lock /var/lib/dpkg/lock-frontend\n"
    )
    report, _ = _run(tmp_path, config, host)
    failure = report.failure
    assert (failure.phase, failure.message) == (Phase.APPLY, "apply() raised UnexpectedExitError")
    assert APT_FAILURE_HINT in failure.next_action
    assert "dpkg --audit" in failure.next_action
    assert host.calls[-1] == INSTALL  # no retry, no recovery command


def test_add_apt_repository_failure_carries_its_hint(config: dict, tmp_path: Path) -> None:
    host = FakeAptHost()
    host.failures[ADD_REPO] = ProcessOutput(1, "", "E: Failed to fetch ...\n")
    report, _ = _run(tmp_path, config, host)
    assert report.failure.phase is Phase.APPLY
    assert ADD_APT_REPOSITORY_FAILURE_HINT in report.failure.next_action
    assert INSTALL not in host.calls


def test_mutating_commands_are_refused_under_a_non_host_root(config, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = FakeAptHost()
    report, _ = _run(tmp_path, config, host, root=root)
    assert report.failure.phase is Phase.APPLY
    assert report.failure.message == f"apply() raised {MutationRefusedError.__name__}"
    assert ADD_REPO not in host.calls and INSTALL not in host.calls


# --- verify() ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "enabled", "expected"),
    [
        (
            "noble",
            False,
            "the repository cloud-archive:epoxy is not enabled: 'apt-cache policy "
            "python3-openstackclient' lists no apt source with suite '<series>-updates/epoxy'",
        ),
        (
            None,
            True,
            "python3-openstackclient is not installed "
            "(dpkg-query --status does not report 'install ok installed')",
        ),
    ],
)
def test_verify_names_the_part_that_failed(config, tmp_path, client, enabled, expected) -> None:
    result = _step(tmp_path, config, FakeAptHost(client=client, pocket_enabled=enabled)).verify()
    assert result.ok is False
    assert result.details == f"{expected}. {UNCONFIRMED}"


def test_verify_reports_both_failures(config: dict, tmp_path: Path) -> None:
    result = _step(tmp_path, config, FakeAptHost()).verify()
    assert not result.ok
    assert "is not installed" in result.details and "is not enabled" in result.details


def test_verify_success_says_the_repository_check_is_unconfirmed(config, tmp_path) -> None:
    result = _step(tmp_path, config, FakeAptHost(client="uca", pocket_enabled=True)).verify()
    assert result.ok
    assert result.details == (
        "python3-openstackclient is installed and the repository cloud-archive:epoxy is "
        "enabled (apt source: http://ubuntu-cloud.archive.canonical.com/ubuntu "
        f"noble-updates/epoxy/main). {UNCONFIRMED}"
    )


# --- registry, catalog and roles --------------------------------------------------------------


def test_step_is_registered_and_still_unverified() -> None:
    assert "openstack-packages" in IMPLEMENTED_STEPS
    step = load_catalog().step("openstack-packages")
    assert step.status == "unverified"
    assert list(step.roles) == ["controller", "compute", "block", "object"]


@pytest.mark.parametrize("role", ["controller", "compute", "block", "object"])
def test_step_builds_for_every_role(config: dict, tmp_path: Path, role: str) -> None:
    step = build_step(
        "openstack-packages", tmp_path, config, _node(config, role),
        executor=_executor(tmp_path, FakeAptHost()),
    )  # fmt: skip
    assert step.pocket == "cloud-archive:epoxy" and step.declares_commands


# --- CLI ----------------------------------------------------------------------------------------


def _apply(root: Path, node: str = "forge-controller-01", *extra: str) -> list[str]:
    return [
        "apply", "--config", str(EXAMPLE_CONFIG), "--step", "openstack-packages",
        "--node", node, "--root", str(root), *extra,
    ]  # fmt: skip


@pytest.mark.parametrize("node", ["forge-controller-01", "forge-compute-01", "forge-object-01"])
def test_cli_dry_run_shows_the_exact_argv_of_every_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], node: str
) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    rc = main(_apply(root, node))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: Step 'openstack-packages' is marked UNVERIFIED" in out
    assert "no files modified and no commands run" in out
    assert (
        "Commands apply() would run, in order, if check() finds the step not done:\n"
        "  /usr/bin/add-apt-repository --yes --cloud cloud-archive:epoxy\n"
        "  /usr/bin/apt-get install --yes python3-openstackclient\n"
    ) in out
    assert (
        "Read-only commands check(), apply() and verify() may run (not run in a dry run):\n"
        "  /usr/bin/dpkg-query --status software-properties-common\n"
        "  /usr/bin/dpkg-query --status python3-openstackclient\n"
        "  /usr/bin/apt-cache policy python3-openstackclient\n"
    ) in out
    assert "comes from the supported release table" in out
    assert "no separate apt-get update is run" in out
    assert "download from the network" in out
    assert "upgrade" not in out.replace("No upgrade, dist-upgrade", "")
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()


def test_cli_yes_with_a_non_host_root_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    rc = main(_apply(root, "forge-compute-01", "--yes"))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Step 'openstack-packages' runs system commands" in captured.err
    assert "--yes with --root other than '/' is refused" in captured.err
    assert not (root / DEFAULT_STATE_TARGET.lstrip("/")).exists()
    assert not (root / DEFAULT_AUDIT_TARGET.lstrip("/")).exists()
