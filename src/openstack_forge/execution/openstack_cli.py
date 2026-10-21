"""Reusable ``/usr/bin/openstack`` CLI provisioning and verification helpers.

Official sources read directly for this module:
- ``https://docs.openstack.org/keystone/2025.1/install/keystone-users-ubuntu.html``
  (raw source: ``doc/source/install/common/keystone-users.inc`` on ``stable/2025.1`` in
  ``openstack/keystone``):
  - ``openstack project create --domain default --description "Service Project" service``
  - Service steps (Glance, Placement, Nova, Neutron, Cinder) subsequently run:
    - ``openstack user create --domain default --password-prompt <user>``
    - ``openstack role add --project service --user <user> admin``
    - ``openstack service create --name <service_name> --description "..." <service_type>``
    - ``openstack endpoint create --region RegionOne <service_name> public <url>``
    - ``openstack endpoint create --region RegionOne <service_name> internal <url>``
    - ``openstack endpoint create --region RegionOne <service_name> admin <url>``
- ``https://docs.openstack.org/keystone/2025.1/install/keystone-verify-ubuntu.html``
  (raw source: ``doc/source/install/keystone-verify-ubuntu.rst`` on ``stable/2025.1``):
  - Verifies live Keystone ``/v3`` operation by authenticating as ``admin`` against
    ``http://controller:5000/v3`` with ``--os-project-domain-name Default``,
    ``--os-user-domain-name Default``, ``--os-project-name admin``, and
    ``--os-username admin``.

Ubuntu 24.04 development machine facts (read-only inspection of
``python3-openstackclient`` ``7.4.0-0ubuntu1~cloud0``):
- ``dpkg -S /usr/bin/openstack`` -> ``python3-openstackclient: /usr/bin/openstack``.
- ``/usr/lib/python3/dist-packages/openstackclient/identity/v3/``:
  - ``project.py`` (``ListProject``, lines 250-310): default columns are
    ``('ID', 'Name')``; ``-f json`` outputs ``[{"ID": "<uuid>", "Name": "<name>"}]``.
  - ``user.py`` (``ListUser``, lines 475-504): default columns are ``['ID', 'Name']``;
    ``-f json`` outputs ``[{"ID": "<uuid>", "Name": "<name>"}]``.
  - ``user.py`` (``CreateUser``, lines 285-324; ``SetUser``, lines 572-636) and
    ``/usr/lib/python3/dist-packages/osc_lib/utils/__init__.py`` (``get_password``,
    lines 562-580): ``--password-prompt`` calls ``osc_lib.utils.get_password(stdin)``,
    which checks ``if hasattr(stdin, 'isatty') and stdin.isatty():`` and calls
    ``getpass.getpass()``, raising
    ``CommandError("No terminal detected attempting to read password")`` whenever
    ``stdin`` is a pipe (``stdin=subprocess.PIPE``). Neither ``CreateUser`` nor
    ``SetUser`` reads an environment variable for the target user's new password.
    Therefore:
    - The authenticating ``admin`` user's password (``ADMIN_PASS``) is passed via
      ``env_params={"admin_password": "OS_PASSWORD"}`` (stored in ``/proc/<pid>/environ``,
      readable only by ``root`` and the same UID, never in ``argv``).
    - The target service user's password in ``user create`` / ``user set`` is passed via
      ``--password {user_password}`` in ``argv`` (masked as ``********`` everywhere by
      OpenStack Forge, while visible in ``/proc/<pid>/cmdline`` for the duration of that
      command).
  - ``role_assignment.py`` (``ListRoleAssignment``, lines 23-51 and 220-250): with
    ``--names -f json``, columns are
    ``('Role', 'User', 'Group', 'Project', 'Domain', 'System', 'Inherited')`` and
    ``_format_role_assignment_`` formats ``User`` and ``Project`` as
    ``"<name>@<domain>"`` (for example ``"glance@Default"`` and ``"service@Default"``)
    when Keystone returns domain metadata, or ``"<name>"`` when omitted.
  - ``service.py`` (``CreateService``, lines 56-104; ``ListService``, lines 144-172):
    ``ListService`` default columns are ``('ID', 'Name', 'Type')``. In Keystone v3,
    ``identity_client.create_service()`` does **not** enforce uniqueness on ``name`` or
    ``type``: running ``openstack service create`` twice creates a **duplicate** service
    with a new UUID.
  - ``endpoint.py`` (``CreateEndpoint``, lines 74-134; ``ListEndpoint``, lines 176-281):
    ``ListEndpoint`` columns are
    ``('ID', 'Region', 'Service Name', 'Service Type', 'Enabled', 'Interface', 'URL')``.
    In Keystone v3, ``identity_client.endpoints.create()`` does **not** enforce
    uniqueness on ``(service, interface, region)``: running ``openstack endpoint create``
    twice creates a **duplicate** endpoint with a new UUID.
  - Consequently, :func:`ensure_service_project`, :func:`ensure_service_user`, and
    :func:`ensure_service_catalog` always inspect existing state with read-only
    ``list -f json`` commands before mutating, and :func:`verify_project_present`,
    :func:`verify_service_user_and_role`, and :func:`verify_service_and_endpoints` flag
    duplicate entries as verification errors.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from openstack_forge.execution.commands import (
    OPENSTACK_ENDPOINT_CREATE,
    OPENSTACK_ENDPOINT_LIST,
    OPENSTACK_PROJECT_CREATE_SERVICE,
    OPENSTACK_PROJECT_LIST,
    OPENSTACK_ROLE_ADD_USER_PROJECT,
    OPENSTACK_ROLE_ASSIGNMENT_LIST,
    OPENSTACK_SERVICE_CREATE,
    OPENSTACK_SERVICE_LIST,
    OPENSTACK_USER_CREATE,
    OPENSTACK_USER_LIST,
    OPENSTACK_USER_SET_PASSWORD,
    CommandExecutor,
    validate_parameter,
)
from openstack_forge.execution.errors import (
    CommandError,
    ExecutionError,
    PreconditionError,
    UnexpectedExitError,
)
from openstack_forge.execution.preview import PlannedCommand
from openstack_forge.execution.redaction import redact_text

DEFAULT_DOMAIN = "default"
SERVICE_PROJECT_NAME = "service"
SERVICE_PROJECT_DESCRIPTION = "Service Project"
ADMIN_ROLE_NAME = "admin"
DEFAULT_REGION = "RegionOne"
ENDPOINT_INTERFACES: tuple[str, ...] = ("public", "internal", "admin")


@dataclass(frozen=True)
class ProjectRecord:
    """One row from ``openstack project list --domain default -f json``."""

    id: str
    name: str


@dataclass(frozen=True)
class UserRecord:
    """One row from ``openstack user list --domain default -f json``."""

    id: str
    name: str


@dataclass(frozen=True)
class RoleAssignmentRecord:
    """One normalized row from ``openstack role assignment list --names -f json``."""

    role: str
    user: str
    project: str
    user_domain: str = DEFAULT_DOMAIN
    project_domain: str = DEFAULT_DOMAIN


@dataclass(frozen=True)
class ServiceRecord:
    """One row from ``openstack service list -f json``."""

    id: str
    name: str
    type: str


@dataclass(frozen=True)
class EndpointRecord:
    """One row from ``openstack endpoint list -f json``."""

    id: str
    region: str
    service_name: str
    service_type: str
    enabled: bool
    interface: str
    url: str


def _load_json_list(stdout: str, command_label: str) -> list[Mapping[str, object]]:
    """Parse ``stdout`` as a JSON list of objects, or raise :class:`ExecutionError`."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        snippet = redact_text(stdout.strip()[:160])
        raise ExecutionError(
            f"Unexpected non-JSON output from {command_label} "
            f"(line {exc.lineno}, column {exc.colno}): {snippet!r}"
        ) from None
    if not isinstance(parsed, list) or not all(isinstance(item, Mapping) for item in parsed):
        raise ExecutionError(
            f"Unexpected JSON structure from {command_label}: expected a JSON list of objects"
        )
    return parsed


def _get_str_field(
    row: Mapping[str, object],
    keys: tuple[str, ...],
    command_label: str,
    *,
    allow_empty: bool = False,
) -> str:
    for key in keys:
        if key in row:
            val = row[key]
            if val is None and allow_empty:
                return ""
            if isinstance(val, str):
                if val or allow_empty:
                    return val
            break
    raise ExecutionError(
        f"Missing or invalid {keys[0]!r} field in {command_label} JSON row: "
        f"{redact_text(repr(dict(row)))}"
    )


def _split_qualified_name(raw: str) -> tuple[str, str]:
    """Split ``'name@Domain'`` into ``(name, domain.lower())``; default domain is ``'default'``."""
    if "@" in raw:
        name, domain = raw.rsplit("@", 1)
        return name, (domain.lower() or DEFAULT_DOMAIN)
    return raw, DEFAULT_DOMAIN


def parse_project_list(stdout: str) -> tuple[ProjectRecord, ...]:
    """Parse ``openstack project list --domain default -f json`` output."""
    rows = _load_json_list(stdout, OPENSTACK_PROJECT_LIST)
    return tuple(
        ProjectRecord(
            id=_get_str_field(row, ("ID", "id"), OPENSTACK_PROJECT_LIST),
            name=_get_str_field(row, ("Name", "name"), OPENSTACK_PROJECT_LIST),
        )
        for row in rows
    )


def parse_user_list(stdout: str) -> tuple[UserRecord, ...]:
    """Parse ``openstack user list --domain default -f json`` output."""
    rows = _load_json_list(stdout, OPENSTACK_USER_LIST)
    return tuple(
        UserRecord(
            id=_get_str_field(row, ("ID", "id"), OPENSTACK_USER_LIST),
            name=_get_str_field(row, ("Name", "name"), OPENSTACK_USER_LIST),
        )
        for row in rows
    )


def parse_role_assignment_list(stdout: str) -> tuple[RoleAssignmentRecord, ...]:
    """Parse ``openstack role assignment list --names -f json`` output."""
    rows = _load_json_list(stdout, OPENSTACK_ROLE_ASSIGNMENT_LIST)
    records: list[RoleAssignmentRecord] = []
    for row in rows:
        role = _get_str_field(row, ("Role", "role"), OPENSTACK_ROLE_ASSIGNMENT_LIST)
        raw_user = _get_str_field(
            row, ("User", "user"), OPENSTACK_ROLE_ASSIGNMENT_LIST, allow_empty=True
        )
        raw_project = _get_str_field(
            row, ("Project", "project"), OPENSTACK_ROLE_ASSIGNMENT_LIST, allow_empty=True
        )
        user_name, user_domain = _split_qualified_name(raw_user)
        project_name, project_domain = _split_qualified_name(raw_project)
        records.append(
            RoleAssignmentRecord(
                role=role,
                user=user_name,
                project=project_name,
                user_domain=user_domain,
                project_domain=project_domain,
            )
        )
    return tuple(records)


def parse_service_list(stdout: str) -> tuple[ServiceRecord, ...]:
    """Parse ``openstack service list -f json`` output."""
    rows = _load_json_list(stdout, OPENSTACK_SERVICE_LIST)
    return tuple(
        ServiceRecord(
            id=_get_str_field(row, ("ID", "id"), OPENSTACK_SERVICE_LIST),
            name=_get_str_field(row, ("Name", "name"), OPENSTACK_SERVICE_LIST, allow_empty=True),
            type=_get_str_field(row, ("Type", "type"), OPENSTACK_SERVICE_LIST),
        )
        for row in rows
    )


def parse_endpoint_list(stdout: str) -> tuple[EndpointRecord, ...]:
    """Parse ``openstack endpoint list -f json`` output."""
    rows = _load_json_list(stdout, OPENSTACK_ENDPOINT_LIST)
    records: list[EndpointRecord] = []
    for row in rows:
        enabled_raw = row.get("Enabled", row.get("enabled", True))
        if not isinstance(enabled_raw, bool):
            raise ExecutionError(
                f"Invalid 'Enabled' field in {OPENSTACK_ENDPOINT_LIST} JSON row: "
                f"{redact_text(repr(dict(row)))}"
            )
        records.append(
            EndpointRecord(
                id=_get_str_field(row, ("ID", "id"), OPENSTACK_ENDPOINT_LIST),
                region=_get_str_field(
                    row, ("Region", "region", "region_id"), OPENSTACK_ENDPOINT_LIST
                ),
                service_name=_get_str_field(
                    row,
                    ("Service Name", "service_name"),
                    OPENSTACK_ENDPOINT_LIST,
                    allow_empty=True,
                ),
                service_type=_get_str_field(
                    row, ("Service Type", "service_type"), OPENSTACK_ENDPOINT_LIST
                ),
                enabled=enabled_raw,
                interface=_get_str_field(
                    row, ("Interface", "interface"), OPENSTACK_ENDPOINT_LIST
                ).lower(),
                url=_get_str_field(row, ("URL", "url"), OPENSTACK_ENDPOINT_LIST),
            )
        )
    return tuple(records)


def _normalize_endpoint_urls(urls: Mapping[str, str] | str) -> dict[str, str]:
    """Return validated ``{'public': url, 'internal': url, 'admin': url}`` mapping."""
    if isinstance(urls, str):
        validated = validate_parameter("endpoint_url", "url", urls)
        return {iface: validated for iface in ENDPOINT_INTERFACES}
    if not isinstance(urls, Mapping) or set(urls) != set(ENDPOINT_INTERFACES):
        raise PreconditionError(
            f"Endpoint URLs must specify exactly {list(ENDPOINT_INTERFACES)}, "
            f"got {sorted(urls) if isinstance(urls, Mapping) else type(urls).__name__}"
        )
    return {
        iface: validate_parameter("endpoint_url", "url", urls[iface])
        for iface in ENDPOINT_INTERFACES
    }


def verify_project_present(
    projects: Iterable[ProjectRecord],
    project_name: str = SERVICE_PROJECT_NAME,
) -> list[str]:
    """Return human-readable problems if ``project_name`` is missing or duplicated."""
    validate_parameter("openstack_name", "project", project_name)
    matches = [proj for proj in projects if proj.name == project_name]
    if not matches:
        return [f"Keystone project {project_name!r} (domain 'default') does not exist"]
    if len(matches) > 1:
        return [
            f"duplicate Keystone project entries for {project_name!r} in domain 'default' "
            f"(found {len(matches)})"
        ]
    return []


def verify_service_user_and_role(
    users: Iterable[UserRecord],
    assignments: Iterable[RoleAssignmentRecord],
    user: str,
    *,
    project: str = SERVICE_PROJECT_NAME,
    role: str = ADMIN_ROLE_NAME,
) -> list[str]:
    """Return problems if ``user`` is missing/duplicated or lacks ``role`` on ``project``."""
    validate_parameter("openstack_name", "user", user)
    validate_parameter("openstack_name", "project", project)
    validate_parameter("openstack_name", "role", role)
    problems: list[str] = []
    matching_users = [item for item in users if item.name == user]
    if not matching_users:
        problems.append(f"Keystone user {user!r} (domain 'default') does not exist")
    elif len(matching_users) > 1:
        problems.append(
            f"duplicate Keystone user entries for {user!r} in domain 'default' "
            f"(found {len(matching_users)})"
        )
    has_role = any(
        item.role == role
        and item.user == user
        and item.project == project
        and item.user_domain == DEFAULT_DOMAIN
        and item.project_domain == DEFAULT_DOMAIN
        for item in assignments
    )
    if not has_role:
        problems.append(
            f"Keystone user {user!r} does not have role {role!r} on project {project!r}"
        )
    return problems


def verify_service_and_endpoints(
    services: Iterable[ServiceRecord],
    endpoints: Iterable[EndpointRecord],
    service_name: str,
    service_type: str,
    urls: Mapping[str, str] | str,
    *,
    region: str = DEFAULT_REGION,
) -> list[str]:
    """Verify one Keystone service entry and its ``public``/``internal``/``admin`` endpoints."""
    validate_parameter("openstack_name", "service_name", service_name)
    validate_parameter("openstack_name", "service_type", service_type)
    expected_urls = _normalize_endpoint_urls(urls)

    problems: list[str] = []
    service_list = list(services)
    exact_services = [
        svc for svc in service_list if svc.name == service_name and svc.type == service_type
    ]
    wrong_type_services = [
        svc for svc in service_list if svc.name == service_name and svc.type != service_type
    ]
    if not exact_services:
        if wrong_type_services:
            found_types = ", ".join(sorted({repr(svc.type) for svc in wrong_type_services}))
            problems.append(
                f"Keystone service {service_name!r} has type {found_types}, "
                f"expected {service_type!r}"
            )
        else:
            problems.append(
                f"Keystone service {service_name!r} (type {service_type!r}) does not exist"
            )
    elif len(exact_services) > 1:
        problems.append(
            f"duplicate Keystone service entries for {service_name!r} "
            f"(type {service_type!r}, found {len(exact_services)})"
        )
    elif wrong_type_services:
        problems.append(
            f"conflicting Keystone service entries for {service_name!r} with mismatched type"
        )

    endpoint_list = [
        ep for ep in endpoints if ep.service_name == service_name or ep.service_type == service_type
    ]
    for interface in ENDPOINT_INTERFACES:
        expected_url = expected_urls[interface]
        iface_endpoints = [ep for ep in endpoint_list if ep.interface == interface]
        if not iface_endpoints:
            problems.append(
                f"Keystone {interface} endpoint for {service_name!r} ({service_type!r}) "
                f"in {region} does not exist"
            )
            continue
        if len(iface_endpoints) > 1:
            problems.append(
                f"duplicate Keystone {interface} endpoints for {service_name!r} "
                f"({service_type!r}) (found {len(iface_endpoints)})"
            )
            continue
        ep = iface_endpoints[0]
        if ep.service_name != service_name or ep.service_type != service_type:
            problems.append(
                f"Keystone {interface} endpoint {ep.id!r} belongs to "
                f"{ep.service_name!r}/{ep.service_type!r}, expected "
                f"{service_name!r}/{service_type!r}"
            )
        if ep.region != region:
            problems.append(
                f"Keystone {interface} endpoint for {service_name!r} is in region "
                f"{ep.region!r}, expected {region!r}"
            )
        if not ep.enabled:
            problems.append(f"Keystone {interface} endpoint for {service_name!r} is disabled")
        if ep.url != expected_url:
            problems.append(
                f"Keystone {interface} endpoint for {service_name!r} has URL {ep.url!r}, "
                f"expected {expected_url!r}"
            )
    return problems


def plan_project_list(admin_password: str) -> PlannedCommand:
    """Return the read-only :class:`PlannedCommand` for ``openstack.project-list``."""
    validate_parameter("secret", "admin_password", admin_password)
    return PlannedCommand(OPENSTACK_PROJECT_LIST, {"admin_password": admin_password})


def plan_create_service_project(admin_password: str) -> PlannedCommand:
    """Return the mutating :class:`PlannedCommand` for ``openstack.project-create-service``."""
    validate_parameter("secret", "admin_password", admin_password)
    return PlannedCommand(OPENSTACK_PROJECT_CREATE_SERVICE, {"admin_password": admin_password})


def service_project_ready(executor: CommandExecutor, admin_password: str) -> bool:
    """For ``check()``: True if ``openstack.project-list`` succeeds and ``service`` exists once."""
    try:
        result = executor.run(OPENSTACK_PROJECT_LIST, {"admin_password": admin_password})
    except CommandError:
        return False
    if result.returncode != 0:
        return False
    try:
        projects = parse_project_list(result.stdout)
    except ExecutionError:
        return False
    return not verify_project_present(projects, SERVICE_PROJECT_NAME)


def service_project_problems(executor: CommandExecutor, admin_password: str) -> list[str]:
    """For ``verify()``: return problems if ``service`` project is missing or duplicated."""
    try:
        result = executor.run(OPENSTACK_PROJECT_LIST, {"admin_password": admin_password})
    except UnexpectedExitError as exc:
        detail = exc.stderr or f"exit status {exc.returncode}"
        return [f"could not query Keystone projects ({OPENSTACK_PROJECT_LIST}): {detail}"]
    except CommandError as exc:
        return [f"could not query Keystone projects ({OPENSTACK_PROJECT_LIST}): {exc}"]
    try:
        projects = parse_project_list(result.stdout)
    except ExecutionError as exc:
        return [str(exc)]
    return verify_project_present(projects, SERVICE_PROJECT_NAME)


def ensure_service_project(executor: CommandExecutor, admin_password: str) -> bool:
    """Idempotently ensure the ``service`` project exists in domain ``default``.

    Returns ``True`` if ``openstack.project-create-service`` was executed, or ``False`` if
    the ``service`` project already existed. Raises :class:`PreconditionError` if duplicate
    ``service`` projects exist.
    """
    validate_parameter("secret", "admin_password", admin_password)
    result = executor.run(OPENSTACK_PROJECT_LIST, {"admin_password": admin_password})
    projects = parse_project_list(result.stdout)
    matches = [proj for proj in projects if proj.name == SERVICE_PROJECT_NAME]
    if len(matches) > 1:
        raise PreconditionError(
            f"Duplicate Keystone project entries for {SERVICE_PROJECT_NAME!r} in domain "
            f"'default' (found {len(matches)}); remove the duplicate project before retrying"
        )
    if len(matches) == 1:
        return False
    executor.run(OPENSTACK_PROJECT_CREATE_SERVICE, {"admin_password": admin_password})
    return True


def service_user_ready(
    executor: CommandExecutor,
    admin_password: str,
    user: str,
    *,
    project: str = SERVICE_PROJECT_NAME,
    role: str = ADMIN_ROLE_NAME,
) -> bool:
    """For ``check()``: True if ``user`` exists once and has ``role`` on ``project``."""
    try:
        user_res = executor.run(OPENSTACK_USER_LIST, {"admin_password": admin_password})
        role_res = executor.run(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password})
    except CommandError:
        return False
    if user_res.returncode != 0 or role_res.returncode != 0:
        return False
    try:
        users = parse_user_list(user_res.stdout)
        assignments = parse_role_assignment_list(role_res.stdout)
    except ExecutionError:
        return False
    return not verify_service_user_and_role(users, assignments, user, project=project, role=role)


def service_user_problems(
    executor: CommandExecutor,
    admin_password: str,
    user: str,
    *,
    project: str = SERVICE_PROJECT_NAME,
    role: str = ADMIN_ROLE_NAME,
) -> list[str]:
    """For ``verify()``: return problems if ``user`` or its role assignment is missing."""
    try:
        user_res = executor.run(OPENSTACK_USER_LIST, {"admin_password": admin_password})
        role_res = executor.run(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password})
    except UnexpectedExitError as exc:
        detail = exc.stderr or f"exit status {exc.returncode}"
        return [f"could not query Keystone users/roles: {detail}"]
    except CommandError as exc:
        return [f"could not query Keystone users/roles: {exc}"]
    try:
        users = parse_user_list(user_res.stdout)
        assignments = parse_role_assignment_list(role_res.stdout)
    except ExecutionError as exc:
        return [str(exc)]
    return verify_service_user_and_role(users, assignments, user, project=project, role=role)


def ensure_service_user(
    executor: CommandExecutor,
    admin_password: str,
    user: str,
    user_password: str,
    *,
    project: str = SERVICE_PROJECT_NAME,
    role: str = ADMIN_ROLE_NAME,
) -> None:
    """Idempotently create or update ``user``'s password and grant ``role`` on ``project``."""
    validate_parameter("secret", "admin_password", admin_password)
    validate_parameter("openstack_name", "user", user)
    validate_parameter("secret", "user_password", user_password)
    validate_parameter("openstack_name", "project", project)
    validate_parameter("openstack_name", "role", role)

    user_res = executor.run(OPENSTACK_USER_LIST, {"admin_password": admin_password})
    users = parse_user_list(user_res.stdout)
    matches = [item for item in users if item.name == user]
    if len(matches) > 1:
        raise PreconditionError(
            f"Duplicate Keystone user entries for {user!r} in domain 'default' "
            f"(found {len(matches)})"
        )
    user_params = {
        "admin_password": admin_password,
        "user": user,
        "user_password": user_password,
    }
    if not matches:
        executor.run(OPENSTACK_USER_CREATE, user_params)
    else:
        executor.run(OPENSTACK_USER_SET_PASSWORD, user_params)

    role_res = executor.run(OPENSTACK_ROLE_ASSIGNMENT_LIST, {"admin_password": admin_password})
    assignments = parse_role_assignment_list(role_res.stdout)
    has_role = any(
        item.role == role
        and item.user == user
        and item.project == project
        and item.user_domain == DEFAULT_DOMAIN
        and item.project_domain == DEFAULT_DOMAIN
        for item in assignments
    )
    if not has_role:
        executor.run(
            OPENSTACK_ROLE_ADD_USER_PROJECT,
            {
                "admin_password": admin_password,
                "project": project,
                "user": user,
                "role": role,
            },
        )


def service_catalog_ready(
    executor: CommandExecutor,
    admin_password: str,
    service_name: str,
    service_type: str,
    urls: Mapping[str, str] | str,
    *,
    region: str = DEFAULT_REGION,
) -> bool:
    """For ``check()``: True if the Keystone service and its 3 endpoints exist and match."""
    try:
        svc_res = executor.run(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password})
        ep_res = executor.run(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password})
    except CommandError:
        return False
    if svc_res.returncode != 0 or ep_res.returncode != 0:
        return False
    try:
        services = parse_service_list(svc_res.stdout)
        endpoints = parse_endpoint_list(ep_res.stdout)
    except ExecutionError:
        return False
    return not verify_service_and_endpoints(
        services, endpoints, service_name, service_type, urls, region=region
    )


def service_catalog_problems(
    executor: CommandExecutor,
    admin_password: str,
    service_name: str,
    service_type: str,
    urls: Mapping[str, str] | str,
    *,
    region: str = DEFAULT_REGION,
) -> list[str]:
    """For ``verify()``: return problems with the Keystone service entry or its 3 endpoints."""
    try:
        svc_res = executor.run(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password})
        ep_res = executor.run(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password})
    except UnexpectedExitError as exc:
        detail = exc.stderr or f"exit status {exc.returncode}"
        return [f"could not query Keystone service catalog: {detail}"]
    except CommandError as exc:
        return [f"could not query Keystone service catalog: {exc}"]
    try:
        services = parse_service_list(svc_res.stdout)
        endpoints = parse_endpoint_list(ep_res.stdout)
    except ExecutionError as exc:
        return [str(exc)]
    return verify_service_and_endpoints(
        services, endpoints, service_name, service_type, urls, region=region
    )


def ensure_service_catalog(
    executor: CommandExecutor,
    admin_password: str,
    service_name: str,
    service_type: str,
    urls: Mapping[str, str] | str,
    *,
    region: str = DEFAULT_REGION,
) -> None:
    """Idempotently ensure one Keystone service and its ``public``/``internal``/``admin`` endpoints.

    Because Keystone v3 creates duplicate UUIDs if ``service create`` or ``endpoint create``
    is run when an entry already exists, this helper always inspects ``service list -f json``
    and ``endpoint list -f json`` first, creates only missing entries, and raises
    :class:`PreconditionError` if duplicate or conflicting entries exist.
    """
    validate_parameter("secret", "admin_password", admin_password)
    validate_parameter("openstack_name", "service_name", service_name)
    validate_parameter("openstack_name", "service_type", service_type)
    expected_urls = _normalize_endpoint_urls(urls)
    if region != DEFAULT_REGION:
        raise PreconditionError(
            f"Unsupported region {region!r}: only {DEFAULT_REGION!r} is supported"
        )

    svc_res = executor.run(OPENSTACK_SERVICE_LIST, {"admin_password": admin_password})
    services = parse_service_list(svc_res.stdout)
    exact_services = [
        svc for svc in services if svc.name == service_name and svc.type == service_type
    ]
    wrong_type_services = [
        svc for svc in services if svc.name == service_name and svc.type != service_type
    ]
    if wrong_type_services:
        raise PreconditionError(
            f"Conflicting Keystone service entry for {service_name!r} with mismatched type; "
            f"expected {service_type!r}"
        )
    if len(exact_services) > 1:
        raise PreconditionError(
            f"Duplicate Keystone service entries for {service_name!r} "
            f"(type {service_type!r}, found {len(exact_services)})"
        )
    if not exact_services:
        executor.run(
            OPENSTACK_SERVICE_CREATE,
            {
                "admin_password": admin_password,
                "service_name": service_name,
                "service_type": service_type,
            },
        )

    ep_res = executor.run(OPENSTACK_ENDPOINT_LIST, {"admin_password": admin_password})
    endpoints = [
        ep
        for ep in parse_endpoint_list(ep_res.stdout)
        if ep.service_name == service_name or ep.service_type == service_type
    ]
    for interface in ENDPOINT_INTERFACES:
        expected_url = expected_urls[interface]
        iface_endpoints = [ep for ep in endpoints if ep.interface == interface]
        if len(iface_endpoints) > 1:
            raise PreconditionError(
                f"Duplicate Keystone {interface} endpoints for {service_name!r} "
                f"({service_type!r}) (found {len(iface_endpoints)})"
            )
        if len(iface_endpoints) == 1:
            ep = iface_endpoints[0]
            if (
                ep.service_name != service_name
                or ep.service_type != service_type
                or ep.region != region
                or not ep.enabled
                or ep.url != expected_url
            ):
                raise PreconditionError(
                    f"Existing Keystone {interface} endpoint for {service_name!r} does not "
                    f"match expected state (region={ep.region!r}, enabled={ep.enabled!r}, "
                    f"url={ep.url!r}; expected region={region!r}, enabled=True, "
                    f"url={expected_url!r})"
                )
            continue
        executor.run(
            OPENSTACK_ENDPOINT_CREATE,
            {
                "admin_password": admin_password,
                "service_name": service_name,
                "interface": interface,
                "url": expected_url,
            },
        )
