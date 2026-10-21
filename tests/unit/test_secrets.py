"""Tests for Milestones 13 & 14: secrets store, redaction, sensitive/stdin/env commands & canary."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.cli import main
from openstack_forge.execution import (
    DEFAULT_REGISTRY,
    DEFAULT_SECRETS_TARGET,
    ENVIRONMENT_SECURITY_URL,
    EXTRA_SECRET_NAMES,
    EXTRA_SECRET_SPECS,
    MASK,
    MAX_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
    PLACEMENT_INSTALL_URL,
    SANITIZED_ENV,
    SECRET_ALPHABET,
    SECRET_DESCRIPTIONS,
    SECRET_NAMES,
    SECRET_SOURCES,
    SECRETS_DIR_MODE,
    SECRETS_FILE_MODE,
    SECURITY_TABLE_SECRET_NAMES,
    AuditLog,
    CommandError,
    CommandExecutor,
    CommandRegistry,
    CommandSpec,
    CommandSpecError,
    Engine,
    FunctionStep,
    KeystoneStep,
    ParameterError,
    PlannedCommand,
    ProcessOutput,
    RabbitMQStep,
    RootEscapeError,
    SecretError,
    SecretSpec,
    SecretStore,
    StateStore,
    StepPreview,
    SubprocessBackend,
    UnsafePathError,
    VerifyResult,
    build_secret_specs,
    clear_registered_secrets,
    format_report,
    generate_secret,
    ini_set,
    managed_block,
    redact_data,
    redact_text,
    register_secret,
    register_secrets,
    registered_secret_count,
    validate_parameter,
    validate_secret_name,
    validate_secret_value,
)

TOOL = "/opt/forge-test/bin/secret-tool"
CANARY = "CanarySecretValue9876543210ZyXwVuTsRq"
CONTROLLER_NODE = {"hostname": "ctl-01", "role": "controller", "management_ip": "192.0.2.11"}


@pytest.fixture(autouse=True)
def _clean_redaction_registry() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# --- official password names, documented extras & extensibility -----------------------


def test_secret_names_match_environment_security_page_plus_documented_extras() -> None:
    expected_security_table = (
        "ADMIN_PASS",
        "CINDER_DBPASS",
        "CINDER_PASS",
        "DASH_DBPASS",
        "DEMO_PASS",
        "GLANCE_DBPASS",
        "GLANCE_PASS",
        "KEYSTONE_DBPASS",
        "METADATA_SECRET",
        "NEUTRON_DBPASS",
        "NEUTRON_PASS",
        "NOVA_DBPASS",
        "NOVA_PASS",
        "PLACEMENT_PASS",
        "RABBIT_PASS",
    )
    assert SECURITY_TABLE_SECRET_NAMES == expected_security_table
    assert len(SECURITY_TABLE_SECRET_NAMES) == 15

    # PLACEMENT_DBPASS is an explicit documented extra citing the Placement 2025.1 Ubuntu
    # installation guide (omitted from the environment-security.html summary table).
    assert EXTRA_SECRET_NAMES == ("PLACEMENT_DBPASS",)
    assert EXTRA_SECRET_SPECS == (
        SecretSpec(
            name="PLACEMENT_DBPASS",
            description="Database password for the Placement service",
            source_url=PLACEMENT_INSTALL_URL,
        ),
    )

    expected_all = (
        "ADMIN_PASS",
        "CINDER_DBPASS",
        "CINDER_PASS",
        "DASH_DBPASS",
        "DEMO_PASS",
        "GLANCE_DBPASS",
        "GLANCE_PASS",
        "KEYSTONE_DBPASS",
        "METADATA_SECRET",
        "NEUTRON_DBPASS",
        "NEUTRON_PASS",
        "NOVA_DBPASS",
        "NOVA_PASS",
        "PLACEMENT_DBPASS",
        "PLACEMENT_PASS",
        "RABBIT_PASS",
    )
    assert SECRET_NAMES == expected_all
    assert len(SECRET_NAMES) == 16
    assert tuple(SECRET_DESCRIPTIONS) == expected_all
    assert tuple(SECRET_SOURCES) == expected_all
    assert SECRET_SOURCES["PLACEMENT_DBPASS"] == PLACEMENT_INSTALL_URL
    for name in SECURITY_TABLE_SECRET_NAMES:
        assert SECRET_SOURCES[name] == ENVIRONMENT_SECURITY_URL


def test_secret_spec_extensibility_for_future_documented_secrets(tmp_path: Path) -> None:
    swift_spec = SecretSpec(
        name="SWIFT_PASS",
        description="Password of Object Storage service user swift",
        source_url="https://docs.openstack.org/swift/2025.1/install/controller-install-ubuntu.html",
    )
    extended_specs = build_secret_specs((*EXTRA_SECRET_SPECS, swift_spec))
    assert "SWIFT_PASS" in extended_specs
    assert len(extended_specs) == 17

    store = SecretStore(tmp_path, extra_specs=(swift_spec,))
    result = store.ensure()
    assert "SWIFT_PASS" in result.created
    assert len(result.created) == 17
    assert len(store.get("SWIFT_PASS")) == 32

    # Invalid SecretSpec definitions are rejected (never silent invention).
    with pytest.raises(SecretError, match="Invalid secret name"):
        SecretSpec("swift_pass", "desc", ENVIRONMENT_SECURITY_URL)
    with pytest.raises(SecretError, match="non-empty description"):
        SecretSpec("SWIFT_PASS", "   ", ENVIRONMENT_SECURITY_URL)
    with pytest.raises(SecretError, match="source_url must start with"):
        SecretSpec("SWIFT_PASS", "desc", "https://example.com/swift")
    with pytest.raises(SecretError, match="Duplicate secret name 'ADMIN_PASS'"):
        build_secret_specs((SecretSpec("ADMIN_PASS", "dup", ENVIRONMENT_SECURITY_URL),))
    with pytest.raises(SecretError, match="Duplicate secret name 'PLACEMENT_DBPASS'"):
        build_secret_specs((*EXTRA_SECRET_SPECS, EXTRA_SECRET_SPECS[0]))
    with pytest.raises(SecretError, match="must be a SecretSpec"):
        build_secret_specs(("SWIFT_PASS",))  # type: ignore[arg-type]


def test_generate_secret_charset_and_length() -> None:
    samples = {generate_secret() for _ in range(32)}
    assert len(samples) == 32
    for sample in samples:
        assert len(sample) == MIN_SECRET_LENGTH == 32
        assert all(ch in SECRET_ALPHABET for ch in sample)
        assert sample.isalnum() and sample.isascii()
        assert validate_secret_value("ADMIN_PASS", sample) == sample
        assert validate_parameter("secret", "password", sample) == sample

    long_sample = generate_secret(64)
    assert len(long_sample) == 64 and long_sample.isalnum()


@pytest.mark.parametrize("bad_length", [0, 1, 16, 31, 257, -5])
def test_generate_secret_rejects_invalid_length(bad_length: int) -> None:
    with pytest.raises(ValueError, match="Secret length must be between"):
        generate_secret(bad_length)


def test_validate_secret_name_and_value_never_echo_rejected_secret() -> None:
    with pytest.raises(SecretError, match="Unknown secret name 'NOT_A_SECRET'"):
        validate_secret_name("NOT_A_SECRET")

    short_secret = "ShortSecret12345"
    with pytest.raises(SecretError, match="too short") as excinfo:
        validate_secret_value("ADMIN_PASS", short_secret)
    assert short_secret not in str(excinfo.value)

    bad_chars_secret = "BadCharsSecretWithDash-AndAtSign@123456"
    with pytest.raises(SecretError, match=r"outside \[A-Za-z0-9\]") as excinfo:
        validate_secret_value("ADMIN_PASS", bad_chars_secret)
    assert bad_chars_secret not in str(excinfo.value)

    too_long = "A" * (MAX_SECRET_LENGTH + 1)
    with pytest.raises(SecretError, match="too long") as excinfo:
        validate_secret_value("ADMIN_PASS", too_long)
    assert too_long not in str(excinfo.value)

    with pytest.raises(SecretError, match="must be a string"):
        validate_secret_value("ADMIN_PASS", 12345)


# --- SecretStore: permissions, ownership, symlinks, idempotence, no .orig/temp --------


def test_secret_store_ensure_creates_0700_dir_and_0600_file_and_is_idempotent(
    tmp_path: Path,
) -> None:
    calls = 0

    def counting_generator() -> str:
        nonlocal calls
        calls += 1
        return f"GeneratedSecretNumber{calls:012d}AbCdEf"

    store = SecretStore(tmp_path, generator=counting_generator)
    first = store.ensure()
    assert first.path == DEFAULT_SECRETS_TARGET
    assert first.created == SECRET_NAMES
    assert first.kept == ()
    assert first.wrote_file is True
    assert calls == 16

    dir_mode = stat.S_IMODE(store.path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(store.path.stat().st_mode)
    assert dir_mode == SECRETS_DIR_MODE == 0o700
    assert file_mode == SECRETS_FILE_MODE == 0o600

    first_stat = store.path.stat()
    first_loaded = store.load()
    assert tuple(first_loaded) == SECRET_NAMES

    # Second run writes nothing and keeps every existing value untouched.
    second = store.ensure()
    assert second.created == ()
    assert second.kept == SECRET_NAMES
    assert second.wrote_file is False
    assert calls == 16
    second_stat = store.path.stat()
    assert second_stat.st_ino == first_stat.st_ino
    assert store.load() == first_loaded


def test_secret_store_never_creates_orig_backup_or_leaves_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)
    secrets_dir = store.path.parent

    # 1. Initial creation
    store.ensure()
    assert [p.name for p in secrets_dir.iterdir()] == ["secrets.json"]
    assert not (secrets_dir / "secrets.json.orig").exists()
    assert list(secrets_dir.glob(".secrets.json.*")) == []

    # 2. Updating a partial store (which rewrites secrets.json) still creates no .orig backup!
    partial = {"ADMIN_PASS": store.get("ADMIN_PASS")}
    store.path.write_text(json.dumps(partial), encoding="utf-8")
    os.chmod(store.path, 0o600)
    res = store.ensure()
    assert res.wrote_file is True
    assert [p.name for p in secrets_dir.iterdir()] == ["secrets.json"]
    assert not (secrets_dir / "secrets.json.orig").exists()
    assert list(secrets_dir.glob(".secrets.json.*")) == []

    # 3. Simulated failure during atomic_write_text (os.replace raising OSError) cleans up temp file
    partial2 = {"ADMIN_PASS": store.get("ADMIN_PASS")}
    store.path.write_text(json.dumps(partial2), encoding="utf-8")
    os.chmod(store.path, 0o600)

    def failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.ensure()
    assert [p.name for p in secrets_dir.iterdir()] == ["secrets.json"]
    assert not (secrets_dir / "secrets.json.orig").exists()
    assert list(secrets_dir.glob(".secrets.json.*")) == []


def test_secret_store_completes_partial_file_without_overwriting_existing_entries(
    tmp_path: Path,
) -> None:
    store = SecretStore(tmp_path)
    secrets_dir = tmp_path / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    custom_admin = "UserProvidedAdminPassword1234567890AbCd"
    custom_rabbit = "UserProvidedRabbitPassword1234567890XyZ"
    secrets_file = secrets_dir / "secrets.json"
    secrets_file.write_text(
        json.dumps({"ADMIN_PASS": custom_admin, "RABBIT_PASS": custom_rabbit}),
        encoding="utf-8",
    )
    os.chmod(secrets_file, 0o600)

    # load(require_complete=True) fails when 14 secrets are still missing.
    with pytest.raises(SecretError, match="missing required secret"):
        store.load()

    # get() works for an already-present secret even before completion.
    assert store.get("ADMIN_PASS") == custom_admin
    with pytest.raises(SecretError, match="missing from /etc/openstack-forge/secrets.json"):
        store.get("KEYSTONE_DBPASS")

    # ensure() keeps the 2 user-supplied values and generates only the 14 missing ones.
    result = store.ensure()
    assert result.kept == ("ADMIN_PASS", "RABBIT_PASS")
    assert len(result.created) == 14
    assert "ADMIN_PASS" not in result.created and "RABBIT_PASS" not in result.created
    assert result.wrote_file is True

    loaded = store.load()
    assert loaded["ADMIN_PASS"] == custom_admin
    assert loaded["RABBIT_PASS"] == custom_rabbit
    assert len(loaded) == 16


@pytest.mark.parametrize("bad_file_mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o700])
def test_secret_store_refuses_overly_permissive_file_mode(
    tmp_path: Path, bad_file_mode: int
) -> None:
    store = SecretStore(tmp_path)
    store.ensure()
    os.chmod(store.path, bad_file_mode)

    with pytest.raises(SecretError, match="more permissive than 0600"):
        store.load()
    with pytest.raises(SecretError, match="more permissive than 0600"):
        store.ensure()
    with pytest.raises(SecretError, match="more permissive than 0600"):
        store.get("ADMIN_PASS")


@pytest.mark.parametrize("bad_dir_mode", [0o755, 0o750, 0o705, 0o770, 0o777])
def test_secret_store_refuses_overly_permissive_directory_mode(
    tmp_path: Path, bad_dir_mode: int
) -> None:
    store = SecretStore(tmp_path)
    store.ensure()
    os.chmod(store.path.parent, bad_dir_mode)
    try:
        with pytest.raises(SecretError, match="more permissive than 0700"):
            store.load()
        with pytest.raises(SecretError, match="more permissive than 0700"):
            store.ensure()
    finally:
        os.chmod(store.path.parent, 0o700)


def test_secret_store_refuses_wrong_owner_on_file_or_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)
    store.ensure()

    # 1. Directory owner mismatch
    wrong_user_store = SecretStore(tmp_path, euid=lambda: os.geteuid() + 1)
    with pytest.raises(SecretError, match="Secrets directory .* owned by uid"):
        wrong_user_store.load()
    with pytest.raises(SecretError, match="Secrets directory .* owned by uid"):
        wrong_user_store.ensure()

    # 2. File-only owner mismatch (directory owner matches euid, file owner does not)
    real_stat = Path.stat

    def fake_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        st = real_stat(self, follow_symlinks=follow_symlinks)
        if self == store.path:
            fields = list(st)
            fields[stat.ST_UID] = os.geteuid() + 99
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(SecretError, match="Secrets file .* owned by uid"):
        store.load()
    with pytest.raises(SecretError, match="Secrets file .* owned by uid"):
        store.ensure()


def test_secret_store_refuses_symlinks_on_file_and_directories(tmp_path: Path) -> None:
    # 1. File is a symlink
    secrets_dir = tmp_path / "etc" / "openstack-forge"
    secrets_dir.mkdir(parents=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    os.chmod(real_file, 0o600)
    (secrets_dir / "secrets.json").symlink_to(real_file)

    store = SecretStore(tmp_path)
    with pytest.raises(UnsafePathError, match="symlink"):
        store.ensure()
    with pytest.raises(UnsafePathError, match="symlink"):
        store.load()

    # 2. Parent directory is a symlink
    (secrets_dir / "secrets.json").unlink()
    secrets_dir.rmdir()
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir(mode=0o700)
    os.chmod(real_dir, 0o700)
    secrets_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(UnsafePathError, match="symlink"):
        store.ensure()
    with pytest.raises(UnsafePathError, match="symlink"):
        store.load()


def test_secret_store_rejects_root_escape_and_invalid_json_or_values(tmp_path: Path) -> None:
    with pytest.raises(RootEscapeError):
        SecretStore(tmp_path, "../outside.json")

    store = SecretStore(tmp_path)
    store.ensure()

    # Invalid JSON does not echo file content
    leaked_fragment = "LeakedBrokenJsonSecret1234567890AbCdEf"
    store.path.write_text(f'{{"ADMIN_PASS": "{leaked_fragment}"', encoding="utf-8")
    with pytest.raises(SecretError, match="not valid JSON") as excinfo:
        store.load()
    assert leaked_fragment not in str(excinfo.value)

    # Non-object JSON
    store.path.write_text('["not", "an", "object"]\n', encoding="utf-8")
    with pytest.raises(SecretError, match="JSON object"):
        store.load()

    # Unknown secret key
    store.path.write_text(json.dumps({"UNKNOWN_PASS": "A" * 32}), encoding="utf-8")
    with pytest.raises(SecretError, match="Unknown secret name 'UNKNOWN_PASS'"):
        store.load()

    # Weak user-supplied value is rejected without echoing the value
    weak = "TooShortUserPassword12345"
    store.path.write_text(json.dumps({"ADMIN_PASS": weak}), encoding="utf-8")
    with pytest.raises(SecretError, match="too short") as excinfo:
        store.load(require_complete=False)
    assert weak not in str(excinfo.value)


# --- exact-value redaction ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_secret",
    ["", "short", "1234567", "has space in it", "has\nnewline", "has\ttab", 123],
)
def test_register_secret_rejects_short_empty_whitespace_or_non_string(
    bad_secret: object,
) -> None:
    with pytest.raises(ValueError):
        register_secret(bad_secret)  # type: ignore[arg-type]
    assert registered_secret_count() == 0


def test_exact_value_redaction_masks_bare_occurrences_everywhere() -> None:
    secret_a = "AlphaSecretValue1234567890AbCdEfGh"
    secret_b = "AlphaSecretValue1234567890AbCdEfGhExtended99"
    register_secrets([secret_a, secret_b])
    assert registered_secret_count() == 2

    # Longest secret is replaced first so no suffix of secret_b leaks.
    text = f"bare {secret_a} and longer {secret_b} in a message without key"
    redacted = redact_text(text)
    assert secret_a not in redacted
    assert secret_b not in redacted
    assert redacted == f"bare {MASK} and longer {MASK} in a message without key"

    nested = {"normal_key": f"failed with {secret_a}", secret_a: "value"}
    cleaned = redact_data(nested)
    assert secret_a not in json.dumps(cleaned)


# --- CommandSpec sensitive_params, stdin_param, and env_params ------------------------


class StdinRecordingBackend:
    """Fake backend that records argv, env, and stdin without spawning any process."""

    def __init__(self, output: ProcessOutput | Exception | None = None) -> None:
        self.output = output or ProcessOutput(0, "", "")
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.envs: list[dict[str, str]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout: float,
        stdin: str | None = None,
    ) -> ProcessOutput:
        self.calls.append((argv, stdin))
        self.envs.append(dict(env))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def test_command_spec_validation_for_sensitive_params_stdin_param_and_env_params() -> None:
    with pytest.raises(CommandSpecError, match="sensitive_params .* are not in params"):
        CommandSpec(
            "test.bad-sensitive",
            (TOOL, "run"),
            sensitive_params=frozenset({"missing"}),
        )

    with pytest.raises(CommandSpecError, match="stdin_param 'pw' is not in params"):
        CommandSpec(
            "test.bad-stdin-missing",
            (TOOL, "run"),
            stdin_param="pw",
        )

    with pytest.raises(CommandSpecError, match="stdin_param 'unit' must be sensitive"):
        CommandSpec(
            "test.bad-stdin-not-sensitive",
            (TOOL, "run"),
            {"unit": "systemd_unit"},
            stdin_param="unit",
        )

    with pytest.raises(CommandSpecError, match="stdin_param 'pw' must not appear in argv"):
        CommandSpec(
            "test.bad-stdin-in-argv",
            (TOOL, "run", "{pw}"),
            {"pw": "secret"},
            stdin_param="pw",
        )

    # env_params validation checks
    with pytest.raises(CommandSpecError, match="env_params parameter 'pw' is not in params"):
        CommandSpec(
            "test.bad-env-missing",
            (TOOL, "run"),
            env_params={"pw": "FORGE_PW"},
        )

    with pytest.raises(CommandSpecError, match="env_params parameter 'unit' must be sensitive"):
        CommandSpec(
            "test.bad-env-not-sensitive",
            (TOOL, "run"),
            {"unit": "systemd_unit"},
            env_params={"unit": "FORGE_UNIT"},
        )

    with pytest.raises(CommandSpecError, match="cannot be in both stdin_param and env_params"):
        CommandSpec(
            "test.bad-env-and-stdin",
            (TOOL, "run"),
            {"pw": "secret"},
            stdin_param="pw",
            env_params={"pw": "FORGE_PW"},
        )

    with pytest.raises(CommandSpecError, match="env_params parameter 'pw' must not appear in argv"):
        CommandSpec(
            "test.bad-env-in-argv",
            (TOOL, "run", "{pw}"),
            {"pw": "secret"},
            env_params={"pw": "FORGE_PW"},
        )

    @pytest.mark.parametrize("bad_env_name", ["", "lowercase", "1BAD", "BAD-NAME", "BAD=NAME"])
    def _check_bad_env_name(name: str) -> None:
        with pytest.raises(CommandSpecError, match="invalid env_params variable name"):
            CommandSpec(
                "test.bad-env-name",
                (TOOL, "run"),
                {"pw": "secret"},
                env_params={"pw": name},
            )

    for bad_name in ("", "lowercase", "1BAD", "BAD-NAME", "BAD=NAME"):
        _check_bad_env_name(bad_name)

    for reserved in SANITIZED_ENV:
        with pytest.raises(CommandSpecError, match="collides with SANITIZED_ENV"):
            CommandSpec(
                "test.bad-env-collision",
                (TOOL, "run"),
                {"pw": "secret"},
                env_params={"pw": reserved},
            )

    with pytest.raises(CommandSpecError, match="duplicate env_params variable 'FORGE_PW'"):
        CommandSpec(
            "test.bad-env-dup",
            (TOOL, "run"),
            {"pw1": "secret", "pw2": "secret"},
            env_params={"pw1": "FORGE_PW", "pw2": "FORGE_PW"},
        )


def test_sensitive_argv_parameter_goes_to_backend_but_is_masked_everywhere_else(
    tmp_path: Path,
) -> None:
    spec = CommandSpec(
        "test.add-user",
        (TOOL, "add-user", "{user}", "{password}"),
        {"user": "hostname", "password": "secret"},
        mutates=False,
    )
    registry = CommandRegistry([spec])
    secret_val = "ArgvSecretPassword1234567890AbCdEfGh"

    # PlannedCommand and build_shown_argv mask the secret even before register_secret().
    planned = PlannedCommand("test.add-user", {"user": "openstack", "password": secret_val})
    assert planned.argv(registry) == (TOOL, "add-user", "openstack", MASK)
    assert planned.describe(registry) == f"{TOOL} add-user openstack '{MASK}'"
    assert secret_val not in planned.describe(registry)
    assert secret_val not in repr(planned)

    # ParameterError on an invalid sensitive value never echoes the rejected secret.
    bad_secret = "InvalidSecretWithSemicolon;1234567890"
    with pytest.raises(ParameterError) as excinfo:
        spec.build_argv({"user": "openstack", "password": bad_secret})
    assert bad_secret not in str(excinfo.value)
    assert MASK in str(excinfo.value)

    # Dry-run and real run mask argv in output, CommandResult, and audit log.
    audit = AuditLog(tmp_path / "commands.log")
    backend = StdinRecordingBackend()
    echoed: list[str] = []
    dry_exec = CommandExecutor(
        registry,
        root=tmp_path,
        audit_log=audit,
        backend=backend,
        dry_run=True,
        echo=echoed.append,
    )
    dry_res = dry_exec.run("test.add-user", {"user": "openstack", "password": secret_val})
    assert dry_res.argv == (TOOL, "add-user", "openstack", MASK)
    assert secret_val not in "\n".join(echoed)

    real_exec = CommandExecutor(registry, root=tmp_path, audit_log=audit, backend=backend)
    res = real_exec.run("test.add-user", {"user": "openstack", "password": secret_val})
    assert backend.calls == [((TOOL, "add-user", "openstack", secret_val), None)]
    assert res.argv == (TOOL, "add-user", "openstack", MASK)
    assert secret_val not in audit.path.read_text(encoding="utf-8")


def test_stdin_param_delivers_secret_on_stdin_and_keeps_argv_clean(tmp_path: Path) -> None:
    spec = CommandSpec(
        "test.stdin-auth",
        (TOOL, "change-password", "{user}"),
        {"user": "hostname", "password": "secret"},
        stdin_param="password",
        mutates=False,
    )
    registry = CommandRegistry([spec])
    secret_val = "StdinSecretPassword1234567890AbCdEfGh"

    audit = AuditLog(tmp_path / "commands.log")
    backend = StdinRecordingBackend()
    executor = CommandExecutor(registry, root=tmp_path, audit_log=audit, backend=backend)
    result = executor.run("test.stdin-auth", {"user": "openstack", "password": secret_val})

    assert backend.calls == [((TOOL, "change-password", "openstack"), secret_val)]
    assert result.argv == (TOOL, "change-password", "openstack")
    assert secret_val not in audit.path.read_text(encoding="utf-8")


def test_env_params_delivers_secret_in_environment_and_masks_everywhere(tmp_path: Path) -> None:
    spec = CommandSpec(
        "test.env-auth",
        (TOOL, "connect", "{user}"),
        {"user": "hostname", "password": "secret"},
        env_params={"password": "FORGE_TEST_PASS"},
        mutates=False,
    )
    registry = CommandRegistry([spec])
    secret_val = "EnvSecretPassword1234567890AbCdEfGhIj"

    # build_env includes SANITIZED_ENV + FORGE_TEST_PASS; build_shown_env masks it.
    built_env = spec.build_env({"user": "openstack", "password": secret_val})
    assert built_env == {**SANITIZED_ENV, "FORGE_TEST_PASS": secret_val}
    shown_env = spec.build_shown_env({"user": "openstack", "password": secret_val})
    assert shown_env == {**SANITIZED_ENV, "FORGE_TEST_PASS": MASK}

    # PlannedCommand and build_shown_argv never expose the secret
    planned = PlannedCommand("test.env-auth", {"user": "openstack", "password": secret_val})
    assert planned.argv(registry) == (TOOL, "connect", "openstack")
    assert secret_val not in planned.describe(registry)
    assert secret_val not in repr(planned)

    # ParameterError on invalid env_params value masks the rejected secret
    bad_secret = "BadEnvSecretWithBang!1234567890AbCd"
    with pytest.raises(ParameterError) as excinfo:
        spec.build_env({"user": "openstack", "password": bad_secret})
    assert bad_secret not in str(excinfo.value)
    assert MASK in str(excinfo.value)

    # Dry-run, normal execution, and failing command output mask the secret everywhere
    audit = AuditLog(tmp_path / "commands.log")
    backend = StdinRecordingBackend()
    echoed: list[str] = []
    dry_exec = CommandExecutor(
        registry,
        root=tmp_path,
        audit_log=audit,
        backend=backend,
        dry_run=True,
        echo=echoed.append,
    )
    dry_res = dry_exec.run("test.env-auth", {"user": "openstack", "password": secret_val})
    assert dry_res.argv == (TOOL, "connect", "openstack")
    assert secret_val not in "\n".join(echoed)

    real_exec = CommandExecutor(registry, root=tmp_path, audit_log=audit, backend=backend)
    res = real_exec.run("test.env-auth", {"user": "openstack", "password": secret_val})
    assert backend.calls == [((TOOL, "connect", "openstack"), None)]
    assert backend.envs == [{**SANITIZED_ENV, "FORGE_TEST_PASS": secret_val}]
    assert res.argv == (TOOL, "connect", "openstack")
    assert secret_val not in audit.path.read_text(encoding="utf-8")

    # CommandError / UnexpectedExitError redacts secret_val even if register_secret wasn't called
    fail_backend = StdinRecordingBackend(ProcessOutput(1, "", f"env auth failed: {secret_val}"))
    fail_exec = CommandExecutor(registry, root=tmp_path, audit_log=audit, backend=fail_backend)
    with pytest.raises(CommandError) as err_info:
        fail_exec.run("test.env-auth", {"user": "openstack", "password": secret_val})
    assert secret_val not in str(err_info.value)
    assert MASK in str(err_info.value)


def test_subprocess_backend_stdin_and_env_delivery() -> None:
    """REAL (harmless sys.executable one-liner): verify stdin pipe vs DEVNULL and env delivery."""
    backend = SubprocessBackend()
    code = "import sys; data = sys.stdin.read(); print(f'len={len(data)}:{data == sys.argv[1]}')"
    secret_val = "RealPipeStdinSecret1234567890AbCdEf"

    # Default: stdin is DEVNULL, so sys.stdin.read() is empty.
    out_default = backend.run(
        (sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"),
        env={"PATH": "/usr/bin:/bin"},
        timeout=10.0,
    )
    assert out_default.returncode == 0
    assert out_default.stdout.strip() == "''"

    # Opt-in stdin: process receives the exact string on stdin.
    out_stdin = backend.run(
        (sys.executable, "-c", code, secret_val),
        env={"PATH": "/usr/bin:/bin"},
        timeout=10.0,
        stdin=secret_val,
    )
    assert out_stdin.returncode == 0
    assert out_stdin.stdout.strip() == f"len={len(secret_val)}:True"

    # Opt-in env: process receives the exact string in os.environ['FORGE_ENV_SECRET'].
    env_code = "import os; val = os.environ.get('FORGE_ENV_SECRET', ''); print(len(val))"
    out_env = backend.run(
        (sys.executable, "-c", env_code),
        env={"PATH": "/usr/bin:/bin", "FORGE_ENV_SECRET": secret_val},
        timeout=10.0,
    )
    assert out_env.returncode == 0
    assert out_env.stdout.strip() == str(len(secret_val))


# --- CLI: openstack-forge secrets init & show -----------------------------------------


def test_cli_secrets_init_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["secrets", "init", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Secrets store: /etc/openstack-forge/secrets.json" in captured.out
    assert "Created (16):" in captured.out

    # None of the 16 generated secret values may appear in `secrets init` output!
    store = SecretStore(tmp_path)
    loaded = store.load()
    for name, value in loaded.items():
        assert name in captured.out
        assert value not in captured.out

    # Second run is idempotent and reports Created (0): none and Kept existing (16).
    rc2 = main(["secrets", "init", "--root", str(tmp_path)])
    captured2 = capsys.readouterr()
    assert rc2 == 0
    assert "Created (0): none" in captured2.out
    assert "Kept existing (16):" in captured2.out
    for value in loaded.values():
        assert value not in captured2.out

    # `secrets show NAME` prints ONLY the single value on stdout.
    rc_show = main(["secrets", "show", "RABBIT_PASS", "--root", str(tmp_path)])
    captured_show = capsys.readouterr()
    assert rc_show == 0
    assert captured_show.err == ""
    assert captured_show.out == f"{loaded['RABBIT_PASS']}\n"


def test_cli_secrets_custom_file_and_error_handling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing store before init
    rc_missing = main(["secrets", "show", "ADMIN_PASS", "--root", str(tmp_path)])
    assert rc_missing == 1
    assert "does not exist" in capsys.readouterr().err

    # Custom --file path
    custom_target = "/etc/custom-forge/my-secrets.json"
    assert main(["secrets", "init", "--root", str(tmp_path), "--file", custom_target]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "secrets",
                "show",
                "ADMIN_PASS",
                "--root",
                str(tmp_path),
                "--file",
                custom_target,
            ]
        )
        == 0
    )
    shown = capsys.readouterr().out.strip()
    assert len(shown) == 32 and shown.isalnum()

    # Unknown secret name
    assert main(["secrets", "show", "NO_SUCH_SECRET", "--root", str(tmp_path)]) == 1
    assert "Unknown secret name" in capsys.readouterr().err

    # Writing to '/' as non-root is refused
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert main(["secrets", "init"]) == 1
    assert "requires root privileges" in capsys.readouterr().err


# --- Canary end-to-end non-leakage test (including RabbitMQStep) ----------------------


def test_canary_secret_never_leaks_outside_the_0600_secrets_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canary test: CANARY must never appear in diffs, logs, state, audit, reports, or dry-run."""
    root = tmp_path / "root"
    (root / "etc" / "openstack-forge").mkdir(parents=True, mode=0o700)
    os.chmod(root / "etc" / "openstack-forge", 0o700)
    (root / "var" / "lib" / "openstack-forge").mkdir(parents=True)
    (root / "var" / "log" / "openstack-forge").mkdir(parents=True, mode=0o700)

    # Pre-seed RABBIT_PASS, KEYSTONE_DBPASS, and ADMIN_PASS with CANARY and generate the other
    # 13 secrets via CLI `secrets init`.
    secrets_path = root / "etc" / "openstack-forge" / "secrets.json"
    secrets_path.write_text(
        json.dumps(
            {
                "RABBIT_PASS": CANARY,
                "KEYSTONE_DBPASS": CANARY,
                "ADMIN_PASS": CANARY,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(secrets_path, 0o600)

    assert main(["secrets", "init", "--root", str(root)]) == 0
    init_out, init_err = capsys.readouterr()
    assert CANARY not in init_out and CANARY not in init_err

    store = SecretStore(root)
    secrets_map = store.load()
    assert secrets_map["RABBIT_PASS"] == CANARY
    assert secrets_map["KEYSTONE_DBPASS"] == CANARY
    assert secrets_map["ADMIN_PASS"] == CANARY

    # 1. File edits (ini_set and managed_block) using CANARY under non-password keys too!
    (root / "etc" / "app.conf").write_text("[DEFAULT]\nauth_line = old\n", encoding="utf-8")
    ini_change = ini_set(root, "/etc/app.conf", "DEFAULT", "custom_field", CANARY, dry_run=False)
    block_change = managed_block(
        root,
        "/etc/app.conf",
        "forge-canary",
        f"bare_credential_line {CANARY}\n",
        dry_run=False,
    )
    assert CANARY not in ini_change.diff
    assert CANARY not in block_change.diff
    assert MASK in ini_change.diff
    assert MASK in block_change.diff

    # 2. Dry-run preview and PlannedCommand with CANARY (argv, stdin, env)
    spec_argv = CommandSpec(
        "test.canary-argv",
        (TOOL, "set-pass", "{password}"),
        {"password": "secret"},
        mutates=False,
    )
    spec_stdin = CommandSpec(
        "test.canary-stdin",
        (TOOL, "set-stdin", "{user}"),
        {"user": "hostname", "password": "secret"},
        stdin_param="password",
        mutates=False,
    )
    spec_env = CommandSpec(
        "test.canary-env",
        (TOOL, "set-env", "{user}"),
        {"user": "hostname", "password": "secret"},
        env_params={"password": "FORGE_CANARY_PASS"},
        mutates=False,
    )
    registry = CommandRegistry([spec_argv, spec_stdin, spec_env])
    preview = StepPreview(
        file_changes=(ini_change, block_change),
        commands=(
            PlannedCommand("test.canary-argv", {"password": CANARY}),
            PlannedCommand("test.canary-stdin", {"user": "openstack", "password": CANARY}),
            PlannedCommand("test.canary-env", {"user": "openstack", "password": CANARY}),
        ),
        notes=(f"Would configure broker with {CANARY}",),
    )
    for cmd in preview.commands:
        assert CANARY not in cmd.describe(registry)
        assert CANARY not in " ".join(cmd.argv(registry))
        assert CANARY not in repr(cmd)

    # 3. CommandExecutor dry-run, failing command output, and backend exception with CANARY
    audit = AuditLog(root / "var" / "log" / "openstack-forge" / "commands.log")
    dry_lines: list[str] = []
    dry_executor = CommandExecutor(
        registry,
        root=root,
        audit_log=audit,
        backend=StdinRecordingBackend(),
        dry_run=True,
        echo=dry_lines.append,
    )
    dry_executor.run("test.canary-argv", {"password": CANARY})
    assert CANARY not in "\n".join(dry_lines)

    failing_backend = StdinRecordingBackend(
        ProcessOutput(1, f"stdout leaked {CANARY}\n", f"stderr leaked {CANARY}\n")
    )
    executor = CommandExecutor(registry, root=root, audit_log=audit, backend=failing_backend)

    # 4. Engine steps that fail in apply() and verify() while echoing CANARY
    state_store = StateStore(root / "var" / "lib" / "openstack-forge" / "state.json")
    engine = Engine(state_store)

    step_cmd_fail = FunctionStep(
        id="canary-cmd-fail",
        check_fn=lambda: False,
        apply_fn=lambda: executor.run(
            "test.canary-stdin", {"user": "openstack", "password": CANARY}
        ),
        verify_fn=lambda: VerifyResult(True),
    )
    report1 = engine.run([step_cmd_fail])
    assert not report1.ok
    report1_text = format_report(report1)

    step_verify_fail = FunctionStep(
        id="canary-verify-fail",
        check_fn=lambda: False,
        apply_fn=lambda: None,
        verify_fn=lambda: VerifyResult(False, f"broker rejected {CANARY} during verify"),
    )
    report2 = engine.run([step_verify_fail])
    assert not report2.ok
    report2_text = format_report(report2)

    # 5. Real RabbitMQStep & KeystoneStep with CANARY (dry_run_preview, Engine run, and report)
    monkeypatch.setattr("openstack_forge.execution.commands.is_host_root", lambda _: True)
    host = ServiceHost(
        root,
        starts={"rabbitmq-server"},
        creates={
            "keystone": (
                (
                    "etc/keystone/keystone.conf",
                    "[database]\nconnection = sqlite:////var/lib/keystone/keystone.db\n[token]\n",
                ),
                ("etc/apache2/apache2.conf", "# Apache2 default\n"),
            )
        },
    )
    service_exec = CommandExecutor(
        DEFAULT_REGISTRY,
        root=root,
        audit_log=audit,
        backend=host,
        euid=lambda: 0,
    )
    rabbit_step = RabbitMQStep(
        root=root, config={}, local_node=CONTROLLER_NODE, executor=service_exec
    )
    rabbit_preview = rabbit_step.dry_run_preview()
    for cmd in (*rabbit_preview.commands, *rabbit_preview.read_only_commands):
        assert CANARY not in cmd.describe(DEFAULT_REGISTRY)
        assert CANARY not in " ".join(cmd.argv(DEFAULT_REGISTRY))
        assert CANARY not in repr(cmd)
    for note in rabbit_preview.notes:
        assert CANARY not in note

    rabbit_report = engine.run([rabbit_step])
    assert rabbit_report.ok
    rabbit_report_text = format_report(rabbit_report)
    assert host.rabbit_passwords == {"openstack": CANARY}

    keystone_step = KeystoneStep(
        root=root, config={}, local_node=CONTROLLER_NODE, executor=service_exec
    )
    keystone_preview_before = keystone_step.dry_run_preview()
    for cmd in (*keystone_preview_before.commands, *keystone_preview_before.read_only_commands):
        assert CANARY not in cmd.describe(DEFAULT_REGISTRY)
        assert CANARY not in " ".join(cmd.argv(DEFAULT_REGISTRY))
        assert CANARY not in repr(cmd)
    for note in keystone_preview_before.notes:
        assert CANARY not in note

    keystone_calls_start = len(host.calls)
    keystone_report = engine.run([keystone_step])
    assert keystone_report.ok
    keystone_report_text = format_report(keystone_report)
    assert host.mariadb_passwords == {
        ("keystone", "localhost"): CANARY,
        ("keystone", "%"): CANARY,
    }
    assert host.keystone_bootstrapped is not None
    assert host.keystone_bootstrapped["password"] == CANARY
    assert any(p["Name"] == "service" for p in host.openstack_projects)
    # Neither KEYSTONE_DBPASS (stdin_param) nor ADMIN_PASS (env_params) ever
    # appears in argv during the keystone step.
    for argv in host.calls[keystone_calls_start:]:
        assert all(CANARY not in arg for arg in argv)

    # Backend raising a raw RuntimeError containing CANARY
    raising_executor = CommandExecutor(
        registry,
        root=root,
        audit_log=audit,
        backend=StdinRecordingBackend(RuntimeError(f"low-level error with {CANARY}")),
    )
    with pytest.raises(CommandError) as excinfo:
        raising_executor.run("test.canary-argv", {"password": CANARY})
    assert CANARY not in str(excinfo.value)

    # 6. Assert CANARY never appears in state.json, commands.log, reports, diffs, or logs.
    state_text = state_store.path.read_text(encoding="utf-8")
    audit_text = audit.path.read_text(encoding="utf-8")
    for surface_name, text in (
        ("state.json", state_text),
        ("commands.log", audit_text),
        ("report1", report1_text),
        ("report2", report2_text),
        ("rabbit_report", rabbit_report_text),
        ("keystone_report", keystone_report_text),
        ("ini_diff", ini_change.diff),
        ("block_diff", block_change.diff),
        ("caplog", caplog.text),
    ):
        assert CANARY not in text, f"CANARY leaked into {surface_name}"

    # Walk all files under tmp_path: only secrets.json, admin-openrc, keystone.conf,
    # and app.conf contain CANARY.
    files_with_canary = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and CANARY in path.read_text(encoding="utf-8")
    )
    assert files_with_canary == [
        "etc/app.conf",
        "etc/keystone/keystone.conf",
        "etc/openstack-forge/admin-openrc",
        "etc/openstack-forge/secrets.json",
    ]
    admin_openrc_path = root / "etc/openstack-forge/admin-openrc"
    assert stat.S_IMODE(admin_openrc_path.stat().st_mode) == 0o600
    assert not (root / "etc/openstack-forge/admin-openrc.orig").exists()
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
