"""Tests for YAML file loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from openstack_forge.config import ConfigFileError, ConfigSyntaxError, load_config_file, parse_yaml


def test_loads_valid_yaml_as_mapping(fixture_path) -> None:
    data = load_config_file(fixture_path("valid_cluster.yaml"))
    assert data["cluster"]["name"] == "fixture-cluster"
    assert len(data["nodes"]) == 4


def test_missing_file_reports_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.yaml"
    with pytest.raises(ConfigFileError, match="not found") as excinfo:
        load_config_file(missing)
    assert str(missing) in str(excinfo.value)


def test_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="not a regular file"):
        load_config_file(tmp_path)


def test_non_utf8_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "latin1.yaml"
    path.write_bytes(b"cluster:\n  name: \xe9\n")
    with pytest.raises(ConfigFileError, match="UTF-8"):
        load_config_file(path)


def test_malformed_yaml_reports_line_and_column(fixture_path) -> None:
    with pytest.raises(ConfigSyntaxError) as excinfo:
        load_config_file(fixture_path("malformed_yaml.yaml"))
    message = str(excinfo.value)
    assert "Invalid YAML" in message
    assert "line 8, column 5" in message
    assert "flow sequence started at line 7" in message


def test_malformed_yaml_does_not_echo_source_line() -> None:
    with pytest.raises(ConfigSyntaxError) as excinfo:
        parse_yaml('password: "s3cr3t\nother: [', source="secret.yaml")
    assert "s3cr3t" not in str(excinfo.value)


def test_empty_file_is_rejected(fixture_path) -> None:
    with pytest.raises(ConfigSyntaxError, match="empty"):
        load_config_file(fixture_path("empty.yaml"))


def test_duplicate_mapping_key_is_rejected(fixture_path) -> None:
    with pytest.raises(ConfigSyntaxError, match="duplicate key 'management'") as excinfo:
        load_config_file(fixture_path("duplicate_yaml_key.yaml"))
    assert "line 13" in str(excinfo.value)


def test_multiple_documents_are_rejected() -> None:
    with pytest.raises(ConfigSyntaxError, match="single document"):
        parse_yaml("cluster: {}\n---\nnodes: []\n")


def test_yaml_merge_keys_are_not_treated_as_duplicates() -> None:
    data = parse_yaml("base: &base {a: 1, b: 2}\nchild:\n  <<: *base\n  b: 3\n")
    assert data["child"] == {"a": 1, "b": 3}


def test_unsafe_yaml_tags_are_rejected() -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_yaml("!!python/object/apply:os.system ['true']")
