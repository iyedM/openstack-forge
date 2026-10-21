"""Tests for building and formatting the read-only deployment plan."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from openstack_forge.orchestration import (
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    Dependency,
    PlanError,
    Step,
    StepCatalog,
    build_plan,
    format_plan_json,
    format_plan_text,
    load_catalog,
)

DOCS = "https://docs.openstack.org/install-guide/"


@pytest.fixture(scope="module")
def catalog() -> StepCatalog:
    return load_catalog()


def _step(
    step_id: str,
    roles: list[str],
    depends_on: list[str] = (),
    dependency_roles: dict[str, list[str]] | None = None,
) -> Step:
    return Step(
        id=step_id,
        title=f"Test step {step_id}",
        roles=tuple(roles),
        depends_on=tuple(depends_on),
        files_modified=(),
        verification="Test verification.",
        doc_url=DOCS,
        status=STATUS_UNVERIFIED,
        dependency_roles=tuple(
            (dependency, tuple(only)) for dependency, only in (dependency_roles or {}).items()
        ),
    )


def _catalog(*steps: Step) -> StepCatalog:
    return StepCatalog(openstack_release="2025.1", steps=steps, notes=())


def _order(plan) -> list[tuple[str, str]]:
    return [(item.node, item.step.id) for item in plan.items]


# --- ordering and dependencies ---------------------------------------------------


def test_every_dependency_comes_before_its_dependent(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    position = {(item.node, item.step.id): item.sequence for item in plan.items}
    for item in plan.items:
        for dependency in item.waits_for:
            assert position[(dependency.node, dependency.step_id)] < item.sequence


def test_plan_is_ordered_by_step_then_controller_first(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    assert [item.sequence for item in plan.items] == list(range(1, len(plan.items) + 1))
    assert _order(plan)[:12] == [
        ("ctl-01", "hosts-file"),
        ("cmp-01", "hosts-file"),
        ("blk-01", "hosts-file"),
        ("obj-01", "hosts-file"),
        ("ctl-01", "time-sync-controller"),
        ("cmp-01", "time-sync-other"),
        ("blk-01", "time-sync-other"),
        ("obj-01", "time-sync-other"),
        ("ctl-01", "openstack-packages"),
        ("cmp-01", "openstack-packages"),
        ("blk-01", "openstack-packages"),
        ("obj-01", "openstack-packages"),
    ]
    assert _order(plan)[-11:] == [
        ("ctl-01", "mariadb"),
        ("ctl-01", "rabbitmq"),
        ("ctl-01", "memcached"),
        ("ctl-01", "etcd"),
        ("ctl-01", "keystone"),
        ("ctl-01", "glance"),
        ("ctl-01", "placement"),
        ("ctl-01", "nova-controller"),
        ("cmp-01", "nova-compute"),
        ("ctl-01", "neutron-controller"),
        ("cmp-01", "neutron-compute"),
    ]


def test_controller_is_ordered_first_regardless_of_config_order(valid_config, catalog) -> None:
    valid_config["nodes"].reverse()
    plan = build_plan(valid_config, catalog)
    assert _order(plan)[:4] == [
        ("ctl-01", "hosts-file"),
        ("cmp-01", "hosts-file"),
        ("blk-01", "hosts-file"),
        ("obj-01", "hosts-file"),
    ]


def test_local_dependencies_wait_on_the_same_node(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    item = next(i for i in plan.items if (i.node, i.step.id) == ("ctl-01", "openstack-packages"))
    assert item.waits_for == (Dependency("time-sync-controller", "ctl-01"),)


def test_time_sync_controller_comes_before_every_dependent_step(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    order = _order(plan)
    controller_sync = order.index(("ctl-01", "time-sync-controller"))
    for node in ("cmp-01", "blk-01", "obj-01"):
        assert order.index((node, "time-sync-other")) > controller_sync
        assert order.index((node, "openstack-packages")) > controller_sync


def test_other_nodes_wait_for_the_controller_time_sync(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    waits = {(i.node, i.step.id): i.waits_for for i in plan.items}
    for node in ("cmp-01", "blk-01", "obj-01"):
        # Its own hosts-file first: 'server controller iburst' needs the /etc/hosts alias.
        assert waits[(node, "time-sync-other")] == (
            Dependency("hosts-file", node),
            Dependency("time-sync-controller", "ctl-01"),
        )


def test_packages_wait_for_the_nodes_own_time_sync_other(valid_config, catalog) -> None:
    # Role-scoped dependencies (milestone 10): on compute, block and object nodes,
    # openstack-packages waits for that node's own time-sync-other, which itself waits for
    # the controller's time-sync-controller.
    plan = build_plan(valid_config, catalog)
    waits = {(i.node, i.step.id): i.waits_for for i in plan.items}
    order = _order(plan)
    for node in ("cmp-01", "blk-01", "obj-01"):
        assert waits[(node, "openstack-packages")] == (Dependency("time-sync-other", node),)
        assert order.index((node, "time-sync-other")) < order.index((node, "openstack-packages"))


def test_controller_packages_do_not_wait_for_other_nodes(valid_config, catalog) -> None:
    for index in (2, 3):
        valid_config["nodes"].append(
            {**valid_config["nodes"][1], "hostname": f"cmp-0{index}", "fqdn": f"c{index}.test"}
        )
    plan = build_plan(valid_config, catalog)
    (item,) = [i for i in plan.items if (i.node, i.step.id) == ("ctl-01", "openstack-packages")]
    assert item.waits_for == (Dependency("time-sync-controller", "ctl-01"),)
    text = format_plan_text(build_plan(valid_config, catalog, node="ctl-01"))
    packages = text[text.index("openstack-packages  [UNVERIFIED]") :].split("\n\n", 1)[0]
    assert "waits for:    time-sync-controller on ctl-01" in packages
    assert "time-sync-other" not in packages


def test_node_plan_shows_the_local_time_sync_other_wait(valid_config, catalog) -> None:
    text = format_plan_text(build_plan(valid_config, catalog, node="blk-01"))
    packages = text[text.index("openstack-packages  [UNVERIFIED]") :].split("\n\n", 1)[0]
    assert "waits for:    time-sync-other on blk-01" in packages
    assert "time-sync-controller" not in packages


def test_role_scoped_dependency_is_ignored_on_other_roles(valid_config) -> None:
    catalog = _catalog(
        _step("sync-a", ["controller"]),
        _step("sync-b", ["compute", "block", "object"]),
        _step(
            "packages",
            ["controller", "compute", "block", "object"],
            ["sync-a", "sync-b"],
            {"sync-a": ["controller"], "sync-b": ["compute", "block", "object"]},
        ),
    )
    waits = {(i.node, i.step.id): i.waits_for for i in build_plan(valid_config, catalog).items}
    assert waits[("ctl-01", "packages")] == (Dependency("sync-a", "ctl-01"),)
    assert waits[("cmp-01", "packages")] == (Dependency("sync-b", "cmp-01"),)


def test_without_role_scope_the_controller_would_wait_for_every_other_node(valid_config) -> None:
    # The same catalog with plain ids: the reason role scopes exist.
    catalog = _catalog(
        _step("sync-a", ["controller"]),
        _step("sync-b", ["compute", "block", "object"]),
        _step("packages", ["controller", "compute", "block", "object"], ["sync-a", "sync-b"]),
    )
    waits = {(i.node, i.step.id): i.waits_for for i in build_plan(valid_config, catalog).items}
    assert waits[("ctl-01", "packages")] == (
        Dependency("sync-a", "ctl-01"),
        Dependency("sync-b", "cmp-01"),
        Dependency("sync-b", "blk-01"),
        Dependency("sync-b", "obj-01"),
    )


def test_role_scoped_dependency_can_still_cross_nodes(valid_config) -> None:
    catalog = _catalog(
        _step("database", ["controller"]),
        _step("agent", ["controller", "compute"], ["database"], {"database": ["compute"]}),
    )
    waits = {(i.node, i.step.id): i.waits_for for i in build_plan(valid_config, catalog).items}
    assert waits[("ctl-01", "agent")] == ()
    assert waits[("cmp-01", "agent")] == (Dependency("database", "ctl-01"),)


def test_dependencies_for_role() -> None:
    step = _step("s", ["controller", "compute"], ["a", "b", "c"], {"b": ["compute"]})
    assert step.dependencies_for("controller") == ("a", "c")
    assert step.dependencies_for("compute") == ("a", "b", "c")


def test_cross_node_dependency_waits_for_the_controller(valid_config) -> None:
    catalog = _catalog(
        _step("base", ["controller", "compute", "block", "object"]),
        _step("database", ["controller"], ["base"]),
        _step("compute-service", ["compute"], ["base", "database"]),
    )
    plan = build_plan(valid_config, catalog)
    order = _order(plan)
    service = next(i for i in plan.items if i.step.id == "compute-service")
    assert service.waits_for == (Dependency("base", "cmp-01"), Dependency("database", "ctl-01"))
    assert order.index(("ctl-01", "database")) < order.index(("cmp-01", "compute-service"))
    assert "waits for:    database on ctl-01" in format_plan_text(plan)


def test_cross_node_dependency_waits_for_every_node_running_the_step(valid_config) -> None:
    valid_config["nodes"].append(
        {**valid_config["nodes"][1], "hostname": "cmp-02", "fqdn": "cmp-02.cluster.test"}
    )
    catalog = _catalog(
        _step("compute-agent", ["compute"]),
        _step("discover-hosts", ["controller"], ["compute-agent"]),
    )
    plan = build_plan(valid_config, catalog)
    discover = next(i for i in plan.items if i.step.id == "discover-hosts")
    assert discover.waits_for == (
        Dependency("compute-agent", "cmp-01"),
        Dependency("compute-agent", "cmp-02"),
    )
    assert _order(plan)[-1] == ("ctl-01", "discover-hosts")


def test_dependency_not_run_by_any_node_is_reported(valid_config) -> None:
    del valid_config["nodes"][3]  # the object node
    catalog = _catalog(
        _step("object-ring", ["object"]),
        _step("object-proxy", ["controller"], ["object-ring"]),
    )
    plan = build_plan(valid_config, catalog)
    (proxy,) = plan.items
    assert proxy.waits_for == (Dependency("object-ring", None),)
    assert "waits for:    object-ring (not run by any node in this cluster)" in (
        format_plan_text(plan)
    )


# --- per-role filtering and --node ---------------------------------------------------


@pytest.mark.parametrize(
    ("hostname", "expected_steps"),
    [
        (
            "ctl-01",
            [
                "hosts-file",
                "time-sync-controller",
                "openstack-packages",
                "mariadb",
                "rabbitmq",
                "memcached",
                "etcd",
                "keystone",
                "glance",
                "placement",
                "nova-controller",
                "neutron-controller",
            ],
        ),
        (
            "cmp-01",
            [
                "hosts-file",
                "time-sync-other",
                "openstack-packages",
                "nova-compute",
                "neutron-compute",
            ],
        ),
        ("blk-01", ["hosts-file", "time-sync-other", "openstack-packages"]),
        ("obj-01", ["hosts-file", "time-sync-other", "openstack-packages"]),
    ],
)
def test_steps_are_filtered_by_role(
    valid_config, catalog, hostname: str, expected_steps: list[str]
) -> None:
    plan = build_plan(valid_config, catalog, node=hostname)
    assert [item.step.id for item in plan.items] == expected_steps
    assert {item.node for item in plan.items} == {hostname}
    assert plan.node_filter == hostname
    assert plan.node_count == 1


def test_nova_compute_waits_for_local_packages_and_controller_nova(valid_config, catalog) -> None:
    plan = build_plan(valid_config, catalog)
    waits = {(i.node, i.step.id): i.waits_for for i in plan.items}
    assert waits[("cmp-01", "nova-compute")] == (
        Dependency("openstack-packages", "cmp-01"),
        Dependency("nova-controller", "ctl-01"),
    )
    assert waits[("cmp-01", "neutron-compute")] == (
        Dependency("openstack-packages", "cmp-01"),
        Dependency("nova-compute", "cmp-01"),
        Dependency("neutron-controller", "ctl-01"),
    )


def test_node_plan_keeps_cluster_wide_sequence_numbers(valid_config, catalog) -> None:
    full = build_plan(valid_config, catalog)
    single = build_plan(valid_config, catalog, node="cmp-01")
    expected = [item.sequence for item in full.items if item.node == "cmp-01"]
    assert [item.sequence for item in single.items] == expected


def test_node_plan_shows_cross_node_waits(valid_config) -> None:
    catalog = _catalog(
        _step("database", ["controller"]),
        _step("compute-service", ["compute"], ["database"]),
    )
    plan = build_plan(valid_config, catalog, node="CMP-01")
    (item,) = plan.items
    assert item.node == "cmp-01"
    assert item.sequence == 2
    assert item.waits_for == (Dependency("database", "ctl-01"),)


def test_unknown_node_is_rejected(valid_config, catalog) -> None:
    with pytest.raises(PlanError, match=r"Node 'nope' was not found .*ctl-01, cmp-01"):
        build_plan(valid_config, catalog, node="nope")


def test_release_mismatch_is_rejected(valid_config, catalog) -> None:
    other = dataclasses.replace(catalog, openstack_release="2025.2")
    with pytest.raises(PlanError, match="describes OpenStack 2025.2"):
        build_plan(valid_config, other)


# --- output ---------------------------------------------------------------------------


def test_unverified_steps_are_flagged_and_counted(valid_config, catalog) -> None:
    text = format_plan_text(build_plan(valid_config, catalog))
    assert "WARNING: 23 of 23 step(s) are UNVERIFIED" in text
    assert text.count("[UNVERIFIED]") == 23
    assert text.rstrip().endswith("Summary: 23 step(s) on 4 node(s); 0 verified, 23 unverified.")


def test_verified_steps_are_not_flagged(valid_config) -> None:
    verified = dataclasses.replace(_step("checked", ["controller"]), status=STATUS_VERIFIED)
    plan = build_plan(valid_config, _catalog(verified, _step("unchecked", ["controller"])))
    text = format_plan_text(plan)
    assert "checked\n" in text  # no flag after the verified step's id
    assert text.count("[UNVERIFIED]") == 1
    assert "WARNING: 1 of 2 step(s) are UNVERIFIED" in text
    assert plan.verified_count == 1


def test_text_shows_every_step_field(valid_config, catalog) -> None:
    text = format_plan_text(build_plan(valid_config, catalog, node="ctl-01"))
    mariadb = text[text.index("mariadb  [UNVERIFIED]") :].split("\n\n", 1)[0]
    assert "Install and configure MariaDB" in mariadb
    assert "files:        /etc/mysql/mariadb.conf.d/99-openstack.cnf" in mariadb
    assert "verification: mariadb-server and python3-pymysql are installed" in mariadb
    assert f"docs:         {DOCS}environment-sql-database-ubuntu.html" in mariadb
    assert "waits for:    openstack-packages on ctl-01" in mariadb
    rabbitmq = text[text.index("rabbitmq  [UNVERIFIED]") :]
    assert "files:        (none listed)" in rabbitmq.split("\n\n", 1)[0]


def test_text_lists_open_decisions(valid_config, catalog) -> None:
    text = format_plan_text(build_plan(valid_config, catalog))
    assert "Open decisions (not steps):" in text
    assert "network-configuration" in text
    assert "netplan" in text


def test_json_output(valid_config, catalog) -> None:
    payload: dict[str, Any] = json.loads(format_plan_json(build_plan(valid_config, catalog)))
    assert payload["cluster"] == "fixture-cluster"
    assert payload["read_only"] is True
    assert payload["node"] is None
    assert payload["summary"] == {"steps": 23, "nodes": 4, "verified": 0, "unverified": 23}
    first, second = payload["steps"][:2]
    assert first == {
        "sequence": 1,
        "node": "ctl-01",
        "role": "controller",
        "id": "hosts-file",
        "title": "Configure name resolution for all cluster nodes in /etc/hosts",
        "files_modified": ["/etc/hosts"],
        "verification": (
            "Every cluster node's hostname resolves to its management IP address on this node."
        ),
        "doc_url": f"{DOCS}environment-networking.html",
        "status": "unverified",
        "waits_for": [],
    }
    assert second["node"] == "cmp-01"
    time_sync = next(s for s in payload["steps"] if s["id"] == "time-sync-controller")
    assert time_sync["waits_for"] == [{"step": "hosts-file", "node": "ctl-01"}]
    other = next(s for s in payload["steps"] if s["id"] == "time-sync-other")
    assert other["waits_for"] == [
        {"step": "hosts-file", "node": "cmp-01"},
        {"step": "time-sync-controller", "node": "ctl-01"},
    ]
    packages = [s for s in payload["steps"] if s["id"] == "openstack-packages"]
    assert [p["waits_for"] for p in packages] == [
        [{"step": "time-sync-controller", "node": "ctl-01"}],
        [{"step": "time-sync-other", "node": "cmp-01"}],
        [{"step": "time-sync-other", "node": "blk-01"}],
        [{"step": "time-sync-other", "node": "obj-01"}],
    ]
    assert [note["id"] for note in payload["notes"]] == ["network-configuration"]


def test_plan_only_reads_the_configuration(valid_config, catalog, tmp_path: Path) -> None:
    before = json.dumps(valid_config, sort_keys=True)
    build_plan(valid_config, catalog)
    assert json.dumps(valid_config, sort_keys=True) == before
