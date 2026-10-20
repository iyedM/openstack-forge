"""Exceptions and issue types raised by configuration loading and validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    """A single problem found in a configuration, anchored to a location."""

    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message}"


class ConfigError(Exception):
    """Base class for all configuration errors."""


class ConfigFileError(ConfigError):
    """The configuration file is missing or cannot be read."""


class ConfigSyntaxError(ConfigError):
    """The configuration file is not valid YAML."""


class ConfigValidationError(ConfigError):
    """The configuration was parsed but does not satisfy the schema or semantic rules."""

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"{len(self.issues)} validation error(s)")
