"""Supported Ubuntu / OpenStack release pairs.

Supporting a new pair is a one-line change to ``SUPPORTED_RELEASES``; the schema only
checks the shape of the version strings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SupportedRelease:
    """An OpenStack release installable on an Ubuntu release from the Ubuntu Cloud Archive."""

    ubuntu_version: str
    openstack_release: str
    codename: str

    @property
    def cloud_archive(self) -> str:
        """The apt repository to enable, as passed to ``add-apt-repository``."""
        return f"cloud-archive:{self.codename}"


SUPPORTED_RELEASES: tuple[SupportedRelease, ...] = (
    SupportedRelease(ubuntu_version="24.04", openstack_release="2025.1", codename="epoxy"),
)


def find_release(ubuntu_version: str, openstack_release: str) -> SupportedRelease | None:
    """Return the supported pair matching both versions, or None."""
    for release in SUPPORTED_RELEASES:
        if (release.ubuntu_version, release.openstack_release) == (
            ubuntu_version,
            openstack_release,
        ):
            return release
    return None
