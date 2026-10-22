"""Unit tests for pure diagnostic checks and node identification."""

from __future__ import annotations

import pytest

from openstack_forge.diagnostics import (
    BYTES_PER_GIB,
    CheckStatus,
    NetworkInterface,
    NodeIdentificationError,
    check_cpu_virtualization,
    check_disk,
    check_interfaces,
    check_object_disks,
    check_os,
    check_provider_interface,
    check_provider_interface_link,
    check_ram,
    check_vcpus,
    check_virt_environment,
    identify_node,
    parse_cpuinfo_flags,
    parse_ip_addr,
    parse_lsblk,
    parse_meminfo,
    parse_os_release,
)

# --- node identification (3 ways + ambiguous + not found) ----------------------


def test_identify_node_by_cli_option(valid_config) -> None:
    identified = identify_node(
        valid_config["nodes"],
        requested_node="CMP-01",
        local_hostname="ctl-01",
        local_ipv4s=["192.0.2.11"],
    )
    assert identified.node["hostname"] == "cmp-01"
    assert identified.identified_by == "cli"


def test_identify_node_by_cli_option_not_found(valid_config) -> None:
    with pytest.raises(NodeIdentificationError, match="Node 'unknown-01' was not found"):
        identify_node(valid_config["nodes"], requested_node="unknown-01")


def test_identify_node_by_local_hostname(valid_config) -> None:
    identified = identify_node(
        valid_config["nodes"],
        local_hostname="blk-01.cluster.test",
        local_ipv4s=["192.0.2.11"],
    )
    assert identified.node["hostname"] == "blk-01"
    assert identified.identified_by == "hostname"


def test_identify_node_by_local_ipv4(valid_config) -> None:
    identified = identify_node(
        valid_config["nodes"],
        local_hostname="unconfigured-host",
        local_ipv4s=["127.0.0.1", "192.0.2.32"],
    )
    assert identified.node["hostname"] == "obj-01"
    assert identified.identified_by == "management_ip"


def test_identify_node_ambiguous_local_ipv4(valid_config) -> None:
    with pytest.raises(NodeIdentificationError, match="ambiguous.*ctl-01.*cmp-01"):
        identify_node(
            valid_config["nodes"],
            local_hostname="bastion",
            local_ipv4s=["192.0.2.11", "192.0.2.21"],
        )


def test_identify_node_ambiguous_hostname(valid_config) -> None:
    duplicate_nodes = [valid_config["nodes"][0], dict(valid_config["nodes"][0])]
    with pytest.raises(NodeIdentificationError, match="ambiguous"):
        identify_node(duplicate_nodes, local_hostname="ctl-01")


def test_identify_node_not_found(valid_config) -> None:
    with pytest.raises(NodeIdentificationError, match="Pass --node HOSTNAME"):
        identify_node(
            valid_config["nodes"],
            local_hostname="laptop",
            local_ipv4s=["10.254.0.5"],
        )


# --- OS check ------------------------------------------------------------------


def test_check_os_pass(doctor_fixture_text) -> None:
    os_release = parse_os_release(doctor_fixture_text("os_release_ubuntu_2404.txt"))
    result = check_os(os_release, "24.04")
    assert result.status is CheckStatus.PASS
    assert result.declared == "Ubuntu 24.04"
    assert result.observed == "Ubuntu 24.04"


def test_check_os_fail_wrong_ubuntu_version(doctor_fixture_text) -> None:
    os_release = parse_os_release(doctor_fixture_text("os_release_ubuntu_2204.txt"))
    result = check_os(os_release, "24.04")
    assert result.status is CheckStatus.FAIL
    assert result.declared == "Ubuntu 24.04"
    assert result.observed == "Ubuntu 22.04"
    assert "expected Ubuntu 24.04" in result.message


def test_check_os_fail_non_ubuntu(doctor_fixture_text) -> None:
    os_release = parse_os_release(doctor_fixture_text("os_release_debian_12.txt"))
    result = check_os(os_release, "24.04")
    assert result.status is CheckStatus.FAIL
    assert "Debian GNU/Linux 12" in result.observed


# --- vCPUs and RAM checks ------------------------------------------------------


def test_check_vcpus_pass_and_fail() -> None:
    passed = check_vcpus(observed_vcpus=16, declared_vcpus=8)
    assert passed.status is CheckStatus.PASS
    assert passed.declared == "8 vCPU(s)"
    assert passed.observed == "16 vCPU(s)"

    failed = check_vcpus(observed_vcpus=4, declared_vcpus=8)
    assert failed.status is CheckStatus.FAIL
    assert "below declared 8 vCPU(s)" in failed.message


def test_check_ram_allows_kernel_reserved_tolerance(doctor_fixture_text) -> None:
    # 16121856 kB is ~15.38 GiB, which is within the 10% kernel-reserved tolerance for 16 GB.
    mem_bytes = parse_meminfo(doctor_fixture_text("meminfo_16gb.txt"))
    result = check_ram(mem_bytes, declared_ram_gb=16)
    assert result.status is CheckStatus.PASS
    assert result.declared == "16 GB"
    assert result.observed == "15.4 GiB"


def test_check_ram_fails_when_below_tolerance(doctor_fixture_text) -> None:
    mem_bytes = parse_meminfo(doctor_fixture_text("meminfo_8gb.txt"))
    result = check_ram(mem_bytes, declared_ram_gb=16)
    assert result.status is CheckStatus.FAIL
    assert result.declared == "16 GB"
    assert "below declared 16 GB" in result.message


def test_check_ram_boundary() -> None:
    # Exactly 90% of 10 GiB passes; 1 byte less fails.
    exact_min = int(10 * BYTES_PER_GIB * 0.90)
    assert check_ram(exact_min, declared_ram_gb=10).status is CheckStatus.PASS
    assert check_ram(exact_min - 1, declared_ram_gb=10).status is CheckStatus.FAIL


# --- virtualization checks -----------------------------------------------------


def test_check_virt_environment_physical_and_vm() -> None:
    phys = check_virt_environment("none")
    assert phys.status is CheckStatus.PASS
    assert phys.observed == "physical (none)"
    assert "physical hardware" in phys.message

    vm = check_virt_environment("kvm")
    assert vm.status is CheckStatus.PASS
    assert vm.observed == "virtual (kvm)"
    assert "virtual machine" in vm.message


def test_check_cpu_virtualization_vmx_svm_and_missing(doctor_fixture_text) -> None:
    vmx_res = check_cpu_virtualization(parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_vmx.txt")))
    assert vmx_res.status is CheckStatus.PASS
    assert vmx_res.observed == "vmx"

    svm_res = check_cpu_virtualization(parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_svm.txt")))
    assert svm_res.status is CheckStatus.PASS
    assert svm_res.observed == "svm"

    warn_res = check_cpu_virtualization(
        parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_no_virt.txt"))
    )
    assert warn_res.status is CheckStatus.WARN
    assert warn_res.observed == "none"
    assert "virt_type=qemu" in warn_res.message


# --- block and object disk checks ----------------------------------------------


def test_check_disk_pass_by_path_and_by_id(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))

    sdb = check_disk("/dev/sdb", devices)
    assert sdb.status is CheckStatus.PASS
    assert "empty disk" in sdb.observed

    by_id = check_disk("/dev/disk/by-id/wwn-0x5000c500a1b2c3d4", devices)
    assert by_id.status is CheckStatus.PASS
    assert "/dev/sdc" in by_id.observed


def test_check_disk_fail_missing(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))
    result = check_disk("/dev/sdz", devices)
    assert result.status is CheckStatus.FAIL
    assert result.observed == "missing"
    assert "does not exist" in result.message


@pytest.mark.parametrize(
    ("disk_path", "expected_snippet"),
    [
        ("/dev/sda", "root disk"),
        ("/dev/sdb", "has partition(s) (sdb1)"),
        ("/dev/sdb", "has a partition table (gpt)"),
        ("/dev/sdg", "has a partition table (dos)"),
        ("/dev/sdh", "has a partition table (gpt)"),
        ("/dev/sdc", "has filesystem (xfs)"),
        ("/dev/sdd", "has mountpoint(s) (/srv/data)"),
        ("/dev/sde", "has LVM/RAID signature (LVM2_member"),
        ("/dev/sdf", "has LVM/RAID signature (linux_raid_member"),
        ("/dev/loop0", "has type 'loop', expected a block 'disk'"),
        ("/dev/sda1", "has type 'part', expected a block 'disk'"),
    ],
)
def test_check_disk_fail_reasons(
    doctor_fixture_text, disk_path: str, expected_snippet: str
) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_non_empty.json"))
    result = check_disk(disk_path, devices)
    assert result.status is CheckStatus.FAIL
    assert expected_snippet in result.message


def test_check_disk_partition_table_without_partitions_fails(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_non_empty.json"))
    result = check_disk("/dev/sdg", devices)
    assert result.status is CheckStatus.FAIL
    assert result.observed == "/dev/sdg (partition table: dos)"
    assert result.message == "Disk /dev/sdg is not empty/usable: has a partition table (dos)."


def test_check_disk_matches_resolved_symlink(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))
    declared = "/dev/disk/by-id/ata-QEMU_HARDDISK_QM00003"
    # lsblk knows nothing about this link; only the resolved path can match.
    assert check_disk(declared, devices).observed == "missing"

    result = check_disk(declared, devices, resolved_disk="/dev/sdb")
    assert result.status is CheckStatus.PASS
    assert result.declared == f"{declared} (empty disk)"
    assert result.observed.startswith("/dev/sdb (empty disk")


def test_check_disk_missing_reports_resolved_path(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))
    result = check_disk("/dev/disk/by-id/ata-GONE", devices, resolved_disk="/dev/sdz")
    assert result.status is CheckStatus.FAIL
    assert result.observed == "missing"
    assert result.message == (
        "Declared disk /dev/disk/by-id/ata-GONE (resolves to /dev/sdz) "
        "does not exist in lsblk output."
    )


def test_check_object_disks_uses_resolved_paths(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_non_empty.json"))
    results = check_object_disks(
        ["/dev/disk/by-id/scsi-0QEMU_disk_a", "/dev/disk/by-id/scsi-0QEMU_disk_b"],
        devices,
        resolved_disks={
            "/dev/disk/by-id/scsi-0QEMU_disk_a": "/dev/sdh",
            "/dev/disk/by-id/scsi-0QEMU_disk_b": None,
        },
    )
    assert [r.status for r in results] == [CheckStatus.FAIL, CheckStatus.FAIL]
    assert "has a partition table (gpt)" in results[0].message
    assert results[1].observed == "missing"


def test_check_object_disks_multiple_devices(doctor_fixture_text) -> None:
    clean_devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))
    results = check_object_disks(
        ["/dev/sdb", "/dev/disk/by-id/wwn-0x5000c500a1b2c3d4"],
        clean_devices,
    )
    assert [r.name for r in results] == [
        "disk:/dev/sdb",
        "disk:/dev/disk/by-id/wwn-0x5000c500a1b2c3d4",
    ]
    assert all(r.status is CheckStatus.PASS for r in results)

    non_empty_devices = parse_lsblk(doctor_fixture_text("lsblk_non_empty.json"))
    mixed = check_object_disks(["/dev/sdb", "/dev/sdc"], non_empty_devices)
    assert [r.status for r in mixed] == [CheckStatus.FAIL, CheckStatus.FAIL]


# --- network interface checks --------------------------------------------------


def test_check_interfaces_pass(valid_config, doctor_fixture_text) -> None:
    host_ifaces = parse_ip_addr(doctor_fixture_text("ip_addr_controller.json"))
    result = check_interfaces(
        valid_config["nodes"][0],
        valid_config["networks"],
        host_ifaces,
    )
    assert result.status is CheckStatus.PASS
    assert "eth0" in result.message
    assert "192.0.2.11" in result.message


def test_check_interfaces_fail_missing_interface(valid_config, doctor_fixture_text) -> None:
    host_ifaces = parse_ip_addr(doctor_fixture_text("ip_addr_missing_iface.json"))
    result = check_interfaces(
        valid_config["nodes"][0],
        valid_config["networks"],
        host_ifaces,
    )
    assert result.status is CheckStatus.FAIL
    assert "eth1, eth2" in result.message


def test_check_interfaces_fail_wrong_management_ip(valid_config, doctor_fixture_text) -> None:
    host_ifaces = parse_ip_addr(doctor_fixture_text("ip_addr_wrong_mgmt_ip.json"))
    result = check_interfaces(
        valid_config["nodes"][0],
        valid_config["networks"],
        host_ifaces,
    )
    assert result.status is CheckStatus.FAIL
    assert "does not carry management_ip 192.0.2.11" in result.message
    assert "192.0.2.99" in result.message


def test_check_provider_interface_pass_and_warn(valid_config) -> None:
    node = valid_config["nodes"][0]
    networks = valid_config["networks"]

    passed = check_provider_interface(node, networks)
    assert passed.status is CheckStatus.PASS
    assert "management=eth0, provider=eth2" in passed.observed

    node["interfaces"]["provider"] = "eth0"
    warned = check_provider_interface(node, networks)
    assert warned.status is CheckStatus.WARN
    assert "management=eth0, provider=eth0" in warned.observed
    assert "bridge migration" in warned.message


def test_check_provider_interface_link_state_and_management_ip_warnings(valid_config) -> None:
    node = valid_config["nodes"][0]
    networks = valid_config["networks"]

    # 1. Provider interface link UP and unnumbered -> PASS
    ifaces_up = [
        NetworkInterface(name="eth0", operstate="UP", ipv4_addresses=("192.0.2.11",)),
        NetworkInterface(name="eth1", operstate="UP", ipv4_addresses=("198.51.100.11",)),
        NetworkInterface(name="eth2", operstate="UP", ipv4_addresses=()),
    ]
    res_up = check_provider_interface(node, networks, host_interfaces=ifaces_up)
    assert res_up.status is CheckStatus.PASS
    assert "operstate=UP" in res_up.observed

    # 2. Provider interface link DOWN -> WARN with operator instruction
    ifaces_down = [
        NetworkInterface(name="eth0", operstate="UP", ipv4_addresses=("192.0.2.11",)),
        NetworkInterface(name="eth1", operstate="UP", ipv4_addresses=("198.51.100.11",)),
        NetworkInterface(name="eth2", operstate="DOWN", ipv4_addresses=()),
    ]
    res_down = check_provider_interface(node, networks, host_interfaces=ifaces_down)
    assert res_down.status is CheckStatus.WARN
    assert "operstate=DOWN" in res_down.observed
    assert "operator must bring the link up" in res_down.message
    assert "does not run netplan or ip link" in res_down.message
    link_down = check_provider_interface_link(node, networks, ifaces_down)
    assert link_down is not None and link_down.status is CheckStatus.WARN

    # 3. Provider interface carries management_ip -> WARN
    ifaces_mgmt_ip = [
        NetworkInterface(name="eth0", operstate="UP", ipv4_addresses=("192.0.2.11",)),
        NetworkInterface(name="eth2", operstate="UP", ipv4_addresses=("192.0.2.11",)),
    ]
    res_mgmt_ip = check_provider_interface(node, networks, host_interfaces=ifaces_mgmt_ip)
    assert res_mgmt_ip.status is CheckStatus.WARN
    assert "carries management_ip 192.0.2.11" in res_mgmt_ip.message
