"""Password and secret generation, 0600 storage, and redaction registration.

Official sources read for this module:
- ``https://docs.openstack.org/install-guide/environment-security.html``
  (source: ``doc/install-guide/source/environment-security.rst`` in
  ``openstack/openstack-manuals``).
- ``https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html``
  (source: ``doc/source/install/install-ubuntu.rst`` in ``openstack/placement``).

The "Passwords" table on ``environment-security.html`` has 16 rows:
- The first row is ``Database password (no variable used)`` ("Root password for the
  database"), which is not used on Ubuntu where MariaDB's ``root`` account uses
  ``unix_socket`` authentication.
- The remaining 15 rows define the named password variables in
  :data:`SECURITY_TABLE_SECRET_NAMES`.

Explicit documented extras (:data:`EXTRA_SECRET_SPECS`):
- ``PLACEMENT_DBPASS`` is omitted from the ``environment-security.html`` summary table, but
  the Placement 2025.1 Ubuntu installation guide
  (``https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html``) requires it
  both in ``GRANT ALL PRIVILEGES ON placement.* TO 'placement'@'localhost' IDENTIFIED BY
  'PLACEMENT_DBPASS';`` (and ``'placement'@'%'``) and in ``/etc/placement/placement.conf``
  under ``[placement_database] connection =
  mysql+pymysql://placement:PLACEMENT_DBPASS@controller/placement``.
- Mechanism for future secrets: names are never invented silently. When a future service
  guide uses a password variable absent from ``environment-security.html`` (for example
  ``SWIFT_PASS`` in the Object Storage installation guide), add a :class:`SecretSpec` with
  its exact uppercase name, human description, and ``https://docs.openstack.org/...``
  ``source_url`` to :data:`EXTRA_SECRET_SPECS` (or pass ``extra_specs`` to
  :func:`build_secret_specs` / :class:`SecretStore`). :class:`SecretSpec` rejects any entry
  missing a description or an official ``https://docs.openstack.org/`` URL.

Why generated secrets are at least 32 characters of ``[A-Za-z0-9]`` only:
1. OpenStack ``.ini`` configuration files (parsed by ``oslo.config`` / ``ConfigParser``)
   treat ``#`` and ``;`` as comment markers, ``$`` as interpolation syntax, and quotes or
   whitespace specially.
2. Database and message-broker connection URLs such as
   ``mysql+pymysql://keystone:KEYSTONE_DBPASS@controller/keystone`` and
   ``rabbit://openstack:RABBIT_PASS@controller`` break if the password contains URI
   delimiters (``:``, ``@``, ``/``, ``?``, ``#``, ``%``).
3. :func:`openstack_forge.execution.commands.validate_parameter` rejects leading ``-``
   (option injection), whitespace/control characters, and shell/punctuation metacharacters
   (``;|&$`"'\\<>(){}[]*?!#~%^,``) in every command parameter.

Using 32 characters drawn with :func:`secrets.choice` from the 62-character alphabet
``[A-Za-z0-9]`` (~190.5 bits of entropy) satisfies all three layers without escaping.

No ``.orig`` backup or leftover temporary file:
:meth:`SecretStore.ensure` writes the store through :func:`atomic_write_text` without calling
:func:`backup_once`, so ``secrets.json.orig`` is never created, and the ``0600`` temporary
file ``.secrets.json.*.tmp`` created by :func:`tempfile.mkstemp` is either atomically
renamed over ``secrets.json`` or unlinked on error.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import string
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from openstack_forge.execution.errors import SecretError, UnsafePathError
from openstack_forge.execution.fsutil import atomic_write_text, resolve_target
from openstack_forge.execution.redaction import register_secrets

DEFAULT_SECRETS_TARGET = "/etc/openstack-forge/secrets.json"
SECRETS_DIR_MODE = 0o700
SECRETS_FILE_MODE = 0o600
MIN_SECRET_LENGTH = 32
DEFAULT_SECRET_LENGTH = 32
MAX_SECRET_LENGTH = 256
SECRET_ALPHABET = string.ascii_letters + string.digits
_SECRET_VALUE_RE = re.compile(rf"^[A-Za-z0-9]{{{MIN_SECRET_LENGTH},{MAX_SECRET_LENGTH}}}$")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
OFFICIAL_DOCS_PREFIX = "https://docs.openstack.org/"

ENVIRONMENT_SECURITY_URL = "https://docs.openstack.org/install-guide/environment-security.html"
PLACEMENT_INSTALL_URL = "https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html"


@dataclass(frozen=True)
class SecretSpec:
    """One named OpenStack secret variable, its description, and its official source URL."""

    name: str
    description: str
    source_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SECRET_NAME_RE.fullmatch(self.name):
            raise SecretError(
                f"Invalid secret name {self.name!r}: must match {_SECRET_NAME_RE.pattern}"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise SecretError(f"Secret {self.name!r} must have a non-empty description")
        if not isinstance(self.source_url, str) or not self.source_url.startswith(
            OFFICIAL_DOCS_PREFIX
        ):
            raise SecretError(
                f"Secret {self.name!r} source_url must start with {OFFICIAL_DOCS_PREFIX!r}, "
                f"got {self.source_url!r}"
            )


# Exact 15 named variables from the "Passwords" table on
# https://docs.openstack.org/install-guide/environment-security.html
SECURITY_TABLE_SECRET_DESCRIPTIONS: Mapping[str, str] = {
    "ADMIN_PASS": "Password of user admin",
    "CINDER_DBPASS": "Database password for the Block Storage service",
    "CINDER_PASS": "Password of Block Storage service user cinder",
    "DASH_DBPASS": "Database password for the Dashboard",
    "DEMO_PASS": "Password of user demo",
    "GLANCE_DBPASS": "Database password for Image service",
    "GLANCE_PASS": "Password of Image service user glance",
    "KEYSTONE_DBPASS": "Database password of Identity service",
    "METADATA_SECRET": "Secret for the metadata proxy",
    "NEUTRON_DBPASS": "Database password for the Networking service",
    "NEUTRON_PASS": "Password of Networking service user neutron",
    "NOVA_DBPASS": "Database password for Compute service",
    "NOVA_PASS": "Password of Compute service user nova",
    "PLACEMENT_PASS": "Password of the Placement service user placement",
    "RABBIT_PASS": "Password of RabbitMQ user openstack",
}

SECURITY_TABLE_SECRET_NAMES: tuple[str, ...] = tuple(SECURITY_TABLE_SECRET_DESCRIPTIONS)

# Explicit documented extras omitted from environment-security.html but required by
# individual OpenStack 2025.1 service installation guides. Add future extras (such as
# SWIFT_PASS) here with their official https://docs.openstack.org/... URL and description.
EXTRA_SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec(
        name="PLACEMENT_DBPASS",
        description="Database password for the Placement service",
        source_url=PLACEMENT_INSTALL_URL,
    ),
)

EXTRA_SECRET_NAMES: tuple[str, ...] = tuple(spec.name for spec in EXTRA_SECRET_SPECS)


def build_secret_specs(
    extra_specs: Iterable[SecretSpec] = EXTRA_SECRET_SPECS,
) -> dict[str, SecretSpec]:
    """Combine the 15 ``environment-security.html`` secrets with documented ``extra_specs``."""
    combined: dict[str, SecretSpec] = {
        name: SecretSpec(
            name=name,
            description=description,
            source_url=ENVIRONMENT_SECURITY_URL,
        )
        for name, description in SECURITY_TABLE_SECRET_DESCRIPTIONS.items()
    }
    for spec in extra_specs:
        if not isinstance(spec, SecretSpec):
            raise SecretError(f"Extra secret entry must be a SecretSpec, got {type(spec).__name__}")
        if spec.name in combined:
            raise SecretError(f"Duplicate secret name {spec.name!r}")
        combined[spec.name] = spec
    return dict(sorted(combined.items()))


SECRET_SPECS: Mapping[str, SecretSpec] = build_secret_specs()
SECRET_DESCRIPTIONS: Mapping[str, str] = {
    name: spec.description for name, spec in SECRET_SPECS.items()
}
SECRET_SOURCES: Mapping[str, str] = {name: spec.source_url for name, spec in SECRET_SPECS.items()}
SECRET_NAMES: tuple[str, ...] = tuple(SECRET_SPECS)


def generate_secret(length: int = DEFAULT_SECRET_LENGTH) -> str:
    """Generate one random alphanumeric secret of ``length`` characters (>= 32)."""
    if length < MIN_SECRET_LENGTH or length > MAX_SECRET_LENGTH:
        raise ValueError(
            f"Secret length must be between {MIN_SECRET_LENGTH} and {MAX_SECRET_LENGTH}, "
            f"got {length}"
        )
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(length))


def validate_secret_name(
    name: object,
    specs: Mapping[str, SecretSpec] = SECRET_SPECS,
) -> str:
    """Return ``name`` if it is in ``specs`` (default :data:`SECRET_SPECS`), else raise."""
    if not isinstance(name, str) or name not in specs:
        known = ", ".join(specs)
        raise SecretError(f"Unknown secret name {name!r} (expected one of: {known})")
    return name


def validate_secret_value(name: str, value: object) -> str:
    """Validate one secret value without ever echoing the value in an error message."""
    if not isinstance(value, str):
        raise SecretError(f"Secret {name!r} must be a string, got {type(value).__name__}")
    if len(value) < MIN_SECRET_LENGTH:
        raise SecretError(
            f"Secret {name!r} is too short (length {len(value)}; "
            f"minimum is {MIN_SECRET_LENGTH} characters)"
        )
    if len(value) > MAX_SECRET_LENGTH:
        raise SecretError(
            f"Secret {name!r} is too long (length {len(value)}; "
            f"maximum is {MAX_SECRET_LENGTH} characters)"
        )
    if not _SECRET_VALUE_RE.fullmatch(value):
        raise SecretError(
            f"Secret {name!r} contains characters outside [A-Za-z0-9]; "
            "only ASCII letters and digits are permitted"
        )
    return value


@dataclass(frozen=True)
class EnsureResult:
    """Summary of :meth:`SecretStore.ensure`; contains secret names only, never values."""

    path: str
    created: tuple[str, ...]
    kept: tuple[str, ...]
    wrote_file: bool


def _normalize_target_under_root(root: Path, target: str | Path | PurePosixPath) -> str:
    """Allow ``target`` to be a target path (``/etc/...``) or a path already inside ``root``."""
    target_str = str(target)
    try:
        root_resolved = Path(root).resolve(strict=True)
    except FileNotFoundError:
        return target_str
    if root_resolved != Path("/"):
        pure_target = PurePosixPath(target_str)
        pure_root = PurePosixPath(str(root_resolved))
        if pure_target.is_relative_to(pure_root):
            rel = pure_target.relative_to(pure_root)
            if rel.parts:
                return "/" + "/".join(rel.parts)
    return target_str


class SecretStore:
    """Manage the ``0600`` JSON secrets file under ``root``.

    Every call to :meth:`load`, :meth:`ensure`, or :meth:`get` registers the loaded/created
    secret values with :func:`openstack_forge.execution.redaction.register_secrets` so that
    exact-value redaction is active before any step uses them.
    """

    def __init__(
        self,
        root: Path = Path("/"),
        target: str | Path | PurePosixPath = DEFAULT_SECRETS_TARGET,
        *,
        euid: Callable[[], int] = os.geteuid,
        generator: Callable[[], str] = generate_secret,
        extra_specs: Iterable[SecretSpec] | None = None,
    ) -> None:
        self.root = Path(root)
        normalized_target = _normalize_target_under_root(self.root, target)
        self.path, self.display_path = resolve_target(self.root, normalized_target)
        self.display_dir = str(PurePosixPath(self.display_path).parent)
        self._euid = euid
        self._generator = generator
        self.specs: Mapping[str, SecretSpec] = (
            SECRET_SPECS
            if extra_specs is None
            else build_secret_specs((*EXTRA_SECRET_SPECS, *extra_specs))
        )
        self.secret_names: tuple[str, ...] = tuple(self.specs)
        self._cache_stat_key: tuple[int, int, int] | None = None
        self._cached_secrets: dict[str, str] | None = None

    def ensure(self) -> EnsureResult:
        """Create missing secrets and keep existing ones; never overwrite existing values."""
        self._reject_symlinks()
        parent = self.path.parent

        if self.path.exists():
            if not parent.exists():  # pragma: no cover - defensive
                raise SecretError(f"Secrets directory {self.display_dir} does not exist")
            self._check_directory()
            file_stat = self._check_file()
            existing = self._read_and_validate(file_stat)
        else:
            if parent.exists():
                self._check_directory()
            else:
                parent.parent.mkdir(parents=True, exist_ok=True)
                parent.mkdir(mode=SECRETS_DIR_MODE, exist_ok=False)
                os.chmod(parent, SECRETS_DIR_MODE)
                self._check_directory()
            existing = {}

        missing = tuple(name for name in self.secret_names if name not in existing)
        kept = tuple(name for name in self.secret_names if name in existing)

        if not missing:
            register_secrets(existing.values())
            return EnsureResult(
                path=self.display_path,
                created=(),
                kept=kept,
                wrote_file=False,
            )

        merged: dict[str, str] = {}
        for name in self.secret_names:
            if name in existing:
                merged[name] = existing[name]
            else:
                merged[name] = validate_secret_value(name, self._generator())

        register_secrets(merged.values())
        payload = json.dumps(merged, indent=2, sort_keys=True) + "\n"
        atomic_write_text(self.path, payload, mode=SECRETS_FILE_MODE)
        self._check_directory()
        new_stat = self._check_file()
        self._cache_stat_key = (new_stat.st_ino, new_stat.st_mtime_ns, new_stat.st_size)
        self._cached_secrets = dict(merged)
        return EnsureResult(
            path=self.display_path,
            created=missing,
            kept=kept,
            wrote_file=True,
        )

    def load(self, *, require_complete: bool = True) -> dict[str, str]:
        """Load, validate, and register all secrets from the store."""
        self._reject_symlinks()
        if not self.path.parent.exists():
            raise SecretError(
                f"Secrets directory {self.display_dir} does not exist; "
                "run 'openstack-forge secrets init' first"
            )
        self._check_directory()
        if not self.path.exists():
            raise SecretError(
                f"Secrets file {self.display_path} does not exist; "
                "run 'openstack-forge secrets init' first"
            )
        file_stat = self._check_file()
        loaded = self._read_and_validate(file_stat)
        register_secrets(loaded.values())
        if require_complete:
            missing = [name for name in self.secret_names if name not in loaded]
            if missing:
                raise SecretError(
                    f"Secrets file {self.display_path} is missing required secret(s): "
                    f"{', '.join(missing)}"
                )
        return dict(loaded)

    def get(self, name: str) -> str:
        """Return one validated secret by name from the store."""
        validate_secret_name(name, self.specs)
        loaded = self.load(require_complete=False)
        if name not in loaded:
            raise SecretError(
                f"Secret {name!r} is missing from {self.display_path}; "
                "run 'openstack-forge secrets init' first"
            )
        return loaded[name]

    def present_names(self) -> frozenset[str]:
        """Return the set of secret names currently present in the store (never values)."""
        self._reject_symlinks()
        if not self.path.exists():
            if self.path.parent.exists():
                self._check_directory()
            return frozenset()
        loaded = self.load(require_complete=False)
        return frozenset(loaded)

    def _reject_symlinks(self) -> None:
        root_dir = self.root.resolve(strict=True)
        rel_parts = self.path.relative_to(root_dir).parts
        current = root_dir
        for part in rel_parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise UnsafePathError(
                    f"Refusing to use secrets directory {self.display_dir}: {current} is a symlink"
                )
        if self.path.is_symlink():
            raise UnsafePathError(
                f"Refusing to use secrets file {self.display_path}: it is a symlink"
            )

    def _check_directory(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise UnsafePathError(
                f"Refusing to use secrets directory {self.display_dir}: it is a symlink"
            )
        dir_stat = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(dir_stat.st_mode):
            raise UnsafePathError(f"Secrets parent {self.display_dir} is not a directory")
        expected_uid = self._euid()
        if dir_stat.st_uid != expected_uid:
            raise SecretError(
                f"Secrets directory {self.display_dir} is owned by uid {dir_stat.st_uid}, "
                f"expected current effective uid {expected_uid}"
            )
        dir_mode = stat.S_IMODE(dir_stat.st_mode)
        if dir_mode & 0o077 != 0:
            raise SecretError(
                f"Secrets directory {self.display_dir} has mode {dir_mode:04o}, which is more "
                f"permissive than {SECRETS_DIR_MODE:04o} (group/other bits must be 0)"
            )

    def _check_file(self) -> os.stat_result:
        if self.path.is_symlink():
            raise UnsafePathError(
                f"Refusing to use secrets file {self.display_path}: it is a symlink"
            )
        file_stat = self.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode):
            raise UnsafePathError(
                f"Refusing to use secrets file {self.display_path}: it is not a regular file"
            )
        expected_uid = self._euid()
        if file_stat.st_uid != expected_uid:
            raise SecretError(
                f"Secrets file {self.display_path} is owned by uid {file_stat.st_uid}, "
                f"expected current effective uid {expected_uid}"
            )
        file_mode = stat.S_IMODE(file_stat.st_mode)
        if file_mode & 0o177 != 0:
            raise SecretError(
                f"Secrets file {self.display_path} has mode {file_mode:04o}, which is more "
                f"permissive than {SECRETS_FILE_MODE:04o} "
                "(execute, group and other bits must be 0)"
            )
        return file_stat

    def _read_and_validate(self, file_stat: os.stat_result | None = None) -> dict[str, str]:
        stat_key = (
            (file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)
            if file_stat is not None
            else None
        )
        if (
            stat_key is not None
            and stat_key == self._cache_stat_key
            and self._cached_secrets is not None
        ):
            return dict(self._cached_secrets)
        try:
            text = self.path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretError(f"Secrets file {self.display_path} is not valid UTF-8") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SecretError(
                f"Secrets file {self.display_path} is not valid JSON "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from None
        if not isinstance(raw, dict):
            raise SecretError(
                f"Secrets file {self.display_path} must contain a JSON object, "
                f"got {type(raw).__name__}"
            )
        validated: dict[str, str] = {}
        for key, value in raw.items():
            name = validate_secret_name(key, self.specs)
            validated[name] = validate_secret_value(name, value)
        if stat_key is not None:
            self._cache_stat_key = stat_key
            self._cached_secrets = dict(validated)
        return validated
