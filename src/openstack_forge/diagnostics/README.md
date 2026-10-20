# Diagnostics package

Implements read-only, local host diagnostics (`openstack-forge doctor`).

## Modules

- `runner.py` - `CommandRunner` protocol, `CommandResult` and `SubprocessRunner`
  (`subprocess.run` without `shell=True`, with timeouts).
- `parsers.py` - pure parsers for `/etc/os-release`, `nproc`, `/proc/meminfo`,
  `/proc/cpuinfo`, `systemd-detect-virt`, `lsblk -J -b` and `ip -j addr`.
- `node.py` - `identify_node()` resolves the local node from `--node`, the local
  hostname, or local IPv4 addresses.
- `checks.py` - pure check functions over parsed system data returning `PASS`,
  `WARN`, or `FAIL` with declared vs observed values.
- `doctor.py` - `run_doctor()` and report formatters (table and JSON).
- `errors.py` - `DoctorError` hierarchy (`CommandError`, `CommandNotFoundError`,
  `CommandTimeoutError`, `ParseError`, `NodeIdentificationError`).

See [`docs/doctor`](../../../docs/doctor/README.md) for the full list of checks and
their rationale. No remote connectivity, privileged commands, or system modifications
are executed here.
