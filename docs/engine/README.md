# Execution engine and `openstack-forge apply`

`openstack_forge.execution` contains the confined file-editing primitives, redaction helper,
state store, step engine, an allowlisted command layer, and two implemented deployment
steps: `hosts-file` and `time-sync-controller`.

**Both steps are marked `unverified` in the step catalog, and neither has ever run on a
real host.** Every other catalog step is rejected as "not implemented" by
`openstack-forge apply`. `hosts-file` is pure Python file operations confined to the target
root. [`time-sync-controller`](#step-time-sync-controller-openstack_forgeexecutionchrony)
is the first step that runs system commands (`apt-get`, `systemctl`, `dpkg-query`), only
through the [command execution layer](#command-execution-layer), whose registry contains
exactly four reviewed commands. Nothing uses `sudo` or the network.

## Why a separate package

`openstack_forge.orchestration` (the step catalog and `plan`), `openstack_forge.diagnostics`
(`doctor`), and `openstack_forge.config` (`config validate`) are documented and tested as
read-only. Putting writing code in `openstack_forge.execution` and importing it **lazily**
only inside the `apply` CLI handler keeps that promise checkable: a test asserts that
importing the CLI or running `config validate`, `plan`, or `doctor` never imports
`openstack_forge.execution`.

## Root directory

Every file operation takes a **root** directory and a **target path** such as
`/etc/hosts`. The target is always mapped under the root (`<root>/etc/hosts`), even when
it is absolute. Tests pass a temporary directory (`--root PATH`); only passing `--yes`
against the default `/` root (which requires effective UID 0) touches the host system.

- Targets containing `..` are refused.
- A target whose resolved location (following symlinks of existing directories) leaves
  the root is refused (`RootEscapeError`).
- A target that is itself a symlink, a directory or another non-regular file is refused
  (`UnsafePathError`), so a write never replaces a link with a file.
- Parent directories of edited target files are never created; a missing parent is an error.

A test installs a CPython audit hook and records every filesystem-modifying event
(`open` for writing, rename/replace, link, unlink, chmod, chown, mkdir, `mkstemp`,
`shutil` copies, xattrs) while the engine and `openstack-forge apply` run real file edits;
every path must lie inside the root.

## File primitives

```python
from openstack_forge.execution import (
    comment_out_lines,
    ini_set,
    managed_block,
    uncomment_lines,
)

managed_block(root, "/etc/hosts", "openstack-forge hosts", "192.0.2.11 ctl-01\n")
comment_out_lines(root, "/etc/hosts", r"^\s*127\.0\.1\.1\s+.*\bctl-01\b")
ini_set(root, "/etc/nova/nova.conf", "api", "auth_strategy", "keystone")
```

All primitives return a `FileChange(path, changed, diff, backup, created)` and accept
`dry_run=True` (compute the redacted diff, write nothing), which is how a step's `check()`
and the default dry-run mode of `openstack-forge apply` work.

### `managed_block`

Inserts or replaces the lines between `# BEGIN <name>` and `# END <name>` (the comment
prefix is configurable). Content outside the markers is never changed; a missing block is
appended, a missing file is created. Unpaired, duplicated or reversed markers, or content
that contains the markers, are refused rather than guessed at.

### `comment_out_lines` and `uncomment_lines`

`comment_out_lines(root, target, pattern, *, prefix="# openstack-forge disabled: ", dry_run=False)`
comments out active lines matching `pattern` (a regex string or compiled `re.Pattern`) by
prepending `prefix`.

- Lines that are already comments (first non-blank character is `#`, including lines
  already carrying `# openstack-forge disabled: `) are skipped, making the primitive
  idempotent.
- Non-matching lines, blank lines, and existing comments are preserved verbatim.
- If the target file does not exist, it returns `changed=False` without creating the file.
- **Reversing or restoring:** `uncomment_lines(root, target, pattern=..., prefix=...)`
  strips `prefix` from previously disabled lines (optionally restricted to those whose
  uncommented text matches `pattern`). Alternatively, because the first modification of an
  existing file saves `<file>.orig`, an operator can restore the pre-modification file
  directly from `<file>.orig` (for example `cp -p /etc/hosts.orig /etc/hosts`).

### `ini_set`

Sets `key = value` in `[section]` with line-based editing; `configparser` is not used
because it drops comments. In order:

1. replace **every** active `key` line in the section (if a file already has duplicates,
   all get the value, so none can override it);
2. otherwise replace the first commented default (`#key = ...` or `;key = ...`) in the
   section;
3. otherwise insert after the section's last non-blank line;
4. otherwise append a new `[section]` at the end of the file (or create the file).

A repeated `[section]` header is treated as the same section. Comments, blank lines and
ordering are preserved, and the call never adds a second line for the same key.

### Guarantees common to all file primitives

- **Idempotent.** A second identical call returns `changed=False`, an empty diff, and
  writes nothing at all (no temporary file, no backup, unchanged mtime).
- **Diff.** Every change returns a unified diff labelled `a/<target>` / `b/<target>`. The
  diff is **redacted** (see below); the file receives the real value.
- **One backup.** Before the first modification of an existing file, it is copied to
  `<file>.orig` (contents, permission bits and timestamps). An existing `.orig` is never
  overwritten, so it keeps the state from before the first change. The backup is copied to
  a temporary file and hard-linked into place, so a partial `.orig` cannot appear. A file
  that did not exist gets no `.orig`.
- **Atomic writes and ownership preservation.** New content is written to a temporary file
  in the same directory, flushed and `fsync`ed, given the original file's owner (`uid`),
  group (`gid`), and permission bits (`mode`), then renamed over the target; the directory
  is `fsync`ed afterwards. An interruption leaves either the old or the new file.
  - When running without root (`chown` not permitted for arbitrary UIDs), `atomic_write_text`
    passes `-1` for any unchanged ID so the process can still preserve the original file's
    group whenever it belongs to that supplementary group (`os.getgroups()`).
  - It never fails silently: if either `uid` or `gid` would change (because `os.chown`
    raised `PermissionError` or the filesystem did not apply it), `UnsafePathError` is
    raised before replacement and the temporary file is removed.
  - **Testing limit:** changing `uid` to another user (`chown` as `root`) cannot be tested
    in an unprivileged test suite without root; non-root unit tests cover supplementary
    group preservation via `os.getgroups()` (skipped if the test user belongs to a single
    group) and verify that unpermitted ownership changes raise `UnsafePathError`.

### Limits

- Files must be UTF-8 with LF line endings. Files containing a carriage return (CRLF) are
  refused instead of being converted.
- `ini_set` matches keys exactly (case-sensitive), understands `=` (not `:`) and bare
  flag lines such as `skip-name-resolve`, and refuses to replace a value continued on
  indented lines. An inline trailing comment on a replaced line is replaced with it.
- When `ini_set` replaces a line, its indentation is not kept.
- A file without a final newline keeps it missing unless content is appended at the end.
- Replacing the file breaks hard links to it, and extended attributes (ACLs, SELinux
  labels) of the original are not copied to the new file.
- Concurrent writers are not coordinated, and paths are checked before use (not
  race-free against someone swapping directories at the same moment).

## Step: `hosts-file` (`openstack_forge.execution.hosts`)

`hosts-file` is the first implemented catalog step. It is a pure, testable operation over a
validated `ClusterConfig`, the target node's hostname, and a target `root` directory:

1. **Managed block (`# BEGIN openstack-forge hosts` ... `# END openstack-forge hosts`)**:
   lists every node in `config.nodes` in configuration order as:
   ```text
   <management_ip> <fqdn> <hostname>
   ```
   For the node with `role == "controller"`, the alias `controller` is appended:
   ```text
   <management_ip> <fqdn> <hostname> controller
   ```
   **Rationale for the `controller` alias:** the official OpenStack installation guide
   consistently uses the hostname `controller` in service endpoints, database connection
   URLs, Memcached servers, and RabbitMQ/Keystone settings, even when a deployment names
   its controller node something like `ctl-01`. Adding the `controller` alias lets both
   the node's real hostname and guide-standard configuration values resolve to the
   controller's management IP. This relies on the cluster having **exactly one controller**,
   which the configuration validator already enforces.
2. **Disabling `127.0.1.1` for the local node:** Debian and Ubuntu installers often map
   `127.0.1.1` to the machine's hostname/FQDN in `/etc/hosts`, which causes services bound
   to the node's hostname to listen on loopback instead of the management network.
   `apply_hosts_file()` comments out (with `# openstack-forge disabled: `) any active line
   whose address is `127.0.1.1` and whose whitespace-delimited tokens (before any `#`
   comment) include this node's `hostname` or `fqdn` (case-insensitively).
   - It **never** touches `127.0.0.1`.
   - It leaves `127.0.1.1` lines for other hostnames untouched.
3. **Pure verification (`verify_hosts_file` / `verify_hosts_text`):** parses `<root>/etc/hosts`
   in pure Python (without calling `getent` or any external command). For each name, the
   **first** active line in `/etc/hosts` that lists that name wins, matching standard
   system resolver (`files`) behaviour. Verification checks that:
   - every node's `hostname` and `fqdn` resolve to its declared `management_ip`;
   - `controller` resolves to the controller node's `management_ip`; and
   - this node's own `hostname` and `fqdn` do not resolve to `127.0.1.1`.

## CLI: `openstack-forge apply`

```sh
openstack-forge apply --config cluster.yaml --step STEP_ID [--node HOSTNAME] [--root PATH] [--yes]
```

| Option | Meaning |
| ------ | ------- |
| `--config PATH` | Cluster configuration (default: `cluster.yaml`), validated before use. |
| `--step STEP_ID` | Catalog step ID to apply (required). Only steps registered in `IMPLEMENTED_STEPS` (`hosts-file`, `time-sync-controller`) can run; any other catalog step exits with code 1 and `"not implemented"`. |
| `--node HOSTNAME` | Target node hostname. If omitted, matched against the local machine's hostname (`socket.gethostname()`). |
| `--root PATH` | Target filesystem root (default: `/`). Used by tests to confine all writes to a temporary directory. |
| `--yes` | Write changes, run the step's commands and update the state file. Without `--yes`, `apply` runs in **dry-run** mode: it prints the redacted unified diff and the exact argv of every command the step would run, writes nothing, runs nothing, and exits 0. |

- **Catalog status warning:** `apply` prints a visible `WARNING` line when a step is marked
  `unverified` in `steps.json`, which both implemented steps are.
- **Dry run:** shows the file diff (or, when the file does not exist yet because its
  package is not installed, the block that will be appended), then the mutating commands
  `apply()` would run in order, then the read-only commands `check()` and `verify()` would
  run. A dry run does not evaluate `check()`, because that would run commands.
- **Steps that run commands and `--root`:** for a step that declares commands, `--yes`
  with a `--root` that does not resolve to `/` is refused with exit code 1 before anything
  is written: those commands would act on the real host, not on the root directory. (The
  command executor refuses mutating commands under such a root as a second safeguard.)
- **Root check:** writing to the real `/` root (`--yes` without `--root`, or `--root /`)
  requires effective UID 0 (`os.geteuid() == 0`); otherwise `apply` fails with a clear
  message and exits 1. Passing `--root PATH` (where `PATH` does not resolve to `/`)
  bypasses the UID check for non-root tests while still confining every write under `PATH`.
- **State file location:** `<root>/var/lib/openstack-forge/state.json` (so `/var/lib/openstack-forge/state.json`
  on `/`, or `<tmp>/var/lib/openstack-forge/state.json` under `--root <tmp>`), written
  atomically with mode `0600` only when `--yes` is given.
- **Audit log location:** `<root>/var/log/openstack-forge/commands.log` (mode `0600`; its
  directory is created with mode `0700` on the first `--yes` run of a step that declares
  commands). Nothing uses `sudo`: writing to `/` requires running the tool as root.

## Redaction

`redact_text()` masks, with `********`:

- values of keys whose name contains `password`, `passwd`, `secret` or `token`
  (`key = value`, `key: value`, `"key": "value"`, `KEY=value`), quoted values up to the
  closing quote, unquoted values up to the end of the line;
- the same keys as command-line flags (`--os-password VALUE`, `--token=VALUE`);
- passwords in URLs (`mysql+pymysql://nova:********@controller/nova`).

`redact_data()` applies the same to JSON-like structures. Redaction is used for every diff,
every stored error and every failure detail. It is deliberately over-eager (an unquoted
match masks the rest of the line), and it cannot recognise a secret that has no telltale
key, such as a bare password printed by a tool.

## State store

`StateStore(path)` keeps one JSON file (by default `<root>/var/lib/openstack-forge/state.json`):

```json
{
  "version": 1,
  "steps": {
    "hosts-file": {"status": "done", "started_at": "...", "finished_at": "...", "error": null}
  }
}
```

- Statuses: `pending`, `running`, `done`, `failed`. Timestamps are UTC ISO 8601.
- Every write is atomic (as above) and the file is always mode `0600`, even if it existed
  with wider permissions.
- It never stores configuration values: only status, timestamps and an error summary,
  which is redacted and truncated to 2000 characters.
- An unreadable, unknown-version or malformed file is an error; it is never silently reset.
- Its directory must already exist (`apply --yes` creates `<root>/var/lib/openstack-forge`
  inside the confined root if needed before invoking the engine).

## Engine

A step is any object with an `id` and three methods:

| Method | Meaning |
| ------ | ------- |
| `check() -> bool` | `True` if the step's result is already in place. |
| `apply()` | Make the change. |
| `verify() -> VerifyResult(ok, details)` | Confirm the change worked. |

`Engine(store).run(steps)` processes the steps in order:

1. `check()`; if it is true, the step is skipped (and recorded as done if it was not
   already).
2. Otherwise the step is recorded as `running`, `apply()` runs, then `verify()`.
3. The step is recorded as `done` only if `verify()` returns `ok=True`.
4. On the first exception or failed verification, the step is recorded as `failed` and the
   run stops. The `RunReport` names the step, the phase (`check`, `apply` or `verify`),
   what failed, the redacted details, a suggested next action, and the steps not started.

`resume(steps)` finds the first step recorded as `failed` (or `running`, meaning the
process was interrupted), calls only its `verify()`: it is **not** applied again, because
the operator is expected to have fixed the host. If verification passes, the step is
marked done and the remaining steps run as in `run()`. If nothing failed, `resume()` is
`run()`. Calling `run()` again instead retries a failed step from its `check()`.

Re-running after a successful run changes nothing: every `check()` is true, the state file
already says `done`, so no file and not even the state file is written.

Limits: steps run one at a time on the local process; there is no parallelism, no
rollback (the `.orig` backups and `uncomment_lines()` exist for a person to use), no
locking against two engines sharing a state file, and no timeout around a step's methods.

## Command execution layer

`openstack_forge.execution.commands` (with `audit.py`) is the only code in the project that
may start a process on behalf of a deployment step. It exists so that future steps (for
example installing a package or restarting a service) have one reviewed path for doing so.
`DEFAULT_REGISTRY` contains exactly the four reviewed commands below, and a test pins
their argv, parameter types, exit codes and `mutates` flags, so adding or changing one is a
deliberate edit. The tests only ever run harmless `sys.executable` one-liners; everything
else uses a fake backend.

| Id | argv | Mutates | Allowed exit codes | Justification |
| -- | ---- | ------- | ------------------ | ------------- |
| `apt-get.install` | `/usr/bin/apt-get install --yes {package}` | yes | 0 | apt(8) "SCRIPT USAGE": scripts should use `apt-get`, not `apt`. apt-get(8) `--yes`: "assume yes ... run non-interactively", still aborting on held, unauthenticated or essential packages. Timeout 900 s. |
| `systemctl.restart` | `/usr/bin/systemctl restart {unit}` | yes | 0 | systemctl(1): stops and starts the unit, starting it if it was not running. Timeout 120 s. |
| `systemctl.is-active` | `/usr/bin/systemctl is-active {unit}` | no | 0-4 | systemctl(1): exit 0 if active, non-zero otherwise; the LSB codes 1-4 all mean "not active", and the man page advises reading the printed state, so callers also require the output `active`. |
| `dpkg-query.status` | `/usr/bin/dpkg-query --status {package}` | no | 0, 1 | dpkg-query(1): `--status` shows the status database entry; exit 1 means the package was not found, 2 is fatal. |

Each path is the one installed by the `apt`, `systemd` and `dpkg` packages on Ubuntu 24.04
(checked with `dpkg -S`); `PATH` is never searched. No other apt or dpkg option is used:
in particular no `--force-*`, no `-o Dpkg::Options` and no lock timeout. A configuration
file prompt from dpkg therefore makes the command fail (stdin is `/dev/null`) instead of
being answered automatically.

### Allowlist, not command strings

A command is a `CommandSpec`:

| Field | Meaning |
| ----- | ------- |
| `id` | Stable name, e.g. `chrony.restart`. |
| `argv` | Template tuple. `argv[0]` must be a literal **absolute path** (no `PATH` lookup). Every other element is a literal or exactly `{name}`. |
| `params` | `{name: type}` for every placeholder; each placeholder appears exactly once. |
| `timeout` | Seconds; required to be positive. |
| `allowed_exit_codes` | Exit codes that count as success for the caller (default `{0}`). |
| `mutates` | Whether the command changes the system. |

Specs are checked when created: shells (`sh`, `bash`, ...), `env`, `xargs`, `chroot`,
`timeout`, `sudo`, `su`, `doas`, `pkexec` and `runuser` cannot be the executable, `sudo`/`su`
cannot appear anywhere in argv, and a placeholder may not be part of a larger argument
(`--unit={unit}` is rejected) so a parameter can never smuggle an option. Specs are only
executable once registered in a `CommandRegistry`; `CommandExecutor.run(command_id, params)`
is the only entry point, and an unknown id raises `UnknownCommandError`. No function takes a
command string.

### Parameter validation

Every value must be a string and is checked **before** anything runs or is logged:

1. it must not be empty or start with `-`;
2. it must not contain whitespace, control characters or any of
   `` ; | & $ ` " ' \ < > ( ) { } [ ] * ? ! # ~ % ^ , ``;
3. it must fully match its type:

| Type | Accepts |
| ---- | ------- |
| `systemd_unit` | `[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}`, e.g. `chrony.service` |
| `package_name` | Debian names `[a-z0-9][a-z0-9+.-]{1,127}`, e.g. `python3-openstackclient` |
| `cloud_archive_pocket` | `cloud-archive:<name>` with optional `-updates`/`-proposed` |
| `hostname` | one DNS label |
| `ipv4_address` | dotted quad, also checked with `ipaddress` |
| `absolute_path` | `/`-separated segments of `[A-Za-z0-9._@+-]`, no empty, `.` or `..` segment |

Missing or unexpected parameters are errors too. There is no free-text type.

### Running a command

`CommandExecutor(registry, root=..., audit_log=..., backend=..., dry_run=...)`:

- **No shell.** The default `SubprocessBackend` passes the argv list to `subprocess.Popen`
  with `shell=False`, stdin from `/dev/null`, `cwd="/"`, and a new session; the backend is
  injectable (`ProcessBackend`), like `diagnostics.CommandRunner`.
- **Sanitized environment.** The child gets exactly `PATH=/usr/sbin:/usr/bin:/sbin:/bin`,
  `LC_ALL=C`, `LANG=C` and `DEBIAN_FRONTEND=noninteractive`; nothing is inherited (no
  `LD_PRELOAD`, no credentials, no `HOME`).
- **Timeout.** On expiry the whole process group gets `SIGTERM`; if it is still running
  after a grace period (`SubprocessBackend(term_grace_period=...)`, default 5 s) it gets
  `SIGKILL`. `CommandTimeoutError.killed` says which happened.
- **Failure hints.** A spec may carry a `failure_hint`; the executor attaches it to the
  error, and the engine appends it to the suggested next action. `apt-get.install` uses
  it to say that apt may have left packages half-installed or half-configured (inspect
  with `dpkg --audit`) and that a "could not get lock" error means another package manager
  is running (wait; do not delete lock files). No recovery is attempted automatically.
- **Typed errors.** `CommandNotFoundError` (missing or non-executable binary),
  `CommandTimeoutError`, `UnexpectedExitError` (exit code not in `allowed_exit_codes`; carries
  the code and redacted stderr), `ParameterError`, `UnknownCommandError`,
  `MutationRefusedError`, `AuditLogError`; any other launch failure becomes `CommandError`.
- **Output.** stdout and stderr are decoded as UTF-8 (invalid bytes replaced), passed
  through `redact_text()`, and only then truncated to the last 64 KiB characters per stream
  (configurable) with a marker saying how much was removed. Redacting first means a cut can
  never leave a secret's value without the key that identifies it. Unredacted output is
  never returned, stored or logged.

### Safety rules

- **Mutating commands only touch the real host.** A command with `mutates=True` is refused
  (`MutationRefusedError`) unless the executor's root resolves to `/`, so tests and
  `--root` runs can never change the host, and unless the effective uid is 0: the tool is
  expected to run as root when it writes to `/`, and `sudo` is never used.
- **Dry run.** With `dry_run=True` the executor prints
  `DRY RUN [<id>]: <exact argv, shell-quoted>` and runs nothing; parameters are still
  validated. Dry-run printing is allowed under any root because nothing executes.
- **Audit log.** Before a command starts, a `start` record is appended to the `AuditLog`
  (JSON lines; `O_APPEND`, never truncated, symlinks refused, mode `0600` enforced, fsynced).
  If that record cannot be written, the command does not run. After it ends, a `finish`
  record adds the outcome (`ok`, `unexpected_exit`, `timeout`, `not_found`, `error`), exit
  code and duration. Records contain the command id and redacted argv, never output. Dry
  runs, refused commands and invalid parameters are not recorded, because nothing ran.

### Use from steps

Steps call the executor from `check()`, `apply()` or `verify()`. `command_succeeds()` returns
whether a command exited 0 (useful with e.g. `allowed_exit_codes={0, 3}`), and
`verify_command()` turns a command into a `VerifyResult` whose details are the redacted
output. Exceptions raised by the executor are reported by the engine as usual, so failure
details, `VerifyResult` details, the state file and the audit log all stay redacted.

### Limits

- Redaction only recognises secrets next to a telltale key, flag or URL; a tool that
  prints a bare password is not protected.
- Output is captured in memory before truncation, so a command that prints gigabytes
  uses that much memory.
- Read-only commands (`mutates=False`) run whatever the root is, and they inspect the real
  host, not the root directory.
- The allowlist is only as safe as the reviewed specs: a registered interpreter with `-c`
  (as the tests use) can run anything, so real specs must name the real tool.
- No retries, no locking between concurrent executors, and the audit log is local and not
  tamper-proof against root.

## Step: `time-sync-controller` (`openstack_forge.execution.chrony`)

**Unverified, and never run on a real host.** It follows the official controller page,
<https://docs.openstack.org/install-guide/environment-ntp-controller.html>, which for Ubuntu
says to install `chrony`, edit `/etc/chrony/chrony.conf` with `server NTP_SERVER iburst`
lines and `allow <subnet>`, and restart the service.

| Phase | What happens |
| ----- | ------------ |
| `check()` | True only if `chrony.conf` needs no edit (pure) **and** `dpkg-query --status chrony` reports `install ok installed`. The file is checked first, so `dpkg-query` is only run when the file is already correct. |
| `apply()` | `apt-get install --yes chrony`; then the file edit; then `systemctl restart chrony`. |
| `verify()` | Pure check that the managed block exists with the expected content and contains `allow <networks.management.subnet>` (and, with `ntp_servers`, that no active `pool`/`server` line is left outside it), plus `systemctl is-active chrony` printing `active`. |

The file edit, in `chrony.conf` only, between `# BEGIN openstack-forge time-sync` and
`# END openstack-forge time-sync`:

- with `cluster.ntp_servers`: every active `pool` and `server` line outside the block is
  commented out with the `# openstack-forge disabled: ` prefix, and the block contains one
  `server <name> iburst` line per server;
- without `cluster.ntp_servers`: the distribution's default pool is kept, and lines this
  step disabled earlier are restored;
- in both cases the block ends with `allow <networks.management.subnet>`.

Everything else in the file (comments, `confdir`, `sourcedir`, `driftfile`, ...) is left as
it is; `comment_out_lines(..., exclude_block=...)` never touches the block's own lines, so
re-running writes nothing. The file must already exist (it is installed by the package); the
step never creates it. The step requires a `management` network and only runs on the
controller.

Deviations from the page, and limits:

- The page restarts with `service chrony restart`; this step uses `systemctl restart chrony`.
- The page says to "add, change, or remove" keys as necessary; commenting out the existing
  sources when servers are configured is this project's reading of it.
- `sourcedir` directives (for example servers learned from DHCP) are not touched.
- **Time synchronization itself is not checked**, because the guide's NTP verification page
  has not been reviewed yet; `verify()` says so in its details.
- `time-sync-other` (the other nodes) is in the catalog but not implemented; its page has
  not been read.

