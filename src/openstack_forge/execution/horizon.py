"""The ``horizon`` deployment step (Dashboard service on the controller node).

**Unverified and never run on a real host.** Every command goes through
:class:`CommandExecutor` and :data:`DEFAULT_REGISTRY`; the unit tests use an injectable fake
backend and a temporary root directory, so nothing runs on the development machine.

Official 2025.1 (Epoxy) Ubuntu installation guide read directly from the ``stable/2025.1``
branch of ``openstack/horizon``:

- **Horizon for Ubuntu**:
  ``https://docs.openstack.org/horizon/2025.1/install/install-ubuntu.html``
  (raw ``doc/source/install/install-ubuntu.rst``)

Exact steps prescribed by the official page
-------------------------------------------

1. **Install the package**:

   - ``apt install openstack-dashboard``

2. **Edit ``/etc/openstack-dashboard/local_settings.py``**:

   - ``OPENSTACK_HOST = "controller"``
   - ``ALLOWED_HOSTS = ['one.example.com', 'two.example.com']``
     (Note: *"Do not edit the ALLOWED_HOSTS parameter under the Ubuntu configuration
     section."*)
   - Configure ``memcached`` session storage:
     ``SESSION_ENGINE = 'django.contrib.sessions.backends.cache'`` and
     ``CACHES = {'default': {'BACKEND':
     'django.core.cache.backends.memcached.PyMemcacheCache', 'LOCATION':
     'controller:11211'}}``
   - Enable Identity API v3:
     ``OPENSTACK_KEYSTONE_URL = "http://%s/identity/v3" % OPENSTACK_HOST``
     (with note: *"In case your keystone run at 5000 port then you would mentioned keystone
     port here as well"* — see deviation 1 below for why OpenStack Forge sets
     ``"http://%s:5000/v3" % OPENSTACK_HOST``)
   - Enable support for domains:
     ``OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT = True``
   - Configure API versions:
     ``OPENSTACK_API_VERSIONS = {"identity": 3, "image": 2, "volume": 3}``
   - Configure ``Default`` as the default domain for users created via the dashboard:
     ``OPENSTACK_KEYSTONE_DEFAULT_DOMAIN = "Default"``
   - Configure ``member`` as the default role for users created via the dashboard:
     ``OPENSTACK_KEYSTONE_DEFAULT_ROLE = "member"`` (already set in Ubuntu's default
     ``local_settings.py``, reaffirmed in the managed block so later overrides cannot drift)
   - Configure time zone: ``TIME_ZONE = "UTC"``

3. **Ensure ``WSGIApplicationGroup %{GLOBAL}`` in
   ``/etc/apache2/conf-available/openstack-dashboard.conf``**:

   - *"Add the following line to /etc/apache2/conf-available/openstack-dashboard.conf if not
     included: ``WSGIApplicationGroup %{GLOBAL}``"*

4. **Finalize installation**:

   - ``systemctl reload apache2.service``

Ubuntu 24.04 + Cloud Archive (Epoxy) package and conffile facts
---------------------------------------------------------------

Inspected read-only on the Ubuntu 24.04 development machine:

- ``apt-cache policy openstack-dashboard``: ``4:25.3.0-0ubuntu1~cloud2`` in
  ``noble-updates/epoxy/main`` (``4:25.3.0-0ubuntu1~cloud0`` installed;
  ``4:24.0.1-0ubuntu0.24.04.2`` in ``noble-updates/main``).
- ``apt-cache show openstack-dashboard``:
  ``Depends: adduser, apache2 | httpd, libapache2-mod-wsgi-py3 (>= 2.3),
  python3-django-horizon (= 4:25.3.0-0ubuntu1~cloud2)``.
- ``/etc/apache2/conf-available/openstack-dashboard.conf`` (conffile MD5
  ``76f61fe386d39513959d77629493d20a``) already ships ``WSGIApplicationGroup %{GLOBAL}`` at
  line 4:

  .. code-block:: apache

     WSGIScriptAlias /horizon /usr/share/openstack-dashboard/openstack_dashboard/wsgi.py \\
         process-group=horizon
     WSGIDaemonProcess horizon user=horizon group=horizon processes=3 threads=10 \\
         display-name=%{GROUP}
     WSGIProcessGroup horizon
     WSGIApplicationGroup %{GLOBAL}

- ``/var/lib/dpkg/info/openstack-dashboard.postinst`` sets ``/etc/openstack-dashboard`` to
  ``0750`` ``root:horizon`` and files in ``/etc/openstack-dashboard`` to ``0640``
  ``root:horizon``, then enables ``openstack-dashboard.conf`` in Apache via
  ``apache2_invoke enconf openstack-dashboard.conf``.
- ``/usr/lib/python3/dist-packages/openstack_dashboard/local/local_settings.py.example``
  (copied to ``/etc/openstack-dashboard/local_settings.py``) ends with an
  ``# Ubuntu Settings`` section that sets ``DEFAULT_THEME = 'ubuntu'``,
  ``WEBROOT = '/horizon/'``, ``ALLOWED_HOSTS = ['*']``, and ``COMPRESS_OFFLINE = True``.

Deviations from the guide and design decisions
----------------------------------------------

1. **``OPENSTACK_KEYSTONE_URL = "http://%s:5000/v3" % OPENSTACK_HOST``**:
   The Horizon guide writes ``"http://%s/identity/v3" % OPENSTACK_HOST`` (a DevStack path) and
   its note writes ``"http://%s:5000/identity/v3" % OPENSTACK_HOST``. However, the Keystone
   2025.1 Ubuntu guide (``keystone-install-ubuntu.html``, implemented in ``keystone.py``)
   bootstraps Keystone endpoints at ``http://controller:5000/v3/`` without an ``/identity``
   prefix. Therefore OpenStack Forge sets
   ``OPENSTACK_KEYSTONE_URL = "http://%s:5000/v3" % OPENSTACK_HOST``.
2. **Idempotent ``managed_block`` at the end of ``/etc/openstack-dashboard/local_settings.py``**:
   Because ``local_settings.py`` is executed top-to-bottom as a Python module and Ubuntu's
   packaging appends ``ALLOWED_HOSTS = ['*']`` at the very bottom under ``# Ubuntu Settings``,
   editing earlier assignments in place would still leave ``ALLOWED_HOSTS`` overridden by the
   trailing ``ALLOWED_HOSTS = ['*']`` line. Appending a ``# BEGIN horizon`` ... ``# END horizon``
   managed block at the end of ``local_settings.py`` cleanly and idempotently overrides
   ``OPENSTACK_HOST``, ``ALLOWED_HOSTS``, ``SESSION_ENGINE``, ``CACHES``,
   ``OPENSTACK_KEYSTONE_URL``, ``OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT``,
   ``OPENSTACK_API_VERSIONS``, ``OPENSTACK_KEYSTONE_DEFAULT_DOMAIN``,
   ``OPENSTACK_KEYSTONE_DEFAULT_ROLE``, and ``TIME_ZONE`` without fragile multi-line regex
   surgery across Python dictionaries.
3. **Explicit ``ALLOWED_HOSTS`` list instead of ``['*']``**:
   Instead of leaving Ubuntu's ``ALLOWED_HOSTS = ['*']`` wildcard active, OpenStack Forge sets
   ``ALLOWED_HOSTS`` to the deduplicated list of ``"controller"``, the controller node's
   ``hostname``, ``fqdn``, and ``management_ip``, ``"localhost"``, and ``"127.0.0.1"``.
4. **``OPENSTACK_NEUTRON_NETWORK`` left unrestricted (Networking Option 2)**:
   The guide only disables ``enable_router`` / ``enable_quotas`` / ``enable_distributed_router``
   / ``enable_ha_router`` when Networking Option 1 (Provider networks only) is chosen. Because
   OpenStack Forge deploys Networking Option 2 (Self-service networks with L3 router support),
   ``OPENSTACK_NEUTRON_NETWORK`` is not restricted.
5. **Conditional edit of ``/etc/apache2/conf-available/openstack-dashboard.conf``**:
   Following *"Add the following line to /etc/apache2/conf-available/openstack-dashboard.conf
   if not included: ``WSGIApplicationGroup %{GLOBAL}``"*, OpenStack Forge inspects the file
   first: when an active ``WSGIApplicationGroup %{GLOBAL}`` directive is already present (as in
   Ubuntu 24.04's default conffile), it returns ``changed=False`` and writes nothing; only if
   the directive is missing does it append a ``# BEGIN horizon-wsgi`` managed block.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from openstack_forge.execution.commands import (
    APT_GET_INSTALL,
    SYSTEMCTL_RELOAD,
    CommandExecutor,
)
from openstack_forge.execution.engine import VerifyResult
from openstack_forge.execution.errors import (
    ExecutionError,
    FileFormatError,
    PreconditionError,
    UnsafePathError,
)
from openstack_forge.execution.files import FileChange, managed_block
from openstack_forge.execution.fsutil import read_text_file, resolve_target
from openstack_forge.execution.preview import PlannedCommand, StepPreview
from openstack_forge.execution.service_checks import (
    package_installed,
    query_active,
    query_package,
    unit_active_problems,
)

HORIZON_STEP_ID = "horizon"
HORIZON_PACKAGE = "openstack-dashboard"
APACHE_UNIT = "apache2"

HORIZON_LOCAL_SETTINGS_TARGET = "/etc/openstack-dashboard/local_settings.py"
HORIZON_APACHE_CONF_TARGET = "/etc/apache2/conf-available/openstack-dashboard.conf"

HORIZON_BLOCK_MARKER = "horizon"
HORIZON_WSGI_BLOCK_MARKER = "horizon-wsgi"
HORIZON_WSGI_APPLICATION_GROUP_LINE = "WSGIApplicationGroup %{GLOBAL}"

HORIZON_OPENSTACK_HOST = "controller"
HORIZON_MEMCACHED_LOCATION = "controller:11211"
HORIZON_SESSION_ENGINE = "django.contrib.sessions.backends.cache"
HORIZON_CACHE_BACKEND = "django.core.cache.backends.memcached.PyMemcacheCache"
HORIZON_KEYSTONE_URL_EXPR = '"http://%s:5000/v3" % OPENSTACK_HOST'
HORIZON_DEFAULT_DOMAIN = "Default"
HORIZON_DEFAULT_ROLE = "member"
HORIZON_TIME_ZONE = "UTC"

INSTALL_HORIZON_PACKAGE = PlannedCommand(APT_GET_INSTALL, {"package": HORIZON_PACKAGE})
RELOAD_APACHE_UNIT = PlannedCommand(SYSTEMCTL_RELOAD, {"unit": APACHE_UNIT})
QUERY_HORIZON_PACKAGE = query_package(HORIZON_PACKAGE)
QUERY_APACHE_ACTIVE = query_active(APACHE_UNIT)

HORIZON_KEYSTONE_URL_NOTE = (
    'Keystone endpoint URL: sets OPENSTACK_KEYSTONE_URL = "http://%s:5000/v3" % OPENSTACK_HOST '
    "(without '/identity') to match the Keystone v3 endpoints (http://controller:5000/v3/) "
    "bootstrapped by the 'keystone' step."
)
HORIZON_NOT_CHECKED = "Not checked: no live HTTP request to http://controller/horizon is performed."


def _read_existing_conf(path: Path, display: str) -> str | None:
    """Read an existing config file, returning ``None`` if neither it nor its parent dir exists."""
    if not path.is_symlink() and not path.exists():
        return None
    return read_text_file(path, display)


def _require_conf_exists(root: Path, target: str, package_hint: str) -> None:
    path, display = resolve_target(root, target)
    if _read_existing_conf(path, display) is None:
        raise PreconditionError(
            f"{display} does not exist (expected from package {package_hint}; "
            "OpenStack Forge will not create a stub configuration file)"
        )


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def horizon_allowed_hosts(local_node: Mapping[str, Any]) -> tuple[str, ...]:
    """Build the deduplicated ``ALLOWED_HOSTS`` tuple for ``local_settings.py``."""
    hostname = local_node.get("hostname")
    fqdn = local_node.get("fqdn")
    mgmt_ip_raw = local_node.get("management_ip")
    if not isinstance(hostname, str) or not hostname.strip():
        raise PreconditionError("Controller node is missing a non-empty 'hostname'")
    if not _HOSTNAME_RE.fullmatch(hostname.strip()):
        raise PreconditionError(f"Controller node has an invalid 'hostname': {hostname!r}")
    if not isinstance(fqdn, str) or not fqdn.strip():
        raise PreconditionError("Controller node is missing a non-empty 'fqdn'")
    if not _FQDN_RE.fullmatch(fqdn.strip()):
        raise PreconditionError(f"Controller node has an invalid 'fqdn': {fqdn!r}")
    try:
        mgmt_ip = str(IPv4Address(mgmt_ip_raw))
    except Exception as exc:
        raise PreconditionError(
            f"Controller node has an invalid 'management_ip': {mgmt_ip_raw!r}"
        ) from exc

    candidates = (
        HORIZON_OPENSTACK_HOST,
        hostname.strip(),
        fqdn.strip(),
        mgmt_ip,
        "localhost",
        "127.0.0.1",
    )
    seen: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def build_horizon_local_settings_block(local_node: Mapping[str, Any]) -> str:
    """Render the Python settings block appended to ``local_settings.py``."""
    allowed_hosts = horizon_allowed_hosts(local_node)
    hosts_lines = ",\n".join(f'    "{host}"' for host in allowed_hosts)
    return (
        f'OPENSTACK_HOST = "{HORIZON_OPENSTACK_HOST}"\n'
        f"ALLOWED_HOSTS = [\n"
        f"{hosts_lines},\n"
        f"]\n"
        f'SESSION_ENGINE = "{HORIZON_SESSION_ENGINE}"\n'
        f"CACHES = {{\n"
        f'    "default": {{\n'
        f'        "BACKEND": "{HORIZON_CACHE_BACKEND}",\n'
        f'        "LOCATION": "{HORIZON_MEMCACHED_LOCATION}",\n'
        f"    }}\n"
        f"}}\n"
        f"OPENSTACK_KEYSTONE_URL = {HORIZON_KEYSTONE_URL_EXPR}\n"
        f"OPENSTACK_KEYSTONE_MULTIDOMAIN_SUPPORT = True\n"
        f"OPENSTACK_API_VERSIONS = {{\n"
        f'    "identity": 3,\n'
        f'    "image": 2,\n'
        f'    "volume": 3,\n'
        f"}}\n"
        f'OPENSTACK_KEYSTONE_DEFAULT_DOMAIN = "{HORIZON_DEFAULT_DOMAIN}"\n'
        f'OPENSTACK_KEYSTONE_DEFAULT_ROLE = "{HORIZON_DEFAULT_ROLE}"\n'
        f'TIME_ZONE = "{HORIZON_TIME_ZONE}"\n'
    )


def edit_horizon_local_settings(
    root: Path,
    local_node: Mapping[str, Any],
    *,
    dry_run: bool,
) -> FileChange:
    """Apply the managed Horizon block to ``/etc/openstack-dashboard/local_settings.py``."""
    _require_conf_exists(root, HORIZON_LOCAL_SETTINGS_TARGET, HORIZON_PACKAGE)
    block = build_horizon_local_settings_block(local_node)
    return managed_block(
        root,
        HORIZON_LOCAL_SETTINGS_TARGET,
        HORIZON_BLOCK_MARKER,
        block,
        dry_run=dry_run,
    )


def verify_horizon_local_settings_text(
    text: str,
    local_node: Mapping[str, Any],
) -> list[str]:
    """Pure check: ``local_settings.py`` ends with (or contains) the expected managed block
    and no later line overrides ``ALLOWED_HOSTS`` or ``OPENSTACK_HOST``."""
    expected_block = build_horizon_local_settings_block(local_node)
    begin_marker = f"# BEGIN {HORIZON_BLOCK_MARKER}"
    end_marker = f"# END {HORIZON_BLOCK_MARKER}"
    full_expected = f"{begin_marker}\n{expected_block}{end_marker}"
    if full_expected not in text:
        return [
            f"{HORIZON_LOCAL_SETTINGS_TARGET}: missing or drifted "
            f"{begin_marker!r} .. {end_marker!r} block"
        ]
    # Ensure no active assignment after # END horizon overrides ALLOWED_HOSTS or OPENSTACK_HOST:
    _, after = text.split(full_expected, 1)
    problems: list[str] = []
    for raw_line in after.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("ALLOWED_HOSTS") or stripped.startswith("OPENSTACK_HOST"):
            problems.append(
                f"{HORIZON_LOCAL_SETTINGS_TARGET}: active assignment {stripped!r} appears "
                f"after {end_marker!r} and would override the managed block"
            )
    return problems


def has_wsgi_application_group_global(text: str) -> bool:
    """Return ``True`` if ``text`` has an active ``WSGIApplicationGroup %{GLOBAL}`` line."""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) == 2 and tokens[0] == "WSGIApplicationGroup" and tokens[1] == "%{GLOBAL}":
            return True
    return False


def edit_horizon_apache_conf(
    root: Path,
    *,
    dry_run: bool,
) -> FileChange:
    """Ensure ``WSGIApplicationGroup %{GLOBAL}`` is present in ``openstack-dashboard.conf``."""
    _require_conf_exists(root, HORIZON_APACHE_CONF_TARGET, HORIZON_PACKAGE)
    path, display = resolve_target(root, HORIZON_APACHE_CONF_TARGET)
    text = read_text_file(path, display)
    begin_marker = f"# BEGIN {HORIZON_WSGI_BLOCK_MARKER}"
    # If an active WSGIApplicationGroup %{GLOBAL} is already present outside our managed block
    # (as in Ubuntu 24.04's default openstack-dashboard.conf), leave the file untouched.
    if begin_marker not in text and has_wsgi_application_group_global(text):
        return FileChange(
            path=display,
            changed=False,
            diff="",
            backup=None,
            created=False,
        )
    return managed_block(
        root,
        HORIZON_APACHE_CONF_TARGET,
        HORIZON_WSGI_BLOCK_MARKER,
        f"{HORIZON_WSGI_APPLICATION_GROUP_LINE}\n",
        dry_run=dry_run,
    )


def verify_horizon_apache_conf_text(text: str) -> list[str]:
    """Pure check: ``/etc/apache2/conf-available/openstack-dashboard.conf`` contains an active
    ``WSGIApplicationGroup %{GLOBAL}`` directive."""
    if not has_wsgi_application_group_global(text):
        return [
            f"{HORIZON_APACHE_CONF_TARGET}: missing active "
            f"{HORIZON_WSGI_APPLICATION_GROUP_LINE!r} directive"
        ]
    return []


@dataclass(frozen=True)
class HorizonStep:
    """``check`` / ``apply`` / ``verify`` for the ``horizon`` catalog step."""

    root: Path
    config: Mapping[str, Any]
    local_node: Mapping[str, Any]
    executor: CommandExecutor
    id: str = HORIZON_STEP_ID
    declares_commands: bool = True

    def __post_init__(self) -> None:
        role = self.local_node.get("role")
        if role != "controller":
            raise ExecutionError(
                f"Step {self.id!r} applies only to the 'controller' role, got {role!r}"
            )
        horizon_allowed_hosts(self.local_node)

    def dry_run_preview(self) -> StepPreview:
        """File diffs + commands ``apply()`` would run."""
        notes: list[str] = []
        file_changes: list[FileChange] = []

        ls_path, ls_display = resolve_target(self.root, HORIZON_LOCAL_SETTINGS_TARGET)
        if _read_existing_conf(ls_path, ls_display) is None:
            block = build_horizon_local_settings_block(self.local_node)
            notes.append(
                f"{ls_display} does not exist yet (created when {HORIZON_PACKAGE} is installed); "
                f"apply() will append managed block {HORIZON_BLOCK_MARKER!r}:\n{block.rstrip()}"
            )
        else:
            file_changes.append(
                edit_horizon_local_settings(self.root, self.local_node, dry_run=True)
            )

        ap_path, ap_display = resolve_target(self.root, HORIZON_APACHE_CONF_TARGET)
        if _read_existing_conf(ap_path, ap_display) is None:
            notes.append(
                f"{ap_display} does not exist yet (created when {HORIZON_PACKAGE} is installed); "
                f"apply() will ensure {HORIZON_WSGI_APPLICATION_GROUP_LINE!r} is present."
            )
        else:
            file_changes.append(edit_horizon_apache_conf(self.root, dry_run=True))

        notes.extend((HORIZON_KEYSTONE_URL_NOTE, HORIZON_NOT_CHECKED))
        return StepPreview(
            file_changes=tuple(file_changes),
            commands=(INSTALL_HORIZON_PACKAGE, RELOAD_APACHE_UNIT),
            read_only_commands=(QUERY_HORIZON_PACKAGE, QUERY_APACHE_ACTIVE),
            notes=tuple(notes),
        )

    def check(self) -> bool:
        """True if both Horizon config files need no edit, ``openstack-dashboard`` is installed,
        and ``apache2`` is active."""
        try:
            if edit_horizon_local_settings(self.root, self.local_node, dry_run=True).changed:
                return False
            if edit_horizon_apache_conf(self.root, dry_run=True).changed:
                return False
        except (PreconditionError, UnsafePathError, FileFormatError):
            return False
        ls_path, ls_display = resolve_target(self.root, HORIZON_LOCAL_SETTINGS_TARGET)
        ls_text = _read_existing_conf(ls_path, ls_display)
        if ls_text is None or verify_horizon_local_settings_text(ls_text, self.local_node):
            return False
        ap_path, ap_display = resolve_target(self.root, HORIZON_APACHE_CONF_TARGET)
        ap_text = _read_existing_conf(ap_path, ap_display)
        if ap_text is None or verify_horizon_apache_conf_text(ap_text):
            return False
        if not package_installed(self.executor, HORIZON_PACKAGE):
            return False
        return not unit_active_problems(self.executor, APACHE_UNIT)

    def apply(self) -> None:
        """Install ``openstack-dashboard``, configure ``local_settings.py`` and
        ``openstack-dashboard.conf``, and reload ``apache2``."""
        self.executor.run(INSTALL_HORIZON_PACKAGE.command_id, INSTALL_HORIZON_PACKAGE.params)
        edit_horizon_local_settings(self.root, self.local_node, dry_run=False)
        edit_horizon_apache_conf(self.root, dry_run=False)
        self.executor.run(RELOAD_APACHE_UNIT.command_id, RELOAD_APACHE_UNIT.params)

    def verify(self) -> VerifyResult:
        """Verify ``local_settings.py``, ``openstack-dashboard.conf``, package, and ``apache2``."""
        problems: list[str] = []

        ls_path, ls_display = resolve_target(self.root, HORIZON_LOCAL_SETTINGS_TARGET)
        try:
            ls_text = _read_existing_conf(ls_path, ls_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {HORIZON_NOT_CHECKED}")
        if ls_text is None:
            problems.append(f"{ls_display} does not exist")
        else:
            problems += verify_horizon_local_settings_text(ls_text, self.local_node)

        ap_path, ap_display = resolve_target(self.root, HORIZON_APACHE_CONF_TARGET)
        try:
            ap_text = _read_existing_conf(ap_path, ap_display)
        except (UnsafePathError, FileFormatError) as exc:
            return VerifyResult(False, f"{exc}. {HORIZON_NOT_CHECKED}")
        if ap_text is None:
            problems.append(f"{ap_display} does not exist")
        else:
            problems += verify_horizon_apache_conf_text(ap_text)

        if not package_installed(self.executor, HORIZON_PACKAGE):
            problems.append(f"package {HORIZON_PACKAGE!r} is not installed")
        problems += unit_active_problems(self.executor, APACHE_UNIT)

        if problems:
            return VerifyResult(False, f"{'; '.join(problems)}. {HORIZON_NOT_CHECKED}")
        return VerifyResult(
            True,
            f"{HORIZON_LOCAL_SETTINGS_TARGET} and {HORIZON_APACHE_CONF_TARGET} match, "
            f"{HORIZON_PACKAGE} is installed, and {APACHE_UNIT} is active. "
            f"{HORIZON_NOT_CHECKED}",
        )


def make_horizon_step(
    root: Path,
    config: Mapping[str, Any],
    local_node: Mapping[str, Any],
    *,
    executor: CommandExecutor,
) -> HorizonStep:
    """Construct a :class:`HorizonStep` for ``openstack-forge apply``."""
    return HorizonStep(root=root, config=config, local_node=local_node, executor=executor)
