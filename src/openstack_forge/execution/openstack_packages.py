"""The ``openstack-packages`` step: enable the Ubuntu Cloud Archive, install the client.

Follows the official page
(https://docs.openstack.org/install-guide/environment-packages-ubuntu.html), which for
OpenStack 2025.1 on Ubuntu 24.04 says, on all nodes that run OpenStack services:

1. ``add-apt-repository cloud-archive:epoxy``;
2. ``apt install python3-openstackclient``.

The pocket is never hardcoded: it is the ``cloud_archive`` of the configuration's entry
in the supported release table. This step runs ``add-apt-repository --yes --cloud <pocket>``
(the man page's non-deprecated form of the guide's command), which refreshes the package
lists itself, then ``apt-get install --yes python3-openstackclient``. It runs no upgrade,
dist-upgrade or reboot and asks apt for no other package (apt still installs the client's
dependencies). ``add-apt-repository`` comes from ``software-properties-common``; if that
package is missing, ``apply()`` fails and says so instead of installing it.

Both commands download from the network through apt; the Python code opens no connection.

The step is done only if the client is installed, the pocket is enabled AND the installed
version is one the pocket provides: a client installed from Ubuntu's own ``noble/main``
before the pocket was enabled (or left there because ``apply()`` stopped after
``add-apt-repository``) does not count. ``apt-get install`` of an already-installed package
installs the newest candidate of that package only (apt-get(8), ``install``), so running
the step again replaces it.

The step is UNVERIFIED and has never run on a real host. The repository check relies on
the Ubuntu Cloud Archive suite naming seen in ``apt-cache policy`` output, which is an
assumption not yet confirmed on a host where this step ran (see
:mod:`openstack_forge.execution.apt_output`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from openstack_forge.config import find_release
from openstack_forge.execution.apt_output import (
    REPOSITORY_CHECK_UNCONFIRMED,
    AptPolicy,
    AptSource,
    cloud_archive_sources,
    describe_cloud_archive_suite,
    installed_from,
    parse_apt_policy,
    parse_dpkg_status,
)
from openstack_forge.execution.commands import (
    ADD_APT_REPOSITORY_CLOUD_ARCHIVE,
    APT_CACHE_POLICY,
    APT_GET_INSTALL,
    DPKG_QUERY_STATUS,
    CommandExecutor,
    validate_parameter,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import ExecutionError, PreconditionError
from openstack_forge.execution.preview import PlannedCommand, StepPreview

OPENSTACK_PACKAGES_STEP_ID = "openstack-packages"
OPENSTACK_CLIENT_PACKAGE = "python3-openstackclient"
# dpkg -S /usr/bin/add-apt-repository -> software-properties-common
SOFTWARE_PROPERTIES_PACKAGE = "software-properties-common"
ADD_APT_REPOSITORY_PATH = "/usr/bin/add-apt-repository"

INSTALL_CLIENT = PlannedCommand(APT_GET_INSTALL, {"package": OPENSTACK_CLIENT_PACKAGE})
QUERY_CLIENT_PACKAGE = PlannedCommand(DPKG_QUERY_STATUS, {"package": OPENSTACK_CLIENT_PACKAGE})
QUERY_CLIENT_POLICY = PlannedCommand(APT_CACHE_POLICY, {"package": OPENSTACK_CLIENT_PACKAGE})
QUERY_SOFTWARE_PROPERTIES = PlannedCommand(
    DPKG_QUERY_STATUS, {"package": SOFTWARE_PROPERTIES_PACKAGE}
)


def cloud_archive_pocket(config: Mapping[str, Any]) -> str:
    """The pocket to enable, from the supported release table (never hardcoded)."""
    cluster = config["cluster"]
    release = find_release(cluster["ubuntu_version"], cluster["openstack_release"])
    if release is None:
        raise ExecutionError(
            f"Ubuntu {cluster['ubuntu_version']} with OpenStack {cluster['openstack_release']} "
            "is not in the supported release table, so there is no Ubuntu Cloud Archive "
            "pocket to enable"
        )
    return validate_parameter("cloud_archive_pocket", "pocket", release.cloud_archive)


@dataclass(frozen=True)
class OpenStackPackagesStep:
    """Executable ``openstack-packages`` step (commands via ``executor``; no file edit)."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = OPENSTACK_PACKAGES_STEP_ID
    pocket: str = field(init=False, default="")
    declares_commands: ClassVar[bool] = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "pocket", cloud_archive_pocket(self.config))

    @property
    def add_repository(self) -> PlannedCommand:
        return PlannedCommand(ADD_APT_REPOSITORY_CLOUD_ARCHIVE, {"pocket": self.pocket})

    def dry_run_preview(self) -> StepPreview:
        """The commands apply()/check()/verify() would run; nothing is evaluated."""
        cluster = self.config["cluster"]
        return StepPreview(
            commands=(self.add_repository, INSTALL_CLIENT),
            read_only_commands=(
                QUERY_SOFTWARE_PROPERTIES,
                QUERY_CLIENT_PACKAGE,
                QUERY_CLIENT_POLICY,
            ),
            notes=(
                f"The pocket {self.pocket} comes from the supported release table "
                f"(Ubuntu {cluster['ubuntu_version']}, OpenStack {cluster['openstack_release']}).",
                f"Before add-apt-repository, apply() checks with dpkg-query that "
                f"{SOFTWARE_PROPERTIES_PACKAGE} (which provides {ADD_APT_REPOSITORY_PATH}) is "
                "installed; if it is not, the step fails and does not install it.",
                "add-apt-repository updates the package lists itself (add-apt-repository(1): "
                "--no-update is not used), so no separate apt-get update is run.",
                "add-apt-repository and apt-get download from the network (the Ubuntu Cloud "
                "Archive and the configured apt sources); OpenStack Forge's Python code opens "
                "no connection itself.",
                "No upgrade, dist-upgrade or reboot is run, and no other package is requested "
                f"(apt-get may install dependencies of {OPENSTACK_CLIENT_PACKAGE}).",
                f"check() and verify() also require the installed {OPENSTACK_CLIENT_PACKAGE} "
                f"version to be one listed for {self.pocket} by apt-cache policy; a version "
                "installed from Ubuntu's own archive is replaced by apt-get install, which "
                "installs the newest candidate of that one package.",
            ),
        )

    def check(self) -> bool:
        """True if the client is installed, the pocket is enabled, and the installed
        version comes from the pocket.

        The package alone is not enough: Ubuntu 24.04's own archive also ships it. An
        enabled pocket is not enough either: a client installed from ``noble/main`` stays
        at that version until ``apt-get install`` replaces it.
        """
        if not self._client_installed():
            return False
        policy, sources = self._pocket_policy()
        return bool(sources) and installed_from(policy, sources)

    def apply(self) -> None:
        """Require software-properties-common, enable the pocket, install the client."""
        self._require_software_properties()
        self.executor.run(self.add_repository.command_id, self.add_repository.params)
        # No apt-get update here: add-apt-repository has just updated the package lists.
        self.executor.run(INSTALL_CLIENT.command_id, INSTALL_CLIENT.params)

    def verify(self) -> VerifyResult:
        """The same three checks as check(), each reported separately."""
        suite = describe_cloud_archive_suite(self.pocket)
        unconfirmed = REPOSITORY_CHECK_UNCONFIRMED.format(suite=suite)
        problems: list[str] = []
        installed = self._client_installed()
        if not installed:
            problems.append(
                f"{OPENSTACK_CLIENT_PACKAGE} is not installed "
                "(dpkg-query --status does not report 'install ok installed')"
            )
        policy, sources = self._pocket_policy()
        if not sources:
            problems.append(
                f"the repository {self.pocket} is not enabled: 'apt-cache policy "
                f"{OPENSTACK_CLIENT_PACKAGE}' lists no apt source with suite '{suite}'"
            )
        elif installed and not installed_from(policy, sources):
            problems.append(self._version_mismatch(policy, sources))
        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {unconfirmed}")
        listed = ", ".join(
            f"{s.uri} {s.suite}/{s.component}" for s in sources if s.version == policy.installed
        )
        return VerifyResult(
            True,
            f"{OPENSTACK_CLIENT_PACKAGE} {policy.installed} is installed from the repository "
            f"{self.pocket}, which is enabled (apt source: {listed}). {unconfirmed}",
        )

    def _client_installed(self) -> bool:
        result = self.executor.run(QUERY_CLIENT_PACKAGE.command_id, QUERY_CLIENT_PACKAGE.params)
        return result.returncode == 0 and parse_dpkg_status(result.stdout)

    def _pocket_policy(self) -> tuple[AptPolicy, tuple[AptSource, ...]]:
        result = self.executor.run(QUERY_CLIENT_POLICY.command_id, QUERY_CLIENT_POLICY.params)
        policy = parse_apt_policy(result.stdout)
        return policy, cloud_archive_sources(policy, self.pocket)

    def _version_mismatch(self, policy: AptPolicy, sources: tuple[AptSource, ...]) -> str:
        offered = ", ".join(
            dict.fromkeys(f"{s.version} ({s.suite}/{s.component})" for s in sources)
        )
        if policy.installed is None:
            return (
                f"dpkg-query reports {OPENSTACK_CLIENT_PACKAGE} installed, but 'apt-cache "
                f"policy {OPENSTACK_CLIENT_PACKAGE}' reports no installed version; the "
                f"repository {self.pocket} offers {offered}"
            )
        return (
            f"the installed {OPENSTACK_CLIENT_PACKAGE} {policy.installed} does not come from "
            f"the repository {self.pocket}, which offers {offered}; it was installed from "
            f"another source (see 'apt-cache policy {OPENSTACK_CLIENT_PACKAGE}')"
        )

    def _require_software_properties(self) -> None:
        result = self.executor.run(
            QUERY_SOFTWARE_PROPERTIES.command_id, QUERY_SOFTWARE_PROPERTIES.params
        )
        if not (result.returncode == 0 and parse_dpkg_status(result.stdout)):
            raise PreconditionError(
                f"The package {SOFTWARE_PROPERTIES_PACKAGE} is not installed. It provides "
                f"{ADD_APT_REPOSITORY_PATH}, which this step needs to enable {self.pocket}. "
                f"Install {SOFTWARE_PROPERTIES_PACKAGE} yourself, then run the step again; "
                "OpenStack Forge does not install it."
            )


def make_openstack_packages_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> OpenStackPackagesStep:
    return OpenStackPackagesStep(
        root=Path(root), config=config, local_node=local_node, executor=executor
    )
