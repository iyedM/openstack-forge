# Execution package

Confined file edits, redaction, state store, step engine, the allowlisted command layer,
and the implemented `hosts-file`, `time-sync-controller` and `openstack-packages` steps used
by `openstack-forge apply`. Imported lazily by the `apply` CLI handler so that
`config validate`, `doctor`, and `plan` never import it.

## Modules

- `fsutil.py` - maps target paths under a root directory, atomic writes that preserve
  owner, group (uid/gid) and mode, one-time `.orig` backups, redacted unified diffs.
- `files.py` - `managed_block()`, `ini_set()`, `comment_out_lines()` and
  `uncomment_lines()`.
- `hosts.py` - the `hosts-file` step (`HostsFileStep`, `build_hosts_block()`,
  `parse_hosts()`, `verify_hosts_file()`) and `IMPLEMENTED_STEPS`.
- `redaction.py` - `redact_text()` and `redact_data()`.
- `state.py` - `StateStore`, the 0600 JSON step-status file.
- `engine.py` - `Engine.run()` / `Engine.resume()` over check/apply/verify steps.
- `commands.py` - allowlisted `CommandSpec`s, typed parameter validation,
  `CommandRegistry` (`DEFAULT_REGISTRY` holds the seven reviewed system commands), and
  `CommandExecutor` with an injectable backend, sanitized environment, SIGTERM-then-SIGKILL
  timeouts, redacted and truncated output.
- `chrony.py` - the `time-sync-controller` step (unverified, never run on a real host).
- `openstack_packages.py` - the `openstack-packages` step (unverified, never run on a real
  host).
- `apt_output.py` - pure parsers for `dpkg-query --status` and `apt-cache policy` output.
- `preview.py` - `StepPreview` / `PlannedCommand` used by `apply` dry runs.
- `steps.py` - `IMPLEMENTED_STEPS` and `build_step()`.
- `audit.py` - `AuditLog`, the append-only 0600 JSON-lines record of executed commands.
- `errors.py` - `ExecutionError` and its subclasses.

See [`docs/engine`](../../../docs/engine/README.md) for the guarantees and limits.
