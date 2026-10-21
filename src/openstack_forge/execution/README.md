# Execution package

Confined file edits, redaction, state store, step engine, and the implemented `hosts-file`
step used by `openstack-forge apply`. Imported lazily by the `apply` CLI handler so that
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
- `errors.py` - `ExecutionError` and its subclasses.

See [`docs/engine`](../../../docs/engine/README.md) for the guarantees and limits.
