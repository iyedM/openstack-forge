"""Tests for Milestone 15 Part 2: MariaDB SQL provisioning brick and ``sql_stdin``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from _service_fakes import ServiceHost

from openstack_forge.execution import (
    DEFAULT_REGISTRY,
    MARIADB_EXECUTE_SQL,
    MARIADB_QUERY_SQL,
    MASK,
    MAX_SQL_STDIN_LENGTH,
    AuditLog,
    CommandExecutor,
    CommandSpec,
    CommandSpecError,
    ParameterError,
    PlannedCommand,
    ProcessOutput,
    SecretError,
    UnexpectedExitError,
    build_service_database_sql,
    clear_registered_secrets,
    parse_mariadb_state,
    plan_service_database_sql,
    provision_service_database,
    redact_text,
    service_database_problems,
    service_database_ready,
    validate_parameter,
    validate_sql_identifier,
    verify_service_database_state,
)

TEST_DB_PASS = "KeystoneDbPassAlphaNumeric32Char"


@pytest.fixture(autouse=True)
def _clean_redaction_registry() -> Iterator[None]:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _executor(tmp_path: Path, host: ServiceHost) -> CommandExecutor:
    (tmp_path / "audit").mkdir(exist_ok=True)
    return CommandExecutor(
        DEFAULT_REGISTRY,
        root=tmp_path / "root",
        audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=host,
        echo=lambda _: None,
        euid=lambda: 0,
    )


# --- sql_stdin parameter type and CommandSpec enforcement -----------------------------


def test_sql_stdin_is_only_allowed_as_stdin_param() -> None:
    with pytest.raises(CommandSpecError, match="only allowed as stdin_param"):
        CommandSpec(
            "test.sql-in-argv",
            ("/usr/bin/mariadb", "-e", "{sql}"),
            {"sql": "sql_stdin"},
        )
    with pytest.raises(CommandSpecError, match="only allowed as stdin_param"):
        CommandSpec(
            "test.sql-in-env",
            ("/usr/bin/mariadb",),
            {"sql": "sql_stdin"},
            env_params={"sql": "SQL_QUERY"},
        )
    spec = CommandSpec(
        "test.sql-in-stdin",
        ("/usr/bin/mariadb", "--batch", "--skip-column-names"),
        {"sql": "sql_stdin"},
        stdin_param="sql",
    )
    assert spec.sensitive_params == frozenset({"sql"})
    assert spec.stdin_param == "sql"


@pytest.mark.parametrize(
    "bad_sql",
    [
        "",
        "SELECT 1;\r\n",
        "SELECT 1;\x00",
        "SELECT\t1;\n",
        "SELECT 'é';\n",
        "A" * (MAX_SQL_STDIN_LENGTH + 1),
    ],
)
def test_sql_stdin_rejects_invalid_strings_and_masks_value_in_error(bad_sql: str) -> None:
    secret_sql = f"{bad_sql}{TEST_DB_PASS}" if bad_sql else ""
    with pytest.raises(ParameterError) as exc_info:
        validate_parameter("sql_stdin", "sql", secret_sql)
    assert TEST_DB_PASS not in str(exc_info.value)
    if secret_sql:
        assert MASK in str(exc_info.value)


def test_sql_stdin_rejects_non_string() -> None:
    with pytest.raises(ParameterError, match="must be a string"):
        validate_parameter("sql_stdin", "sql", 123)


def test_sql_stdin_accepts_multiline_printable_ascii_sql() -> None:
    sql = build_service_database_sql(("keystone",), "keystone", TEST_DB_PASS)
    assert validate_parameter("sql_stdin", "sql", sql) == sql


def test_other_parameter_types_still_reject_spaces_and_metacharacters() -> None:
    for type_name in (
        "systemd_unit",
        "package_name",
        "cloud_archive_pocket",
        "hostname",
        "ipv4_address",
        "absolute_path",
        "secret",
    ):
        with pytest.raises(ParameterError, match="whitespace"):
            validate_parameter(type_name, "p", "two words")
        with pytest.raises(ParameterError, match="forbidden character"):
            validate_parameter(type_name, "p", "bad;value")


# --- identifier validation and SQL generation -----------------------------------------


@pytest.mark.parametrize("ident", ["keystone", "nova_api", "nova_cell0", "a1_b2"])
def test_validate_sql_identifier_accepts_valid_names(ident: str) -> None:
    assert validate_sql_identifier("database name", ident) == ident


@pytest.mark.parametrize(
    "bad_ident",
    ["", "Keystone", "1keystone", "keystone-db", "keystone;DROP", "keystone`", 42],
)
def test_validate_sql_identifier_rejects_invalid_names(bad_ident: object) -> None:
    with pytest.raises(ParameterError):
        validate_sql_identifier("database name", bad_ident)


def test_build_service_database_sql_single_and_multi_database() -> None:
    sql = build_service_database_sql(("keystone",), "keystone", TEST_DB_PASS)
    assert sql == (
        "CREATE DATABASE IF NOT EXISTS `keystone`;\n"
        f"GRANT ALL PRIVILEGES ON `keystone`.* TO 'keystone'@'localhost' "
        f"IDENTIFIED BY '{TEST_DB_PASS}';\n"
        f"GRANT ALL PRIVILEGES ON `keystone`.* TO 'keystone'@'%' "
        f"IDENTIFIED BY '{TEST_DB_PASS}';\n"
        "FLUSH PRIVILEGES;\n"
    )

    multi = build_service_database_sql(("nova_api", "nova", "nova_cell0"), "nova", TEST_DB_PASS)
    assert multi.splitlines() == [
        "CREATE DATABASE IF NOT EXISTS `nova_api`;",
        (
            "GRANT ALL PRIVILEGES ON `nova_api`.* TO 'nova'@'localhost' "
            f"IDENTIFIED BY '{TEST_DB_PASS}';"
        ),
        f"GRANT ALL PRIVILEGES ON `nova_api`.* TO 'nova'@'%' IDENTIFIED BY '{TEST_DB_PASS}';",
        "CREATE DATABASE IF NOT EXISTS `nova`;",
        f"GRANT ALL PRIVILEGES ON `nova`.* TO 'nova'@'localhost' IDENTIFIED BY '{TEST_DB_PASS}';",
        f"GRANT ALL PRIVILEGES ON `nova`.* TO 'nova'@'%' IDENTIFIED BY '{TEST_DB_PASS}';",
        "CREATE DATABASE IF NOT EXISTS `nova_cell0`;",
        (
            "GRANT ALL PRIVILEGES ON `nova_cell0`.* TO 'nova'@'localhost' "
            f"IDENTIFIED BY '{TEST_DB_PASS}';"
        ),
        f"GRANT ALL PRIVILEGES ON `nova_cell0`.* TO 'nova'@'%' IDENTIFIED BY '{TEST_DB_PASS}';",
        "FLUSH PRIVILEGES;",
    ]


@pytest.mark.parametrize(
    "bad_dbs",
    [
        (),
        "keystone",
        b"keystone",
        123,
        ("keystone", "keystone"),
        ("keystone", "BadName"),
    ],
)
def test_build_service_database_sql_rejects_bad_databases(bad_dbs: object) -> None:
    with pytest.raises(ParameterError):
        build_service_database_sql(bad_dbs, "keystone", TEST_DB_PASS)  # type: ignore[arg-type]


def test_build_service_database_sql_validates_password() -> None:
    with pytest.raises(SecretError):
        build_service_database_sql(("keystone",), "keystone", "too-short")


def test_plan_service_database_sql_and_redaction() -> None:
    cmd = plan_service_database_sql(("keystone",), "keystone", TEST_DB_PASS)
    assert isinstance(cmd, PlannedCommand)
    assert cmd.command_id == MARIADB_EXECUTE_SQL
    assert cmd.argv(DEFAULT_REGISTRY) == ("/usr/bin/mariadb", "--batch", "--skip-column-names")
    assert TEST_DB_PASS not in cmd.describe(DEFAULT_REGISTRY)
    assert TEST_DB_PASS not in repr(cmd)
    assert MASK in repr(cmd)
    assert TEST_DB_PASS not in redact_text(cmd.params["sql"])


# --- MariaDB state parser, readiness checks, and provisioning -------------------------


def test_parse_mariadb_state_and_verify() -> None:
    stdout = (
        "DB\tinformation_schema\t\t\n"
        "DB\tkeystone\t\t\n"
        "GRANT\tkeystone\tlocalhost\tkeystone\n"
        "GRANT\tkeystone\t%\tkeystone\n"
        "   \n"
        "DB keystone_extra\n"
    )
    state = parse_mariadb_state(stdout)
    assert "keystone" in state.databases
    assert "keystone_extra" in state.databases
    assert ("keystone", "localhost", "keystone") in state.grants
    assert ("keystone", "%", "keystone") in state.grants
    assert verify_service_database_state(stdout, ("keystone",), "keystone") == []


def test_verify_service_database_state_reports_missing_db_and_grants() -> None:
    assert verify_service_database_state("", ("keystone",), "keystone") == [
        "MariaDB database 'keystone' does not exist",
        "MariaDB grant for 'keystone'@'localhost' on database 'keystone' is missing",
        "MariaDB grant for 'keystone'@'%' on database 'keystone' is missing",
    ]
    partial = "DB\tkeystone\t\t\nGRANT\tkeystone\tlocalhost\tkeystone\n"
    assert verify_service_database_state(partial, ("keystone",), "keystone") == [
        "MariaDB grant for 'keystone'@'%' on database 'keystone' is missing",
    ]


def test_provision_and_query_service_database_via_executor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = ServiceHost(root)
    executor = _executor(tmp_path, host)

    assert service_database_ready(executor, ("keystone",), "keystone") is False
    assert service_database_problems(executor, ("keystone",), "keystone") != []

    # Mutating provision command requires host root '/'
    executor_real = CommandExecutor(
        DEFAULT_REGISTRY,
        root=Path("/"),
        audit_log=AuditLog(tmp_path / "audit/commands.log"),
        backend=host,
        echo=lambda _: None,
        euid=lambda: 0,
    )
    res = provision_service_database(executor_real, ("keystone",), "keystone", TEST_DB_PASS)
    assert res.ok
    assert res.argv == ("/usr/bin/mariadb", "--batch", "--skip-column-names")
    assert TEST_DB_PASS not in " ".join(res.argv)

    # Verify stdin received the SQL while argv did not
    exec_argv, exec_stdin = host.stdin_calls[-1]
    assert exec_argv == ("/usr/bin/mariadb", "--batch", "--skip-column-names")
    assert exec_stdin is not None
    assert "CREATE DATABASE IF NOT EXISTS `keystone`;" in exec_stdin
    assert host.mariadb_passwords[("keystone", "localhost")] == TEST_DB_PASS
    assert host.mariadb_passwords[("keystone", "%")] == TEST_DB_PASS

    assert service_database_ready(executor, ("keystone",), "keystone") is True
    assert service_database_problems(executor, ("keystone",), "keystone") == []

    # Audit log never contains the password
    audit_raw = (tmp_path / "audit/commands.log").read_text(encoding="utf-8")
    assert TEST_DB_PASS not in audit_raw


def test_service_database_ready_and_problems_on_query_failure(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    host = ServiceHost(root)
    host.failures[("/usr/bin/mariadb", "--batch")] = ProcessOutput(
        1, "", "ERROR 2002 (HY000): Can't connect to local server\n"
    )
    executor = _executor(tmp_path, host)
    assert service_database_ready(executor, ("keystone",), "keystone") is False
    problems = service_database_problems(executor, ("keystone",), "keystone")
    assert len(problems) == 1
    assert "mariadb query failed" in problems[0]

    with pytest.raises(UnexpectedExitError) as exc_info:
        executor.run(MARIADB_QUERY_SQL)
    assert exc_info.value.hint is not None
    assert "unix_socket" in exc_info.value.hint
