"""Tests for the bundled deployment step catalog and its validation."""

from __future__ import annotations

import copy
import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.config.semantic import ROLE_LIMITS
from openstack_forge.orchestration import (
    STATUS_UNVERIFIED,
    CatalogError,
    load_catalog,
    load_catalog_schema,
    validate_catalog,
)
from openstack_forge.orchestration.catalog import (
    CATALOG_PACKAGE,
    CATALOG_RESOURCE,
    CATALOG_SCHEMA_RESOURCE,
    _find_cycle,
    package_resource,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_ROLES = ["controller", "compute", "block", "object"]

# The initial catalog content. Changing a status here must be a deliberate decision:
# a step becomes "verified" only after it has been compared with the official page.
EXPECTED_STEPS: list[tuple[str, list[str], list[str], str]] = [
    ("hosts-file", ALL_ROLES, ["/etc/hosts"], STATUS_UNVERIFIED),
    ("time-sync-controller", ["controller"], ["/etc/chrony/chrony.conf"], STATUS_UNVERIFIED),
    (
        "time-sync-other",
        ["compute", "block", "object"],
        ["/etc/chrony/chrony.conf"],
        STATUS_UNVERIFIED,
    ),  # fmt: skip
    ("openstack-packages", ALL_ROLES, [], STATUS_UNVERIFIED),
    ("mariadb", ["controller"], ["/etc/mysql/mariadb.conf.d/99-openstack.cnf"], STATUS_UNVERIFIED),
    ("rabbitmq", ["controller"], [], STATUS_UNVERIFIED),
    ("memcached", ["controller"], ["/etc/memcached.conf"], STATUS_UNVERIFIED),
    ("etcd", ["controller"], ["/etc/default/etcd"], STATUS_UNVERIFIED),
    (
        "keystone",
        ["controller"],
        [
            "/etc/keystone/keystone.conf",
            "/etc/apache2/apache2.conf",
            "/etc/openstack-forge/admin-openrc",
        ],
        STATUS_UNVERIFIED,
    ),
    ("glance", ["controller"], ["/etc/glance/glance-api.conf"], STATUS_UNVERIFIED),
    ("placement", ["controller"], ["/etc/placement/placement.conf"], STATUS_UNVERIFIED),
    ("nova-controller", ["controller"], ["/etc/nova/nova.conf"], STATUS_UNVERIFIED),
    (
        "nova-compute",
        ["compute"],
        ["/etc/nova/nova.conf", "/etc/nova/nova-compute.conf"],
        STATUS_UNVERIFIED,
    ),
    (
        "neutron-controller",
        ["controller"],
        [
            "/etc/neutron/neutron.conf",
            "/etc/neutron/plugins/ml2/ml2_conf.ini",
            "/etc/neutron/plugins/ml2/openvswitch_agent.ini",
            "/etc/neutron/l3_agent.ini",
            "/etc/neutron/dhcp_agent.ini",
            "/etc/neutron/metadata_agent.ini",
            "/etc/nova/nova.conf",
        ],
        STATUS_UNVERIFIED,
    ),
    (
        "neutron-compute",
        ["compute"],
        [
            "/etc/neutron/neutron.conf",
            "/etc/neutron/plugins/ml2/openvswitch_agent.ini",
            "/etc/nova/nova.conf",
        ],
        STATUS_UNVERIFIED,
    ),
    (
        "horizon",
        ["controller"],
        [
            "/etc/openstack-dashboard/local_settings.py",
            "/etc/apache2/conf-available/openstack-dashboard.conf",
        ],
        STATUS_UNVERIFIED,
    ),
    (
        "cinder-controller",
        ["controller"],
        ["/etc/cinder/cinder.conf", "/etc/nova/nova.conf"],
        STATUS_UNVERIFIED,
    ),
    (
        "cinder-storage",
        ["block"],
        [
            "/etc/lvm/lvm.conf",
            "/etc/cinder/cinder.conf",
            "/etc/tgt/conf.d/cinder.conf",
        ],
        STATUS_UNVERIFIED,
    ),
]


@pytest.fixture(scope="module")
def catalog_schema() -> dict[str, Any]:
    return load_catalog_schema()


@pytest.fixture
def raw_catalog() -> dict[str, Any]:
    """A fresh, mutable copy of the bundled catalog data."""
    return json.loads(package_resource(CATALOG_RESOURCE).read_text(encoding="utf-8"))


def _messages(raw: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    return [str(issue) for issue in validate_catalog(raw, schema)]


# --- the bundled catalog -----------------------------------------------------------


def test_catalog_schema_is_a_valid_json_schema(catalog_schema) -> None:
    assert catalog_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_bundled_catalog_is_valid(raw_catalog, catalog_schema) -> None:
    assert validate_catalog(raw_catalog, catalog_schema) == []


def test_catalog_targets_the_supported_release() -> None:
    assert load_catalog().openstack_release == "2025.1"


def test_initial_catalog_content() -> None:
    catalog = load_catalog()
    actual = [
        (step.id, list(step.roles), list(step.files_modified), step.status)
        for step in catalog.steps
    ]
    assert actual == EXPECTED_STEPS


def test_no_step_is_marked_verified() -> None:
    assert [step.id for step in load_catalog().steps if step.verified] == []


def test_step_ids_are_unique() -> None:
    ids = [step.id for step in load_catalog().steps]
    assert len(ids) == len(set(ids))


def test_dependencies_exist() -> None:
    catalog = load_catalog()
    ids = {step.id for step in catalog.steps}
    for step in catalog.steps:
        assert set(step.depends_on) <= ids, step.id


def test_dependencies_have_no_cycles() -> None:
    catalog = load_catalog()
    assert _find_cycle({step.id: step.depends_on for step in catalog.steps}) is None


def test_catalog_roles_match_configuration_roles(catalog_schema) -> None:
    assert catalog_schema["$defs"]["role"]["enum"] == list(ROLE_LIMITS)
    for step in load_catalog().steps:
        assert set(step.roles) <= set(ROLE_LIMITS), step.id


def test_doc_urls_point_to_official_documentation() -> None:
    for step in load_catalog().steps:
        assert step.doc_url.startswith("https://docs.openstack.org/"), step.id


def test_network_configuration_is_a_note_not_a_step() -> None:
    catalog = load_catalog()
    (note,) = catalog.notes
    assert note.id == "network-configuration"
    assert "/etc/network/interfaces" in note.text
    assert "netplan" in note.text
    assert "network-configuration" not in {step.id for step in catalog.steps}
    assert all("/etc/network/interfaces" not in step.files_modified for step in catalog.steps)


def test_catalog_files_ship_as_package_data() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    package_dir = REPO_ROOT / "src" / Path(*CATALOG_PACKAGE.split("."))
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"][CATALOG_PACKAGE]
    for resource in (CATALOG_RESOURCE, CATALOG_SCHEMA_RESOURCE):
        assert (package_dir / resource).is_file()
        assert any(fnmatch(resource, pattern) for pattern in patterns), resource


# --- validation of invalid catalogs --------------------------------------------------


def test_duplicate_step_id(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"].append(copy.deepcopy(raw_catalog["steps"][0]))
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[18].id: duplicate id 'hosts-file'; already used by steps[0]"
    ]


def test_unknown_dependency(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][4]["depends_on"] = ["openstack-packages", "nova"]
    assert _messages(raw_catalog, catalog_schema) == ["steps[4].depends_on[1]: unknown step 'nova'"]


def test_dependency_cycle(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][0]["depends_on"] = ["openstack-packages"]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps: dependency cycle: "
        "hosts-file -> openstack-packages -> time-sync-controller -> hosts-file"
    ]


def test_self_dependency_is_a_cycle(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][5]["depends_on"] = ["rabbitmq"]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps: dependency cycle: rabbitmq -> rabbitmq"
    ]


def test_unknown_role(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][3]["roles"] = ["storage"]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[3].roles[0]: unsupported value 'storage'; "
        "expected one of: controller, compute, block, object"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.openstack.org/install-guide/",
        "https://docs.openstack.org.example.com/",
        "https://example.com/https://docs.openstack.org/",
    ],
)
def test_doc_url_must_be_official(raw_catalog, catalog_schema, url: str) -> None:
    raw_catalog["steps"][0]["doc_url"] = url
    (message,) = _messages(raw_catalog, catalog_schema)
    assert message.startswith("steps[0].doc_url:")
    assert "https://docs.openstack.org/" in message


def test_status_must_be_verified_or_unverified(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][0]["status"] = "done"
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[0].status: unsupported value 'done'; expected one of: verified, unverified"
    ]


def test_files_modified_must_be_absolute(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][0]["files_modified"] = ["etc/hosts"]
    (message,) = _messages(raw_catalog, catalog_schema)
    assert message.startswith("steps[0].files_modified[0]: 'etc/hosts' has an invalid format")


@pytest.mark.parametrize("field", ["command", "commands", "content"])
def test_steps_cannot_carry_commands_or_file_contents(
    raw_catalog, catalog_schema, field: str
) -> None:
    raw_catalog["steps"][0][field] = "anything"
    (message,) = _messages(raw_catalog, catalog_schema)
    assert message.startswith(f"steps[0].{field}: unknown field")


@pytest.mark.parametrize(
    "field",
    ["id", "title", "roles", "depends_on", "files_modified", "verification", "doc_url", "status"],
)
def test_every_step_field_is_required(raw_catalog, catalog_schema, field: str) -> None:
    del raw_catalog["steps"][2][field]
    assert _messages(raw_catalog, catalog_schema) == [
        f"steps[2].{field}: required field is missing"
    ]


def test_load_catalog_reports_issues(tmp_path: Path, raw_catalog) -> None:
    raw_catalog["steps"][1]["depends_on"] = ["missing-step"]
    path = tmp_path / "steps.json"
    path.write_text(json.dumps(raw_catalog), encoding="utf-8")
    with pytest.raises(CatalogError, match="unknown step 'missing-step'") as excinfo:
        load_catalog(path)
    assert [issue.location for issue in excinfo.value.issues] == ["steps[1].depends_on[0]"]


# --- role-scoped dependencies ----------------------------------------------------------


def test_openstack_packages_dependencies_are_role_scoped() -> None:
    step = load_catalog().step("openstack-packages")
    assert step.depends_on == ("time-sync-controller", "time-sync-other")
    assert step.dependency_roles == (
        ("time-sync-controller", ("controller",)),
        ("time-sync-other", ("compute", "block", "object")),
    )
    assert step.dependencies_for("controller") == ("time-sync-controller",)
    for role in ("compute", "block", "object"):
        assert step.dependencies_for(role) == ("time-sync-other",)


def test_role_scoped_dependency_must_name_the_steps_own_roles(raw_catalog, catalog_schema) -> None:
    # mariadb runs on the controller only.
    raw_catalog["steps"][4]["depends_on"] = [
        {"step": "openstack-packages", "roles": ["controller", "compute"]}
    ]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[4].depends_on[0].roles: role 'compute' does not run this step "
        "(its roles: controller)"
    ]


def test_role_scoped_dependency_on_an_unknown_step(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][4]["depends_on"] = [{"step": "nova", "roles": ["controller"]}]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[4].depends_on[0].step: unknown step 'nova'"
    ]


def test_dependency_listed_twice_is_refused(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][3]["depends_on"].append("time-sync-other")
    assert _messages(raw_catalog, catalog_schema) == [
        "steps[3].depends_on[2]: duplicate dependency 'time-sync-other'; already listed at "
        "steps[3].depends_on[1]"
    ]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"step": "hosts-file"}, "steps[4].depends_on[0].roles: required field is missing"),
        (
            {"step": "hosts-file", "roles": []},
            "steps[4].depends_on[0].roles: must contain at least 1 item(s)",
        ),
        (
            {"step": "hosts-file", "roles": ["controller"], "when": "x"},
            "steps[4].depends_on[0].when: unknown field (allowed fields: step, roles)",
        ),
    ],
)
def test_role_scoped_dependency_structure(raw_catalog, catalog_schema, entry, expected) -> None:
    raw_catalog["steps"][4]["depends_on"] = [entry]
    assert _messages(raw_catalog, catalog_schema) == [expected]


def test_role_scoped_dependency_cycle_is_refused(raw_catalog, catalog_schema) -> None:
    raw_catalog["steps"][0]["depends_on"] = [{"step": "openstack-packages", "roles": ["object"]}]
    assert _messages(raw_catalog, catalog_schema) == [
        "steps: dependency cycle: "
        "hosts-file -> openstack-packages -> time-sync-controller -> hosts-file"
    ]


def test_load_catalog_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "steps.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid JSON"):
        load_catalog(path)
