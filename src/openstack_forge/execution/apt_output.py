"""Pure parsers for the output of read-only package commands (no process, no I/O).

- :func:`parse_dpkg_status` reads ``dpkg-query --status <package>``.
- :func:`parse_apt_policy` reads ``apt-cache policy <package>`` (run with ``LC_ALL=C``).
- :func:`cloud_archive_sources` finds the apt sources of an Ubuntu Cloud Archive pocket in
  a parsed policy.
- :func:`installed_from` says whether the installed version is one of those sources'
  versions (enabling the pocket does not replace a version installed from elsewhere).

The ``apt-cache policy`` format was taken from real output read on an Ubuntu 24.04
machine (apt 2.8); the test fixtures in ``tests/fixtures/apt/`` are synthetic, modeled on
it. For one package it prints the package name, ``Installed:`` and ``Candidate:``
(``(none)`` when absent), then a ``Version table:`` where each version line
(`` *** <version> <priority>`` for the installed one, five spaces otherwise) is followed by
its sources, one per line::

    <priority> <uri> <suite>/<component> <arch> Packages
    500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages
    100 /var/lib/dpkg/status

The last line is the local status file, not a source. The Ubuntu Cloud Archive suite is
expected to be ``<series>-<pocket>/<name>``, e.g. ``noble-updates/epoxy`` with component
``main``. **That naming is an assumption that has not been confirmed on a host where
OpenStack Forge ran the step**; see :data:`REPOSITORY_CHECK_UNCONFIRMED`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from openstack_forge.execution.commands import validate_parameter

# add-apt-repository(1), --cloud: "If not specified, the pocket defaults to -updates."
DEFAULT_CLOUD_ARCHIVE_POCKET = "updates"

REPOSITORY_CHECK_UNCONFIRMED = (
    "The repository check is unconfirmed on a real host: it parses 'apt-cache policy' and "
    "assumes the Ubuntu Cloud Archive suite is named '{suite}'."
)

_VERSION_LINE_RE = re.compile(r"^ (?:\*\*\*|   ) (\S+) (-?[0-9]+)$")
_SOURCE_LINE_RE = re.compile(r"^ +(-?[0-9]+) (\S+) (\S+)/([^/\s]+) (\S+) Packages$")
_FIELD_RE = re.compile(r"^  (Installed|Candidate): (\S+)$")


def parse_dpkg_status(text: str) -> bool:
    """True if a ``dpkg-query --status`` entry says ``Status: install ok installed``."""
    for line in text.splitlines():
        if line.startswith("Status:"):
            words = line.split(":", 1)[1].split()
            return len(words) == 3 and words[1] == "ok" and words[2] == "installed"
    return False


@dataclass(frozen=True)
class AptSource:
    """One apt source line under a version in ``apt-cache policy`` output."""

    version: str
    priority: int
    uri: str
    suite: str
    component: str
    architecture: str


@dataclass(frozen=True)
class AptPolicy:
    """The parts of ``apt-cache policy <package>`` output used by the steps."""

    package: str | None
    installed: str | None
    candidate: str | None
    sources: tuple[AptSource, ...]


def parse_apt_policy(text: str) -> AptPolicy:
    """Parse ``apt-cache policy <one package>``; unknown lines are ignored.

    Empty output (what apt-cache prints on stdout for an unknown package) gives a policy
    with no package and no sources. ``(none)`` becomes ``None``. The local status line
    (``100 /var/lib/dpkg/status``) is not a source.
    """
    package: str | None = None
    fields: dict[str, str | None] = {"Installed": None, "Candidate": None}
    sources: list[AptSource] = []
    version: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if package is None and line.endswith(":") and not line.startswith(" "):
            package = line[:-1]
            continue
        if field := _FIELD_RE.match(line):
            fields[field[1]] = None if field[2] == "(none)" else field[2]
            continue
        if (source := _SOURCE_LINE_RE.match(line)) and version is not None:
            priority, uri, suite, component, architecture = source.groups()
            sources.append(AptSource(version, int(priority), uri, suite, component, architecture))
            continue
        if entry := _VERSION_LINE_RE.match(line):
            version = entry[1]
    return AptPolicy(package, fields["Installed"], fields["Candidate"], tuple(sources))


def cloud_archive_suite(pocket: str) -> tuple[str, str]:
    """``(name, apt pocket)`` of a ``cloud-archive:<name>[-updates|-proposed]`` pocket."""
    validate_parameter("cloud_archive_pocket", "pocket", pocket)
    name, _, apt_pocket = pocket.split(":", 1)[1].partition("-")
    return name, apt_pocket or DEFAULT_CLOUD_ARCHIVE_POCKET


def describe_cloud_archive_suite(pocket: str) -> str:
    """The assumed suite, e.g. ``<series>-updates/epoxy`` for ``cloud-archive:epoxy``."""
    name, apt_pocket = cloud_archive_suite(pocket)
    return f"<series>-{apt_pocket}/{name}"


def cloud_archive_sources(policy: AptPolicy, pocket: str) -> tuple[AptSource, ...]:
    """The sources in ``policy`` whose suite is ``<series>-<apt pocket>/<name>``.

    Only the suite is compared, not the URI, so a mirror or proxy of the Ubuntu Cloud
    Archive counts; any Ubuntu series name is accepted.
    """
    name, apt_pocket = cloud_archive_suite(pocket)
    suite = re.compile(rf"[a-z]+-{re.escape(apt_pocket)}/{re.escape(name)}")
    return tuple(source for source in policy.sources if suite.fullmatch(source.suite))


def installed_from(policy: AptPolicy, sources: Sequence[AptSource]) -> bool:
    """True if ``policy.installed`` is the version listed under one of ``sources``.

    A pocket being enabled is not enough: a version installed earlier from another
    archive (for example Ubuntu's own ``noble/main``) stays installed until apt installs
    the pocket's version. Versions are compared as exact strings, as apt prints them.
    """
    return policy.installed is not None and any(
        source.version == policy.installed for source in sources
    )
