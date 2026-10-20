"""Safe YAML loading for cluster configuration files."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from openstack_forge.config.errors import ConfigFileError, ConfigSyntaxError

_MERGE_TAG = "tag:yaml.org,2002:merge"


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, Hashable):
                if key in seen:
                    raise ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def read_config_text(path: Path) -> str:
    """Read a configuration file, translating OS errors into ConfigFileError."""
    if not path.exists():
        raise ConfigFileError(f"Configuration file not found: {path}")
    if not path.is_file():
        raise ConfigFileError(f"Configuration path is not a regular file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigFileError(f"Configuration file is not valid UTF-8 text: {path}") from exc
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise ConfigFileError(f"Cannot read configuration file {path}: {reason}") from exc


def parse_yaml(text: str, source: str = "<string>") -> Any:
    """Parse a single YAML document safely; raise ConfigSyntaxError on malformed input."""
    try:
        # Safe: _UniqueKeySafeLoader only adds a duplicate-key check on top of SafeLoader.
        data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.MarkedYAMLError as exc:
        raise ConfigSyntaxError(_describe_yaml_error(exc, source)) from exc
    except yaml.YAMLError as exc:
        raise ConfigSyntaxError(f"Invalid YAML in {source}: {exc}") from exc
    if data is None:
        raise ConfigSyntaxError(f"Configuration file is empty: {source}")
    return data


def load_config_file(path: str | Path) -> Any:
    """Read and parse a YAML configuration file without validating its contents."""
    config_path = Path(path)
    return parse_yaml(read_config_text(config_path), source=str(config_path))


def _describe_yaml_error(exc: yaml.MarkedYAMLError, source: str) -> str:
    # The offending source line is deliberately not echoed, so a malformed line
    # holding a secret is never printed to the terminal.
    mark = exc.problem_mark or exc.context_mark
    message = f"Invalid YAML in {source}{_describe_mark(mark)}: "
    message += exc.problem or exc.context or "malformed document"
    if exc.problem and exc.context and exc.context_mark:
        message += f" ({exc.context} started{_describe_mark(exc.context_mark)})"
    return message


def _describe_mark(mark: yaml.Mark | None) -> str:
    return f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
