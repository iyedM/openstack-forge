# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added

- Initial repository skeleton for OpenStack Forge.
- Python project configuration and minimal CLI scaffolding.
- Example YAML cluster configuration and initial JSON Schema draft.
- Ansible and documentation placeholders.
- CI, lint, security, and dependency automation scaffolds.
- Initial test coverage for package import and CLI behavior.
- Cluster configuration loading and validation (`openstack_forge.config`): safe YAML
  parsing with duplicate-key detection, JSON Schema validation and semantic checks.
- Working `openstack-forge config validate [PATH]` command (exit 0 if valid, 1 otherwise).
- Unit tests and YAML fixtures for valid and invalid configurations.
- Supported release table (`openstack_forge.config.releases`): Ubuntu 24.04 with
  OpenStack 2025.1 (Epoxy) from `cloud-archive:epoxy`; `config validate` prints it.
- Semantic checks for the supported release pair, role counts (exactly one controller,
  at least one compute, at most one block and one object node) and provider network
  ranges (inside the subnet, ordered, excluding the gateway and node addresses, no
  overlap between `floating_ip_range` and `dhcp_range`).
- Required per-node `vcpus` and `ram_gb`; role-dependent `disk` (block) and `disks`
  (object) device paths.
- Packaging test that builds a wheel and validates the example from it.
- Read-only local `openstack-forge doctor --config cluster.yaml [--node HOSTNAME] [--json]`
  command (`openstack_forge.diagnostics`) reusing the configuration validator.
- Local node identification by `--node`, local hostname matching `nodes[].hostname`, or
  local IPv4 matching `nodes[].management_ip`, with actionable errors when ambiguous or
  not found.
- Injectable `CommandRunner` abstraction (`SubprocessRunner` with `shell=False` and
  timeouts) and separate pure parsers for `/etc/os-release`, `nproc`, `/proc/meminfo`,
  `/proc/cpuinfo`, `systemd-detect-virt`, `lsblk -J -b`, and `ip -j addr`.
- Pure diagnostic checks for OS version, vCPUs, RAM (with documented 10% kernel-reserved
  tolerance), virtualization environment (`systemd-detect-virt`), compute CPU
  virtualization flags (`vmx`/`svm`, warning that Nova will need `virt_type=qemu` when
  absent), block and object storage disk readiness, network interfaces and management IP
  assignment, and shared management/provider interface bridge migration warnings.
- Recorded command outputs under `tests/fixtures/doctor/`, unit tests with a fake
  `CommandRunner`, and check documentation in `docs/doctor/README.md`.
- Read-only `openstack-forge plan --config cluster.yaml [--node HOSTNAME] [--json]`
  command (`openstack_forge.orchestration`). It reuses the configuration validator, orders
  the steps for every node with dependencies resolved across nodes ("waits for <step> on
  <node>"), flags unverified steps and counts them in the summary. It runs no command and
  uses no network.
- Deployment step catalog shipped as package data
  (`src/openstack_forge/orchestration/data/steps.json`) with its own JSON Schema, checked
  for unique ids, existing dependencies and cycles. Initial steps, all **unverified**:
  `/etc/hosts`, chrony, `cloud-archive:epoxy` with `python3-openstackclient`, MariaDB,
  RabbitMQ, Memcached and etcd. Network configuration is recorded as an open decision
  (the guide edits `/etc/network/interfaces`; Ubuntu 24.04 uses netplan).
- `docs/plan/README.md`, including when a step may be marked verified.
- Execution building blocks in a new `openstack_forge.execution` package, kept separate
  so that `orchestration` and the CLI stay read-only (a test checks they never import it).
  No real installation step exists yet: there is no `apply` command, no catalog step is
  executed, and nothing runs commands, uses `sudo` or the network.
  - `managed_block()` and `ini_set()` (line-based, comment-preserving, no `configparser`)
    with target paths confined to an injectable root, redacted unified diffs, `changed`
    flags, idempotent re-runs that write nothing, a one-time `<file>.orig` backup, and
    atomic writes that keep permissions. LF-only UTF-8 files; CRLF files are refused.
  - `redact_text()` / `redact_data()` masking values of password, secret and token keys,
    such flags, and URL passwords.
  - `StateStore`: per-step status (pending/running/done/failed), timestamps and redacted
    error summaries in an atomically written 0600 JSON file.
  - `Engine.run()` / `Engine.resume()`: check, apply, verify in order; stop at the first
    failure with a report and suggested next action; resume verifies the failed step
    before continuing.
- `docs/engine/README.md` describing the guarantees and limits.
- Ownership and group preservation in `atomic_write_text` (`openstack_forge.execution.fsutil`):
  preserves the original file's `uid`, `gid`, and `mode` (passing `-1` for unchanged IDs so
  non-root users preserve supplementary groups from `os.getgroups()`), and raises
  `UnsafePathError` rather than failing silently if ownership would change.
- `comment_out_lines()` and `uncomment_lines()` in `openstack_forge.execution.files` to
  idempotently disable active lines matching a regex with `# openstack-forge disabled: `,
  with redacted diffs, `.orig` backup, `dry_run`, and root confinement.
- First implemented step `hosts-file` (`openstack_forge.execution.hosts`), still marked
  `unverified` in the step catalog and not yet run on a real VM:
  - writes the `# BEGIN openstack-forge hosts` managed block for all cluster nodes
    (`management_ip`, `fqdn`, `hostname`, plus alias `controller` for the single controller);
  - comments out active `127.0.1.1` lines mapping to the local node's `hostname` or `fqdn`
    while never touching `127.0.0.1` or other hosts' entries;
  - pure-Python `verify()` parser over `/etc/hosts` (first matching line wins, no `getent`
    or external commands).
- Minimal `openstack-forge apply --config cluster.yaml --step hosts-file [--node HOSTNAME] [--root PATH] [--yes]`
  command:
  - explicit `IMPLEMENTED_STEPS` registry (`hosts-file` only; other catalog steps fail with
    `"not implemented"` and exit 1);
  - dry-run by default (prints redacted diff, writes nothing, exits 0); writes only with
    `--yes`, requiring effective UID 0 when targeting `/` and recording state under
    `<root>/var/lib/openstack-forge/state.json` (mode `0600`);
  - lazy import of `openstack_forge.execution` inside the `apply` handler so `config validate`,
    `doctor`, and `plan` never import it.
- Allowlisted command execution layer for future mutating steps
  (`openstack_forge.execution.commands` and `.audit`). **No real system command is
  registered** (`DEFAULT_REGISTRY` is empty) and no CLI command uses it.
  - `CommandSpec` (id, absolute-path argv template with whole-argument placeholders, typed
    parameters, timeout, allowed exit codes, `mutates`) and `CommandRegistry`; no API accepts
    a command string, shells, `env` and `sudo`/`su` are rejected as executables.
  - Strict per-type parameter validation (`systemd_unit`, `package_name`,
    `cloud_archive_pocket`, `hostname`, `ipv4_address`, `absolute_path`) after rejecting
    whitespace, shell metacharacters, quotes and a leading `-`.
  - `CommandExecutor` with an injectable backend, no shell, sanitized environment
    (`PATH`, `LC_ALL=C`, `LANG=C`, `DEBIAN_FRONTEND=noninteractive`), process-group kill on
    timeout, output redacted then truncated, and typed errors.
  - Mutating commands refuse to run unless the root is `/` and the effective uid is 0;
    `dry_run` prints the exact argv and runs nothing; an append-only 0600 audit log records
    start and finish (outcome, exit code, duration) of every executed command, redacted.
  - `command_succeeds()` / `verify_command()` helpers for steps; engine failure details,
    `VerifyResult` details and state stay redacted.
- `time-sync-controller` step (`openstack_forge.execution.chrony`), the first step that
  runs system commands. **Unverified and never run on a real host.** Following the official
  controller NTP page: install `chrony`, add a managed block to `/etc/chrony/chrony.conf`
  with `server <name> iburst` per `cluster.ntp_servers` entry (existing `pool`/`server`
  lines are commented out only when servers are configured) and
  `allow <networks.management.subnet>`, then `systemctl restart chrony`. `verify()` checks
  the file and `systemctl is-active`; actual time synchronization is not checked yet.
- The first real command specs in `DEFAULT_REGISTRY`: `apt-get install --yes`,
  `systemctl restart`, `systemctl is-active` and `dpkg-query --status`, with literal
  absolute paths and justifications from their manual pages.
- Optional `cluster.ntp_servers` in the configuration schema (see
  `docs/configuration/README.md`).
- `apply` dry runs show the exact argv of every command a step would run; `--yes` with a
  `--root` other than `/` is refused for steps that run commands; command audit log at
  `<root>/var/log/openstack-forge/commands.log` (0600).
- `comment_out_lines(..., exclude_block=...)` to leave a managed block's own lines alone.

### Changed

- `jsonschema` and `PyYAML` are now runtime dependencies instead of dev-only extras.
- The schema now restricts `nodes[].hostname` to a single RFC 1123 DNS label and
  `nodes[].fqdn` to a dotted DNS name (see `docs/configuration/README.md`).
- Pre-commit `check-yaml` skips `tests/fixtures/`, which holds intentionally invalid YAML.
- **Breaking (configuration format):** `ubuntu_version` must be `"24.04"` and
  `openstack_release` `"2025.1"`; role `storage` is replaced by `block` and `object`;
  `vcpus`, `ram_gb` and `networks.provider` (with `floating_ip_range`) are required.
  Reasons and migration steps are in `docs/configuration/README.md`.
- The provider network may share the management subnet if the subnets are identical.
- The JSON Schema moved from `schemas/` into the package
  (`src/openstack_forge/config/schemas/cluster.schema.json`) and is shipped as package data.
- Minimum `jsonschema` lowered from 4.23 to 4.10.3, the version packaged by Ubuntu 24.04;
  the test suite passes against it.
- `setuptools` and `wheel` added to the `dev` extra for the offline packaging test.
- The catalog's `time-sync` step is split into `time-sync-controller` (controller) and
  `time-sync-other` (compute, block, object; depends on `time-sync-controller`; not
  implemented). `openstack-packages` now depends on `time-sync-controller` only, so other
  nodes do not wait for their own `time-sync-other` (documented; per-role dependencies are
  future work).
- Command timeouts now send SIGTERM to the process group, wait a configurable grace period
  (default 5 s), then send SIGKILL.
- Engine failure reports append command-specific hints; an `apt-get` failure mentions a
  possible half-configured dpkg state and the dpkg lock, without any automatic recovery.
- The registry test now pins the four reviewed commands instead of asserting an empty
  registry; `IMPLEMENTED_STEPS` moved to `openstack_forge.execution.steps`.

### Fixed

- Validation now works from a regular (non-editable) install; previously the schema
  was only found in a source checkout.
