# Execution package

Confined file edits, redaction, secrets store, state store, step engine, the allowlisted
command layer, the reusable MariaDB SQL provisioning brick, the reusable `/usr/bin/openstack`
CLI provisioning brick, and the implemented `hosts-file`, `time-sync-controller`,
`time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`,
`keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`, `neutron-controller`,
`neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage` steps used by
`openstack-forge apply` and `openstack-forge verify`.
Imported lazily by the `apply`, `verify`, and `secrets` CLI handlers so that `init`,
`config validate`, `doctor`, and `plan` never import it.

## Modules

- `fsutil.py` - maps target paths under a root directory, atomic writes that preserve
  owner, group (uid/gid) and mode, one-time `.orig` backups, redacted unified diffs.
- `files.py` - `managed_block()`, `ini_set()`, `ini_set_many()` (including `comment_out`),
  `ini_comment_out_keys()`, `comment_out_lines()` and `uncomment_lines()`.
- `hosts.py` - the `hosts-file` step (`HostsFileStep`, `build_hosts_block()`,
  `parse_hosts()`, `verify_hosts_file()`).
- `redaction.py` - `redact_text()`, `redact_data()`, and exact-value secret registration
  (`register_secret()`, `register_secrets()`).
- `secrets.py` - `SecretStore`, `SecretSpec` (`SECRET_SPECS` / `EXTRA_SECRET_SPECS`), and
  `generate_secret()` for the `0600` `/etc/openstack-forge/secrets.json` store.
- `state.py` - `StateStore` (the `0600` JSON step-status file) and `apply_lock()` (exclusive
  advisory lock at `<root>/var/lib/openstack-forge/apply.lock`).
- `engine.py` - `Engine.run()` / `Engine.resume()` over check/apply/verify steps.
- `commands.py` - allowlisted `CommandSpec`s (including `sensitive_params`, `stdin_param`,
  `env_params`, and `sql_stdin`), typed parameter validation, `CommandRegistry`
  (`DEFAULT_REGISTRY` holds the forty-nine reviewed system commands), and `CommandExecutor`
  (including `read_only=True` enforcement mode) with an injectable backend, sanitized
  environment, SIGTERM-then-SIGKILL timeouts, redacted and truncated output.
- `sql.py` - reusable MariaDB database and user-grant provisioning brick via `stdin`
  (`build_service_database_sql()`, `provision_service_database()`, `service_database_ready()`,
  `service_database_problems()`).
- `openstack_cli.py` - reusable `/usr/bin/openstack` CLI provisioning brick
  (`wait_for_keystone_ready()`, `ensure_service_project()`, `ensure_service_user()`,
  `ensure_service_catalog()`, `service_project_ready()`, `service_user_ready()`,
  `service_catalog_ready()`, and JSON output parsers).
- `chrony.py` - the `time-sync-controller` and `time-sync-other` steps (unverified, never
  run on a real host).
- `openstack_packages.py` - the `openstack-packages` step (unverified, never run on a real
  host).
- `mariadb.py` - the `mariadb` step (unverified, never run on a real host).
- `rabbitmq.py` - the `rabbitmq` step (unverified, never run on a real host).
- `memcached.py` / `etcd.py` - the `memcached` and `etcd` steps (unverified, never run on a
  real host).
- `keystone.py` - the `keystone` step (unverified, never run on a real host).
- `glance.py` - the `glance` step (unverified, never run on a real host).
- `placement.py` - the `placement` step (unverified, never run on a real host).
- `nova.py` - the `nova-controller` and `nova-compute` steps (unverified, never run on a real
  host).
- `neutron.py` - the `neutron-controller` and `neutron-compute` steps (unverified, never run
  on a real host).
- `horizon.py` - the `horizon` controller step (unverified, never run on a real host).
- `cinder.py` - the `cinder-controller` and `cinder-storage` steps (unverified, never run on a
  real host).
- `service_checks.py` - read-only package / unit-active / unit-enabled checks shared by
  the controller service steps.
- `apt_output.py` - pure parsers for `dpkg-query --status` and `apt-cache policy` output,
  and `installed_from()` (is the installed version one of the pocket's?).
- `preview.py` - `StepPreview` / `PlannedCommand` used by `apply` dry runs.
- `steps.py` - `IMPLEMENTED_STEPS` and `build_step()`.
- `audit.py` - `AuditLog`, the append-only 0600 JSON-lines record of executed commands.
- `errors.py` - `ExecutionError` and its subclasses.

See [`docs/engine`](../../../docs/engine/README.md) for the guarantees and limits.
