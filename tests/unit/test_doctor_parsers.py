"""Unit tests for pure host output parsers in ``openstack_forge.diagnostics.parsers``."""

from __future__ import annotations

import json

import pytest

from openstack_forge.diagnostics import (
    ParseError,
    parse_cpuinfo_flags,
    parse_hostname,
    parse_ip_addr,
    parse_lsblk,
    parse_meminfo,
    parse_nproc,
    parse_os_release,
    parse_readlink,
    parse_virt,
)


def test_parse_os_release_ubuntu_2404(doctor_fixture_text) -> None:
    os_release = parse_os_release(doctor_fixture_text("os_release_ubuntu_2404.txt"))
    assert os_release.id == "ubuntu"
    assert os_release.version_id == "24.04"
    assert os_release.name == "Ubuntu"
    assert "24.04.1 LTS" in os_release.pretty_name


def test_parse_os_release_single_quotes_and_comments() -> None:
    text = "# comment\nID='ubuntu'\nVERSION_ID='24.04'\nNAME=\"Ubuntu \\\"Noble\\\"\"\n"
    os_release = parse_os_release(text)
    assert os_release.id == "ubuntu"
    assert os_release.version_id == "24.04"
    assert os_release.name == 'Ubuntu "Noble"'


@pytest.mark.parametrize("bad_input", ["", "# only comment\n", "NOT_A_KEY_VALUE\n", "FOO=bar\n"])
def test_parse_os_release_invalid(bad_input: str) -> None:
    with pytest.raises(ParseError):
        parse_os_release(bad_input)


def test_parse_nproc_valid() -> None:
    assert parse_nproc("16\n") == 16


@pytest.mark.parametrize("bad_input", ["", "0\n", "-4\n", "eight\n"])
def test_parse_nproc_invalid(bad_input: str) -> None:
    with pytest.raises(ParseError):
        parse_nproc(bad_input)


def test_parse_meminfo_fixtures(doctor_fixture_text) -> None:
    assert parse_meminfo(doctor_fixture_text("meminfo_64gb.txt")) == 65850368 * 1024
    assert parse_meminfo(doctor_fixture_text("meminfo_16gb.txt")) == 16121856 * 1024


def test_parse_meminfo_bytes_and_invalid() -> None:
    assert parse_meminfo("MemTotal: 1073741824 B\n") == 1073741824
    with pytest.raises(ParseError, match="MemTotal not found"):
        parse_meminfo("MemFree: 1024 kB\n")
    with pytest.raises(ParseError, match="Unsupported MemTotal unit"):
        parse_meminfo("MemTotal: 1024 tb\n")
    with pytest.raises(ParseError, match="Invalid MemTotal value"):
        parse_meminfo("MemTotal: nope kB\n")


def test_parse_cpuinfo_flags(doctor_fixture_text) -> None:
    vmx_flags = parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_vmx.txt"))
    assert "vmx" in vmx_flags
    assert "svm" not in vmx_flags

    svm_flags = parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_svm.txt"))
    assert "svm" in svm_flags
    assert "vmx" not in svm_flags

    no_virt_flags = parse_cpuinfo_flags(doctor_fixture_text("cpuinfo_no_virt.txt"))
    assert "vmx" not in no_virt_flags
    assert "svm" not in no_virt_flags


def test_parse_virt() -> None:
    assert parse_virt("kvm\n", returncode=0) == "kvm"
    assert parse_virt("none\n", returncode=1) == "none"
    assert parse_virt("", returncode=0) == "none"
    with pytest.raises(ParseError):
        parse_virt("", returncode=1)


def test_parse_lsblk_clean(doctor_fixture_text) -> None:
    devices = parse_lsblk(doctor_fixture_text("lsblk_clean.json"))
    assert [dev.name for dev in devices] == ["sda", "sdb", "sdc"]
    assert devices[0].children[1].mountpoints == ("/",)
    assert devices[1].size == 536870912000
    assert devices[2].wwn == "0x5000c500a1b2c3d4"
    assert devices[0].pttype == "gpt"
    assert devices[1].pttype is None


def test_parse_lsblk_pttype_without_partitions(doctor_fixture_text) -> None:
    devices = {dev.name: dev for dev in parse_lsblk(doctor_fixture_text("lsblk_non_empty.json"))}
    assert devices["sdg"].pttype == "dos"
    assert devices["sdg"].children == ()
    assert devices["sdh"].pttype == "gpt"
    assert devices["sdc"].pttype is None


@pytest.mark.parametrize("raw_pttype", [None, "", "  "])
def test_parse_lsblk_blank_pttype_is_none(raw_pttype: str | None) -> None:
    raw = json.dumps(
        {"blockdevices": [{"name": "vdb", "type": "disk", "size": 1, "pttype": raw_pttype}]}
    )
    (device,) = parse_lsblk(raw)
    assert device.pttype is None


def test_parse_readlink() -> None:
    assert parse_readlink("/dev/sdb\n") == "/dev/sdb"
    assert parse_readlink("\n  /dev/nvme0n1  \n") == "/dev/nvme0n1"


@pytest.mark.parametrize("bad_output", ["", "\n", "sdb\n", "../sdb\n"])
def test_parse_readlink_invalid(bad_output: str) -> None:
    with pytest.raises(ParseError):
        parse_readlink(bad_output)


def test_parse_lsblk_legacy_mountpoint_and_string_size() -> None:
    raw = (
        '{"blockdevices": [{"name": "vda", "type": "disk", '
        '"size": "21474836480", "mountpoint": "/"}]}'
    )
    (device,) = parse_lsblk(raw)
    assert device.path == "/dev/vda"
    assert device.size == 21474836480
    assert device.mountpoints == ("/",)


@pytest.mark.parametrize(
    "bad_json",
    [
        "not json",
        "[]",
        '{"blockdevices": "not-a-list"}',
        '{"blockdevices": [{"type": "disk"}]}',
        '{"blockdevices": [{"name": "sda"}]}',
    ],
)
def test_parse_lsblk_invalid(bad_json: str) -> None:
    with pytest.raises(ParseError):
        parse_lsblk(bad_json)


def test_parse_ip_addr(doctor_fixture_text) -> None:
    interfaces = parse_ip_addr(doctor_fixture_text("ip_addr_controller.json"))
    by_name = {iface.name: iface for iface in interfaces}
    assert set(by_name) == {"lo", "eth0", "eth1", "eth2"}
    assert by_name["eth0"].ipv4_addresses == ("192.0.2.11",)
    assert by_name["eth2"].ipv4_addresses == ()


@pytest.mark.parametrize(
    "bad_json",
    [
        "{}",
        "not json",
        '[{"operstate": "UP"}]',
        '[{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "999.999.1.1"}]}]',
    ],
)
def test_parse_ip_addr_invalid(bad_json: str) -> None:
    with pytest.raises(ParseError):
        parse_ip_addr(bad_json)


def test_parse_hostname() -> None:
    assert parse_hostname("ctl-01\n") == "ctl-01"
    with pytest.raises(ParseError):
        parse_hostname("   \n")
