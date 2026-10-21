# Execution engine and `openstack-forge apply`

`openstack_forge.execution` contains the confined file-editing primitives, redaction helper,
secrets store, state store, step engine, an allowlisted command layer, a reusable MariaDB
SQL provisioning brick, a reusable `/usr/bin/openstack` CLI provisioning brick, and nine
implemented deployment steps: `hosts-file`, `time-sync-controller`, `time-sync-other`,
`openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`, and `keystone`.

**All nine steps are marked `unverified` in the step catalog, and none has ever run on a
real host.** Every other catalog step is rejected as "not implemented" by
`openstack-forge apply`. `hosts-file` is pure Python file operations confined to the target
root. [`time-sync-controller`](#step-time-sync-controller-openstack_forgeexecutionchrony),
[`time-sync-other`](#step-time-sync-other-openstack_forgeexecutionchrony),
[`openstack-packages`](#step-openstack-packages-openstack_forgeexecutionopenstack_packages),
[`mariadb`](#step-mariadb-openstack_forgeexecutionmariadb),
[`rabbitmq`](#step-rabbitmq-openstack_forgeexecutionrabbitmq),
[`memcached`](#step-memcached-openstack_forgeexecutionmemcached),
[`etcd`](#step-etcd-openstack_forgeexecutionetcd),
and [`keystone`](#step-keystone-openstack_forgeexecutionkeystone)
run system commands (`apt-get`, `add-apt-repository`, `apt-cache`, `systemctl`,
`dpkg-query`, `rabbitmqctl`, `mariadb`, `keystone-manage`, `openstack`), only through the
[command execution layer](#command-execution-layer), whose registry contains exactly
thirty-one reviewed commands. Nothing uses `sudo` or `su`.

**Network:** OpenStack Forge's Python code opens no network connection. Some of the
commands it runs do: `apt-get update`, `apt-get install` and `add-apt-repository` download
package lists and packages from the node's configured apt sources (and, for
`openstack-packages`, the Ubuntu Cloud Archive). `hosts-file`, `config validate`, `doctor`
and `plan` use no network.

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

### `disable_lines_and_set_block`

`disable_lines_and_set_block(root, target, pattern, name, content)` is `comment_out_lines`
(with `exclude_block=name`) followed by `managed_block`, done as **one** edit: one write,
one `.orig` backup, one diff. It exists for the `memcached` and `etcd` steps: with two
separate writes, a crash between them would leave the old lines disabled and no block, and
for memcached that means no `-l` line at all, i.e. listening on every interface. The file
must already exist (a missing file is left absent), and re-running it writes nothing.

### `ini_set_many`

`ini_set_many(root, target, settings, *, dry_run=False, new_file_mode=0o644)` applies
`(section, key, value)` triples in order, each with the rules of `ini_set`, in a **single**
edit of the file. Calling `ini_set` once per key is wrong for a file that does not exist
yet: the first call creates it with one key, and the second call then backs up that
one-key file as `<file>.orig`; in dry-run mode, separate calls also do not see each
other's edits. With `ini_set_many`:

- all settings are validated before the file is read;
- the result has one combined, redacted unified diff;
- a file that did not exist is created with `created=True` and **no** `.orig` backup;
- an existing file that changes gets one `<file>.orig` backup of its content before the
  call (an existing `.orig` is still never overwritten);
- if every key already has its value, it returns `changed=False`, an empty diff, and
  writes nothing;
- the parent directory must already exist; it is never created.

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
| `--step STEP_ID` | Catalog step ID to apply (required). Only steps registered in `IMPLEMENTED_STEPS` (`hosts-file`, `time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`, `keystone`) can run; any other catalog step exits with code 1 and `"not implemented"`. A step whose catalog `roles` do not include the node's role (for example `time-sync-other` on the controller) exits with code 1. |
| `--node HOSTNAME` | Target node hostname. If omitted, matched against the local machine's hostname (`socket.gethostname()`). |
| `--root PATH` | Target filesystem root (default: `/`). Used by tests to confine all writes to a temporary directory. |
| `--yes` | Write changes, run the step's commands and update the state file. The commands of every step except `hosts-file` use the network through apt. Without `--yes`, `apply` runs in **dry-run** mode: it prints the redacted unified diff and the exact argv of every command the step would run, writes nothing, runs nothing, and exits 0. |

- **Catalog status warning:** `apply` prints a visible `WARNING` line when a step is marked
  `unverified` in `steps.json`, which all implemented steps are.
- **Dry run:** shows the file diff (or, when the file does not exist yet because its
  package is not installed, the block that will be appended), notes, then the mutating
  commands `apply()` would run in order, then the read-only commands `check()`, `apply()`
  (for its preconditions) and `verify()` may run. A dry run does not evaluate `check()`,
  because that would run commands.
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

## Secrets store and threat model (`openstack_forge.execution.secrets`)

**Nothing in this module has been run on a real host.** `SecretStore` manages 16 named
OpenStack passwords (`SECRET_SPECS` / `SECRET_NAMES`):

1. The 15 named variables from the "Passwords" table on
   <https://docs.openstack.org/install-guide/environment-security.html> (`ADMIN_PASS`,
   `CINDER_DBPASS`, `CINDER_PASS`, `DASH_DBPASS`, `DEMO_PASS`, `GLANCE_DBPASS`,
   `GLANCE_PASS`, `KEYSTONE_DBPASS`, `METADATA_SECRET`, `NEUTRON_DBPASS`, `NEUTRON_PASS`,
   `NOVA_DBPASS`, `NOVA_PASS`, `PLACEMENT_PASS`, `RABBIT_PASS`; row 1 of that table is
   "Database password (no variable used)", unused on Ubuntu where MariaDB's `root` uses
   `unix_socket`).
2. Explicit documented extras (`EXTRA_SECRET_SPECS`): `PLACEMENT_DBPASS` ("Database password
   for the Placement service"), which is omitted from the `environment-security.html` summary
   table but required by the Placement 2025.1 Ubuntu installation guide
   (<https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html>) in both the
   MariaDB `GRANT ... IDENTIFIED BY 'PLACEMENT_DBPASS'` statement and
   `/etc/placement/placement.conf` (`[placement_database] connection`).
3. **Mechanism for future secrets (`SecretSpec`):** secret names are never invented silently.
   When a future service installation guide uses a password variable absent from
   `environment-security.html` (for example `SWIFT_PASS` in the Object Storage guide), add a
   `SecretSpec(name, description, source_url)` entry to `EXTRA_SECRET_SPECS` (or pass
   `extra_specs` to `build_secret_specs()` / `SecretStore`). `SecretSpec` enforces an
   uppercase `^[A-Z][A-Z0-9_]*$` name, a non-empty description, and an official
   `https://docs.openstack.org/` source URL.

- **Where secrets live and permissions:** a single JSON file under the execution `root`,
  by default `<root>/etc/openstack-forge/secrets.json` (configurable with `--file`). The
  parent directory must have mode `0700` (or stricter: `mode & 0o077 == 0`) and the file
  must have mode `0600` (or stricter: `mode & 0o177 == 0`). Both must be owned by the
  current effective user (`euid`), and symlinks are refused on the file and every directory
  under `root`. `load()`, `get()` and `ensure()` refuse to read or write if any of those
  checks fails.
- **No `.orig` backup or leftover temporary file:** `SecretStore.ensure()` writes through
  `atomic_write_text()` without calling `backup_once()`, so `secrets.json.orig` is never
  created, and the `0600` temporary file `.secrets.json.*.tmp` is either atomically renamed
  over `secrets.json` or unlinked on error.
- **No-overwrite rule and idempotence:** `ensure()` generates only the missing names (with
  `secrets.choice` from `[A-Za-z0-9]`, 32 characters, ~190.5 bits of entropy) and keeps
  every existing value untouched. Once all 16 secrets exist, a second `ensure()` writes
  nothing (`wrote_file=False`). Operators may pre-populate their own values in the JSON
  file; `load()` and `ensure()` validate that every value is 32–256 ASCII alphanumeric
  characters (`[A-Za-z0-9]`) without ever echoing a rejected value in an error message.
- **Why `[A-Za-z0-9]{32,}`:** `.ini` files treat `#`, `;` and `$` specially; database and
  broker URLs (`mysql+pymysql://user:PASSWORD@controller/db`,
  `rabbit://openstack:PASSWORD@controller`) break on `:`, `@`, `/`, `?`, `#` and `%`; and
  `CommandSpec` parameter validation rejects leading `-` (option injection), whitespace and
  shell/punctuation metacharacters. Restricting secrets to `[A-Za-z0-9]` of length >= 32
  avoids escaping across all three layers.
- **CLI (`openstack-forge secrets`):**
  - `openstack-forge secrets init [--root PATH] [--file PATH]` creates missing secrets and
    prints **secret names only**, never values. Writing to `/` requires effective UID 0.
  - `openstack-forge secrets show NAME [--root PATH] [--file PATH]` prints the single named
    secret value to `stdout` and nothing else. It is the only command that outputs a secret.
- **Passing secrets to commands (`stdin_param`, `env_params`, `argv`) and the `ps` limitation:**
  - `CommandSpec` supports three explicit delivery modes for sensitive parameters:
    1. `stdin_param`: passes one sensitive parameter on standard input (`stdin=subprocess.PIPE`)
       instead of `argv`.
    2. `env_params`: maps `{param_name: ENV_VAR_NAME}` to pass sensitive parameters through
       environment variables added to `SANITIZED_ENV` instead of `argv` (validated against
       `^[A-Z][A-Z0-9_]*$` and forbidden from colliding with `SANITIZED_ENV` keys `PATH`,
       `LC_ALL`, `LANG`, `DEBIAN_FRONTEND`). On Linux, `/proc/<pid>/environ` is readable only
       by the same user and `root`, unlike `/proc/<pid>/cmdline` (`ps`) which is
       world-readable by default. Used by `keystone-manage.bootstrap`
       (`OS_BOOTSTRAP_PASSWORD`) and all 11 `openstack.*` commands (`OS_PASSWORD`).
    3. `argv` placeholder with `sensitive_params` / type `"secret"`: used only when an
       external tool offers no `stdin`, environment variable, or `0600` file option (for
       example `rabbitmqctl add_user` / `change_password` and the target service user's
       `--password {user_password}` in `openstack user create` / `openstack user set`).
  - When an external tool only accepts a password as a command-line argument in `argv` (for
    example `rabbitmqctl add_user openstack RABBIT_PASS` and
    `rabbitmqctl change_password openstack RABBIT_PASS`), any local user on the machine can
    read `/proc/<pid>/cmdline` (`ps`) while that child process is running. **OpenStack Forge
    cannot prevent this OS-level `ps` exposure when a tool leaves no `stdin`, environment, or
    file option**, although `CommandSpec(sensitive_params=...)` masks the argument in
    `build_shown_argv()`, dry runs, `PlannedCommand`, `CommandResult.argv`, the audit log and
    error messages.
- **Multi-node secret distribution is not implemented:** `SecretStore` is local to one root.
  Copying secrets to other nodes (such as `RABBIT_PASS`, `NOVA_PASS`, `NEUTRON_PASS`, or
  `METADATA_SECRET` on compute, block or object nodes) is **not** automated yet and must be
  performed manually by the operator while preserving `0700`/`0600` permissions and owner.

## Redaction

`redact_text()` masks secrets with `********` in two ways:

1. **Exact-value redaction (`register_secret` / `register_secrets`):** whenever
   `SecretStore.ensure()`, `load()` or `get()` runs, every secret value in the store is
   registered in memory (values shorter than 8 characters or containing whitespace/control
   characters are rejected so common words can never be masked by accident). `redact_text()`
   replaces any exact occurrence of a registered secret (longest first) with `********`,
   even when it appears bare without a `password=` key (for example inside a command
   argument, a log line or an error message).
2. **Pattern-based redaction:**
   - values of keys whose name contains `password`, `passwd`, `secret` or `token`
     (`key = value`, `key: value`, `"key": "value"`, `KEY=value`), quoted values up to the
     closing quote, unquoted values up to the end of the line;
   - the same keys as command-line flags (`--os-password VALUE`, `--token=VALUE`);
   - passwords in URLs (`mysql+pymysql://nova:********@controller/nova`).

`redact_data()` applies both to JSON-like structures (keys and values). Redaction is used
for every diff, every stored error, every `VerifyResult` detail, command output, the state
file and the audit log.

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
may start a process on behalf of a deployment step. It exists so that steps (for example
installing a package or restarting a service) have one reviewed path for doing so.
`DEFAULT_REGISTRY` contains exactly the thirty-one reviewed commands below, and a test pins
their argv, parameter types, exit codes and `mutates` flags, so adding or changing one is a
deliberate edit. The tests only ever run harmless `sys.executable` one-liners; everything
else uses a fake backend.

| Id | argv | Mutates | Network | Allowed exit codes | Justification |
| -- | ---- | ------- | ------- | ------------------ | ------------- |
| `apt-get.install` | `/usr/bin/apt-get install --yes {package}` | yes | yes (downloads packages) | 0 | apt(8) "SCRIPT USAGE": scripts should use `apt-get`, not `apt`. apt-get(8) `--yes`: "assume yes ... run non-interactively", still aborting on held, unauthenticated or essential packages. Timeout 900 s. |
| `apt-get.update` | `/usr/bin/apt-get update` | yes (`/var/lib/apt/lists`) | **yes** | 0 | apt-get(8): "update is used to resynchronize the package index files from their sources"; no option needed (it asks nothing); DIAGNOSTICS: 0 on success, 100 on error. Timeout 600 s. |
| `add-apt-repository.cloud-archive` | `/usr/bin/add-apt-repository --yes --cloud {pocket}` | yes | **yes** (it updates the lists) | 0 | add-apt-repository(1): `-y, --yes` "Assume yes to all queries" (non-interactive); `-C, --cloud` "Add an Ubuntu Cloud Archive ... cloud-archive:CANAME ... the pocket defaults to -updates" (the positional LINE form used by the guide is "deprecated"); `-n, --no-update` "After adding the repository, do not update the package cache", i.e. **it updates the package lists by default**, so no separate `apt-get update` follows it. Timeout 600 s. |
| `apt-cache.policy` | `/usr/bin/apt-cache policy {package}` | no | no | 0 | apt-cache(8): "does not manipulate the state of the system", works from the local lists "(e.g. offline)"; `policy pkg` prints the package's versions and the sources providing them. DIAGNOSTICS: 0 normally, 100 on error; an unknown package exits 0 with empty stdout and an `N:` notice on stderr. |
| `systemctl.restart` | `/usr/bin/systemctl restart {unit}` | yes | no | 0 | systemctl(1): stops and starts the unit, starting it if it was not running. Timeout 120 s. |
| `systemctl.is-active` | `/usr/bin/systemctl is-active {unit}` | no | no | 0-4 | systemctl(1): exit 0 if active, non-zero otherwise; the LSB codes 1-4 all mean "not active", and the man page advises reading the printed state, so callers also require the output `active`. |
| `systemctl.enable` | `/usr/bin/systemctl enable {unit}` | yes | no | 0 | systemctl(1): enable "will create a set of symlinks, as encoded in the [Install] sections", reloads the manager configuration, and does "not have the effect of also starting" the unit. Needed because the etcd page runs `systemctl enable etcd`. Timeout 60 s. |
| `systemctl.is-enabled` | `/usr/bin/systemctl is-enabled {unit}` | no | no | 0-4 | systemctl(1): exit 0 "if at least one is enabled, non-zero otherwise", and prints the status; its table gives 0 for several statuses (`static`, `alias`, `indirect`, ...), so callers require the output `enabled`. Added so that `check()` of `etcd` does not report a configured but disabled unit as done. |
| `dpkg-query.status` | `/usr/bin/dpkg-query --status {package}` | no | no | 0, 1 | dpkg-query(1): `--status` shows the status database entry; exit 1 means the package was not found, 2 is fatal. |
| `rabbitmqctl.add-user` | `/usr/sbin/rabbitmqctl add_user openstack {password}` | yes | no | 0 | Guide (`environment-messaging-ubuntu.html`): `rabbitmqctl add_user openstack RABBIT_PASS`; `rabbitmqctl(8)`: `add_user username password` (`password` is type `secret`, masked everywhere in output/logs). Timeout 60 s. |
| `rabbitmqctl.change-password` | `/usr/sbin/rabbitmqctl change_password openstack {password}` | yes | no | 0 | `rabbitmqctl(8)`: `change_password username newpassword`. Used by `rabbitmq` `apply()` when `list_users` shows `openstack` already exists (`add_user` fails non-zero on an existing user). Timeout 60 s. |
| `rabbitmqctl.set-permissions` | `/usr/sbin/rabbitmqctl set_permissions openstack .* .* .*` | yes | no | 0 | Guide (`environment-messaging-ubuntu.html`): `rabbitmqctl set_permissions openstack ".*" ".*" ".*"`; `rabbitmqctl(8)`: `set_permissions [-p vhost] user conf write read` (`-p vhost` defaults to `/`). The `".*"` regexes contain `*` and are fixed literals in `argv`. Timeout 60 s. |
| `rabbitmqctl.list-users` | `/usr/sbin/rabbitmqctl list_users` | no | no | 0 | `rabbitmqctl(8)`: `list_users` lists user names and tags; used by `check()`, `apply()`, and `verify()`. Timeout 30 s. |
| `rabbitmqctl.list-user-permissions` | `/usr/sbin/rabbitmqctl list_user_permissions openstack` | no | no | 0 | `rabbitmqctl(8)`: `list_user_permissions username` lists vhost, configure, write, and read permissions; used by `check()` and `verify()`. Timeout 30 s. |
| `mariadb.execute-sql` | `/usr/bin/mariadb --batch --skip-column-names` (`stdin_param="sql"`) | yes | no | 0 | `mariadb(1)` (`mariadb-client-core`, where `/usr/bin/mariadb` is the ELF binary and `/usr/bin/mysql -> mariadb` is a symlink): reads SQL statements (`CREATE DATABASE IF NOT EXISTS`, `GRANT ALL PRIVILEGES ... IDENTIFIED BY '<password>'`, `FLUSH PRIVILEGES`) from `stdin` (`sql_stdin`) so database passwords never appear in `argv` or `/proc/<pid>/cmdline` (`ps`). Timeout 60 s. |
| `mariadb.query-sql` | `/usr/bin/mariadb --batch --skip-column-names --execute "SELECT 'DB', SCHEMA_NAME, '', '' FROM information_schema.SCHEMATA UNION ALL SELECT 'GRANT', User, Host, Db FROM mysql.db;"` | no | no | 0 | `mariadb(1)`: fixed literal read-only `SELECT` over `information_schema.SCHEMATA` and `mysql.db` with `params={}` (`mutates=False`), reusable by `check()` and `verify()` of all controller services without passing any parameter or touching `stdin`. Timeout 30 s. |
| `keystone-manage.db-sync` | `/usr/bin/keystone-manage db_sync` | yes | no | 0 | Guide (`keystone-install-ubuntu.html`): `keystone-manage db_sync` (`dpkg -S`: `python3-keystone: /usr/bin/keystone-manage`). Invoked directly as `root` instead of `su -s /bin/sh -c ...` because `CommandSpec` forbids `su`. Timeout 300 s. |
| `keystone-manage.fernet-setup` | `/usr/bin/keystone-manage fernet_setup --keystone-user keystone --keystone-group keystone` | yes | no | 0 | Guide (`keystone-install-ubuntu.html`): initializes `/etc/keystone/fernet-keys` owned by `keystone:keystone` (`FernetUtils.initialize_key_repository` is idempotent if key `0` exists). Timeout 60 s. |
| `keystone-manage.credential-setup` | `/usr/bin/keystone-manage credential_setup --keystone-user keystone --keystone-group keystone` | yes | no | 0 | Guide (`keystone-install-ubuntu.html`): initializes `/etc/keystone/credential-keys` owned by `keystone:keystone`. Timeout 60 s. |
| `keystone-manage.bootstrap` | `/usr/bin/keystone-manage bootstrap --bootstrap-admin-url http://controller:5000/v3/ --bootstrap-internal-url http://controller:5000/v3/ --bootstrap-public-url http://controller:5000/v3/ --bootstrap-region-id RegionOne` (`env_params={"password": "OS_BOOTSTRAP_PASSWORD"}`) | yes | no | 0 | Guide (`keystone-install-ubuntu.html`) + `/usr/lib/python3/dist-packages/keystone/cmd/cli.py`: `BootStrap.do_bootstrap()` reads `os.environ.get('OS_BOOTSTRAP_PASSWORD') or CONF.command.bootstrap_password`, so `ADMIN_PASS` is delivered via `OS_BOOTSTRAP_PASSWORD` in `/proc/<pid>/environ` (never `argv`). Timeout 120 s. |
| `openstack.project-list` | `/usr/bin/openstack ... project list --domain default -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | no | no (local controller `http://controller:5000/v3`) | 0 | `python3-openstackclient`: lists projects in the `default` domain as JSON (`ID`, `Name`). Timeout 30 s. |
| `openstack.project-create-service` | `/usr/bin/openstack ... project create --domain default --description "Service Project" service -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | Guide (`keystone-users-ubuntu.html`): `openstack project create --domain default --description "Service Project" service`. Timeout 60 s. |
| `openstack.user-list` | `/usr/bin/openstack ... user list --domain default -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | no | no (local controller) | 0 | `python3-openstackclient`: lists users in the `default` domain as JSON (`ID`, `Name`). Timeout 30 s. |
| `openstack.user-create` | `/usr/bin/openstack ... user create --domain default --password {user_password} {username} -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | Guide (`glance`/`placement`/`nova`/`neutron`/`cinder` install pages): `openstack user create --domain default --password-prompt <user>` (`--password-prompt` requires `stdin.isatty()`, so `--password {user_password}` is used non-interactively). Timeout 60 s. |
| `openstack.user-set-password` | `/usr/bin/openstack ... user set --domain default --password {user_password} {username}` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | `python3-openstackclient` (`SetUser`): converges the service user's password when `apply()` runs and the user already exists. Timeout 60 s. |
| `openstack.role-assignment-list` | `/usr/bin/openstack ... role assignment list --user {username} --user-domain default --project {project} --project-domain default --names -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | no | no (local controller) | 0 | `python3-openstackclient` (`ListRoleAssignment`): lists role assignments (`Role`, `User`, `Project`) as JSON. Timeout 30 s. |
| `openstack.role-add-user-project` | `/usr/bin/openstack ... role add --project {project} --project-domain default --user {username} --user-domain default {role}` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | Guide (`glance`/`placement`/`nova`/`neutron`/`cinder` install pages): `openstack role add --project service --user <user> admin` (idempotent in Keystone v3 `PUT`, checked first via `role-assignment-list`). Timeout 60 s. |
| `openstack.service-list` | `/usr/bin/openstack ... service list -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | no | no (local controller) | 0 | `python3-openstackclient`: lists catalog services (`ID`, `Name`, `Type`) as JSON. Timeout 30 s. |
| `openstack.service-create` | `/usr/bin/openstack ... service create --name {service_name} {service_type} -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | Guide (`glance`/`placement`/`nova`/`neutron`/`cinder` install pages): `openstack service create --name <name> --description ... <type>` (`--description` omitted so multi-word prose never flows through parameter validation; checked first via `service-list` to avoid duplicate UUIDs). Timeout 60 s. |
| `openstack.endpoint-list` | `/usr/bin/openstack ... endpoint list -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | no | no (local controller) | 0 | `python3-openstackclient`: lists catalog endpoints (`ID`, `Region`, `Service Name`, `Service Type`, `Enabled`, `Interface`, `URL`) as JSON. Timeout 30 s. |
| `openstack.endpoint-create` | `/usr/bin/openstack ... endpoint create --region RegionOne {service_type} {interface} {url} -f json` (`env_params={"admin_password": "OS_PASSWORD"}`) | yes | no (local controller) | 0 | Guide (`glance`/`placement`/`nova`/`neutron`/`cinder` install pages): `openstack endpoint create --region RegionOne <type> public|internal|admin <url>` (checked first via `endpoint-list` to avoid duplicate UUIDs). Timeout 60 s. |

Each path is the one installed on Ubuntu 24.04 by the package named by `dpkg -S`: `apt`
(`apt-get`, `apt-cache`), `software-properties-common` (`add-apt-repository`), `systemd`,
`dpkg`, `rabbitmq-server` (`/usr/sbin/rabbitmqctl`), `mariadb-client-core`
(`/usr/bin/mariadb`), `python3-keystone` (`/usr/bin/keystone-manage`), and
`python3-openstackclient` (`/usr/bin/openstack`, installed via `update-alternatives` from
`/usr/bin/python3-openstack`); `PATH` is never searched. No other
apt or dpkg option is used: in particular no `--force-*`, no `-o Dpkg::Options`, no lock
timeout, and no `upgrade`, `dist-upgrade` or `reboot` command exists in the registry. A
configuration file prompt from dpkg therefore makes the command fail (stdin is `/dev/null`)
instead of being answered automatically.

### Allowlist, not command strings

A command is a `CommandSpec`:

| Field | Meaning |
| ----- | ------- |
| `id` | Stable name, e.g. `chrony.restart`. |
| `argv` | Template tuple. `argv[0]` must be a literal **absolute path** (no `PATH` lookup). Every other element is a literal or exactly `{name}`. |
| `params` | `{name: type}` for every placeholder; each parameter not in `stdin_param` or `env_params` appears in `argv` exactly once. |
| `timeout` | Seconds; required to be positive. |
| `allowed_exit_codes` | Exit codes that count as success for the caller (default `{0}`). |
| `mutates` | Whether the command changes the system. |
| `sensitive_params` | Parameter names whose real values are sent to the process while `build_shown_argv()`, `build_shown_env()`, `PlannedCommand`, dry runs, `CommandResult.argv`, the audit log and error messages show `********` (any parameter of type `secret` is included automatically). |
| `stdin_param` | Optional name of one sensitive parameter delivered on `stdin` (`stdin=subprocess.PIPE`) instead of `argv`; must not appear in `argv`. |
| `env_params` | Optional mapping `{param_name: ENV_VAR_NAME}` of sensitive parameters delivered via environment variables added to `SANITIZED_ENV` instead of `argv`; variable names must match `^[A-Z][A-Z0-9_]*$` and must not collide with `SANITIZED_ENV`. |

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
| `secret` | `[A-Za-z0-9]{32,256}`, marked sensitive automatically |
| `sql_stdin` | `[\x20-\x7e\n]{1,4096}` (printable ASCII plus LF only; no CR, NUL or tabs), **permitted only when declared as `stdin_param`** (never in `argv` or `env_params`), marked sensitive automatically |
| `openstack_name` | `[a-z][a-z0-9_-]{0,63}`, e.g. `service`, `glance`, `compute`, `volumev3` |
| `endpoint_interface` | `public`, `internal`, or `admin` |
| `endpoint_url` | `http://controller:<port>[/<path>]` where `<port>` is 1–65535 and optional `/<path>` uses `[A-Za-z0-9._/-]` with no `..` or `//` (also permits the literal `%\(tenant_id\)s` token for Cinder v3 if needed) |

Missing or unexpected parameters are errors too. For sensitive parameters (including types
`secret` and `sql_stdin`), `ParameterError` masks the rejected value as `'********'`. There
is no free-text `argv` type.

### Running a command

`CommandExecutor(registry, root=..., audit_log=..., backend=..., dry_run=...)`:

- **No shell.** The default `SubprocessBackend` passes the argv list to `subprocess.Popen`
  with `shell=False`, `stdin=subprocess.DEVNULL` by default (or `stdin=subprocess.PIPE`
  only for specs that opt into `stdin_param`), `cwd="/"`, and a new session; the backend is
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
  is running (wait; do not delete lock files). `apt-get.update` and
  `add-apt-repository.cloud-archive` point at network reachability of the apt sources and
  the same lock advice. No recovery is attempted automatically.
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

- Registered secrets (loaded from `SecretStore` or passed as `sensitive_params`) are masked
  by exact value anywhere they appear, and telltale key/flag/URL patterns are masked even
  without registration; a secret that was neither registered nor next to a telltale key
  cannot be recognised.
- When an external tool only accepts a password in `argv`, local users on the host can see
  it in `/proc/<pid>/cmdline` (`ps`) for the duration of the command.
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
| `apply()` | `apt-get update` (a fresh image may have stale or empty package lists; this uses the network); `apt-get install --yes chrony`; then the file edit; then `systemctl restart chrony`. |
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

- `apt-get update` before the install is not on the page; it is there because a freshly
  installed image may have stale or empty package lists.
- The page restarts with `service chrony restart`; this step uses `systemctl restart chrony`.
- The page says to "add, change, or remove" keys as necessary; commenting out the existing
  sources when servers are configured is this project's reading of it.
- `sourcedir` directives (for example servers learned from DHCP) are not touched.
- **Time synchronization itself is not checked**, because the guide's NTP verification page
  has not been reviewed yet; `verify()` says so in its details.

## Step: `time-sync-other` (`openstack_forge.execution.chrony`)

**Unverified, and never run on a real host.** It follows the official page for the other
nodes, <https://docs.openstack.org/install-guide/environment-ntp-other.html>, which for
Ubuntu says: install `chrony`; in `/etc/chrony/chrony.conf`, "comment out or remove all but
one `server` key" and change it to reference the controller node (`server controller
iburst`); comment out the `pool 2.debian.pool.ntp.org offline iburst` line; restart the
service with `service chrony restart`. It runs on `compute`, `block` and `object` nodes;
constructing it for the controller raises `ExecutionError` (and `apply` refuses the
controller earlier, because the catalog roles do not include it).

It uses only commands already in the registry (`apt-get.update`, `apt-get.install`,
`systemctl.restart`, `systemctl.is-active`, `dpkg-query.status`); none was added.

| Phase | What happens |
| ----- | ------------ |
| `check()` | True only if `chrony.conf` needs no edit (pure) **and** `dpkg-query --status chrony` reports `install ok installed`. The file is checked first; a missing file means not done, without running anything. |
| `apply()` | `apt-get update` (network); `apt-get install --yes chrony` (network); then the file edit; then `systemctl restart chrony`. |
| `verify()` | Pure check that the managed block is intact and holds exactly `server controller iburst`, and that no active `pool`/`server` line is left outside it, plus `systemctl is-active chrony` exiting 0 and printing `active`. The details always end with the sentence saying that actual time synchronization is not checked (`TIME_SYNC_NOT_CHECKED`). |

The file edit, in `chrony.conf` only (it must already exist: it is installed by the
package, and the step never creates it):

- every active `pool` and `server` line outside `# BEGIN openstack-forge time-sync` /
  `# END openstack-forge time-sync` is commented out with the `# openstack-forge disabled: `
  prefix; this covers the guide's `pool 2.debian.pool.ntp.org offline iburst` line and any
  other default source;
- the block contains the single line `server controller iburst`. `controller` is the
  alias that the `hosts-file` step writes to `/etc/hosts` for the controller's management
  address; this step does not check `/etc/hosts` itself, but in the catalog it depends on
  the node's own `hosts-file` (and on the controller's `time-sync-controller`).

Re-running writes nothing: lines already commented out are skipped and the block's own
line is never commented out. A source line added later outside the block is commented out
on the next run.

Deviations from the page, and limits:

- `apt-get update` before the install is not on the page (stale or empty package lists on
  a fresh image).
- `systemctl restart chrony` instead of `service chrony restart`.
- The page allows commenting out *or removing* sources; this step only comments them out,
  so `uncomment_lines()` or `chrony.conf.orig` can restore them.
- Only `pool` and `server` lines are handled; `sourcedir`, `confdir` and `include`
  directives, which may add other time sources, are left unchanged.
- **Time synchronization itself is not checked** (the guide's verification page has not
  been reviewed); `verify()` says so.

## Step: `openstack-packages` (`openstack_forge.execution.openstack_packages`)

**Unverified, and never run on a real host.** It follows
<https://docs.openstack.org/install-guide/environment-packages-ubuntu.html>, which for
OpenStack 2025.1 (Epoxy) on Ubuntu 24.04 says to run `add-apt-repository
cloud-archive:epoxy` "on all nodes that run OpenStack services", then
`apt install python3-openstackclient`. The step runs on every role.

The pocket is never written in the step: it is the `cloud_archive` of the configuration's
entry in the supported release table (`openstack_forge.config.releases`), validated as a
`cloud_archive_pocket` parameter.

| Phase | What happens |
| ----- | ------------ |
| `check()` | True only if, in order: `dpkg-query --status python3-openstackclient` reports `install ok installed`; `apt-cache policy python3-openstackclient` lists an apt source whose suite is `<series>-updates/<name>` for the pocket (for example `noble-updates/epoxy`); **and** the `Installed:` version reported by `apt-cache policy` is the version listed under one of those pocket sources. The package alone is not enough (Ubuntu 24.04's own archive ships `python3-openstackclient` too, 2024.1), and neither is an enabled pocket: a client installed from `noble/main` before the pocket was enabled, or left there because `apply()` stopped after `add-apt-repository`, stays at Ubuntu's version. |
| `apply()` | Precondition: `dpkg-query --status software-properties-common` must report it installed (it provides `/usr/bin/add-apt-repository`); otherwise `PreconditionError` names the package, and nothing is installed. Then `add-apt-repository --yes --cloud <pocket>` (which updates the package lists itself), then `apt-get install --yes python3-openstackclient`. For an already-installed client, apt-get(8) `install` installs the newest candidate of that package only (and its dependencies), which replaces a version from Ubuntu's archive with the pocket's. |
| `verify()` | The same three checks, each reported separately in the details: "is not installed", "is not enabled", or, when both hold but the version does not match, `the installed python3-openstackclient <installed> does not come from the repository <pocket>, which offers <version> (<suite>/<component>)`. On success the details name the installed version and its pocket source. The details always end with a sentence saying the repository check is unconfirmed on a real host. |

No `apt-get update` is run by this step, because `add-apt-repository` updates the package
lists unless given `--no-update` (add-apt-repository(1)). No upgrade, dist-upgrade or
reboot is run, and no other package is requested (apt still installs the client's
dependencies). `add-apt-repository` and `apt-get` use the network; the Python code does
not. `--yes` with a `--root` other than `/` is refused, as for every step that declares
commands.

How the repository is detected: `apt-cache policy` was run read-only on an Ubuntu 24.04
development machine to see the real output format. Each source line looks like
`500 http://ubuntu-cloud.archive.canonical.com/ubuntu noble-updates/epoxy/main amd64 Packages`;
only the suite (`noble-updates/epoxy`) is compared, not the URI, so mirrors count. The
test fixtures in `tests/fixtures/apt/` are synthetic, modeled on that output.

Deviations from the page, and limits:

- The page uses the positional form `add-apt-repository cloud-archive:epoxy`; the step
  uses `--cloud cloud-archive:epoxy` because the man page calls the positional LINE form
  deprecated and ambiguous, and `--yes` because stdin is `/dev/null`.
- **The Ubuntu Cloud Archive suite naming (`<series>-<pocket>/<name>`) is an assumption
  not yet confirmed on a host where this step ran**; `verify()` says so.
- Any Ubuntu series name is accepted in the suite, and source priorities are ignored.
- Versions are compared as exact strings. If something keeps apt from choosing the
  pocket's version (for example an apt pin), `apt-get install` is expected to leave the
  installed version as it is and `verify()` then fails with the installed and pocket
  versions (modeled with a fake backend, not observed on a host); the step does not touch
  apt preferences.
- `add-apt-repository` is run even if the pocket is already configured (for its update);
  that re-running it is harmless is assumed, not confirmed.
- The step does not install `software-properties-common`; the operator must.

## Step: `mariadb` (`openstack_forge.execution.mariadb`)

**Unverified, and never run on a real host.** It follows the official page,
<https://docs.openstack.org/install-guide/environment-sql-database-ubuntu.html>, which for
Ubuntu 20.04 and later says to install `mariadb-server` and `python3-pymysql`, create
`/etc/mysql/mariadb.conf.d/99-openstack.cnf` with a `[mysqld]` section, and restart the
database service. It runs only on the controller (constructing it for another role raises
`ExecutionError`), and uses only the existing `apt-get.install`, `systemctl.restart`,
`systemctl.is-active` and `dpkg-query.status` commands.

The file gets exactly these keys, written with `ini_set_many` in one edit:

```ini
[mysqld]
bind-address = <management_ip of the controller>
default-storage-engine = innodb
innodb_file_per_table = on
max_connections = 4096
collation-server = utf8_general_ci
character-set-server = utf8
```

| Phase | What happens |
| ----- | ------------ |
| `check()` | Pure first: if `/etc/mysql/mariadb.conf.d` or the file is missing, or the edit would change it, `False` without running any command. Otherwise `True` only if `dpkg-query --status` reports both `mariadb-server` and `python3-pymysql` as `install ok installed`. |
| `apply()` | `apt-get install --yes mariadb-server`; `apt-get install --yes python3-pymysql`; the file edit; `systemctl restart mariadb`. |
| `verify()` | Pure check that the file exists and that in `[mysqld]` each key has its expected active value, with no other active line for that key with a different value; `dpkg-query --status python3-pymysql` (checking only the service would miss a missing Python driver); `systemctl is-active mariadb` printing `active`. |

The details of `verify()` always say that **SQL connectivity and TCP listening on
`<management_ip>:3306` are not checked, and `mysql_secure_installation` is not run.**

Decisions and limits:

- **No `mysql_secure_installation`.** The guide runs it to set a root password. It is
  interactive (commands run with stdin from `/dev/null`), and the same page notes that on
  Ubuntu MariaDB uses the `unix_socket` authentication plugin, so local `root` access
  needs no password. The step therefore does not run it and sets no database password.
- **Unit name `mariadb`.** On Ubuntu 24.04 `mariadb-server` ships
  `/usr/lib/systemd/system/mariadb.service` and `mysql.service` as a symlink to it
  (checked with `dpkg -S` on a development machine). The guide restarts `mysql`; the step
  uses the canonical `mariadb`, which is the same unit.
- **Directory.** `/etc/mysql/mariadb.conf.d` comes with the MariaDB packages (on the
  development machine `dpkg -S` lists `mariadb-common`, `mariadb-server`, `mariadb-client`
  and others for it). The step never creates it; if it is still missing after
  `apt-get install`, `apply()` stops with a `PreconditionError` before restarting anything.
  A dry run on a host without it shows the exact content that will be written.
- **No package list update.** On the controller the step runs after `time-sync-controller`
  and `openstack-packages`, which already refreshed the package lists. Run alone with
  `apply --step mariadb` on a host whose package lists are empty, `apt-get install` may
  fail until they are updated.
- **Universe repository.** The guide notes that since Ubuntu 18.04 `mariadb-server` is not
  in the default repository and Universe must be enabled. The step enables no repository;
  whether this still applies to Ubuntu 24.04 has not been confirmed.
- **Other option files are not inspected.** MariaDB also reads the other files in
  `mariadb.conf.d` and its other option files; a file read after `99-openstack.cnf` could
  override these keys. Keys are compared by exact name (MariaDB may also accept `_` for
  `-` in option names; this is not relied on), and values exactly (`ON` is not `on`).

## Security of the controller services

`memcached` and `etcd`, configured as on the official pages, have **no authentication and
no encryption** and listen on the controller's management IP (etcd's peer port listens on
all interfaces, as on its page). `rabbitmq` authenticates with the `openstack` account and
`RABBIT_PASS`, but as on the official page it listens for AMQP on TCP port 5672 **without
TLS**. Anyone who can reach the management network can read and change Memcached/etcd data
or observe unencrypted AMQP traffic. The management network must therefore be a trusted,
isolated network; the Memcached page itself recommends firewalling, authentication and
encryption for production. OpenStack Forge adds no firewall rule and changes no firewall.

## Step: `rabbitmq` (`openstack_forge.execution.rabbitmq`)

**Unverified, and never run on a real host.** Source:
<https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html>, which for
Ubuntu says:

1. `apt install rabbitmq-server`;
2. `rabbitmqctl add_user openstack RABBIT_PASS` ("Replace `RABBIT_PASS` with a suitable
   password");
3. `rabbitmqctl set_permissions openstack ".*" ".*" ".*"` ("Permit configuration, write,
   and read access for the `openstack` user").

It runs only on the controller (constructing it for another role raises `ExecutionError`).

| Phase | What happens |
| ----- | ------------ |
| `check()` | Pure first: `RABBIT_PASS` must exist in `SecretStore(root)` (`<root>/etc/openstack-forge/secrets.json`); if the store is missing, unreadable, overly permissive or lacks `RABBIT_PASS`, `False` without running any command. Otherwise `True` only if, in order: `dpkg-query --status rabbitmq-server` reports it installed, `systemctl is-active rabbitmq-server` prints `active`, `rabbitmqctl list_users` lists `openstack`, and `rabbitmqctl list_user_permissions openstack` shows `.* .* .*` in vhost `/`. |
| `apply()` | Precondition: `SecretStore(root).get("RABBIT_PASS")` must succeed; otherwise `PreconditionError` tells the operator to run `openstack-forge secrets init` first (the step never generates secrets itself). Then `apt-get install --yes rabbitmq-server`; then `rabbitmqctl list_users`; if `openstack` does not exist yet it runs `rabbitmqctl add_user openstack {password}`, whereas if `openstack` already exists it converges the password with `rabbitmqctl change_password openstack {password}`; then `rabbitmqctl set_permissions openstack .* .* .*`. |
| `verify()` | Checks that `RABBIT_PASS` exists in `SecretStore(root)`, `rabbitmq-server` is installed, `systemctl is-active rabbitmq-server` is `active`, `rabbitmqctl list_users` includes `openstack`, and `rabbitmqctl list_user_permissions openstack` has `.* .* .*` in vhost `/`. |

Established on an Ubuntu 24.04 development machine with read-only commands (`rabbitmq-server`
`3.12.1-1ubuntu1.2` in `noble-updates/main` and `noble-security/main`):

- `/usr/sbin/rabbitmqctl` (`dpkg -S`: `rabbitmq-server`) is a symlink to
  `../lib/rabbitmq/bin/rabbitmq-script-wrapper`; when invoked as `root`, that wrapper runs
  `su rabbitmq -s /bin/sh -c "/usr/lib/rabbitmq/bin/rabbitmqctl ${CMDLINE}"`;
- `/usr/lib/systemd/system/rabbitmq-server.service` runs
  `ExecStart=/usr/lib/rabbitmq/bin/rabbitmq-server` as `User=rabbitmq`, and
  `/var/lib/dpkg/info/rabbitmq-server.postinst` creates `/var/lib/rabbitmq/.erlang.cookie`
  (mode `0400`), installs `/etc/rabbitmq/rabbitmq-env.conf` (default `#NODENAME=rabbit`),
  and enables and starts `rabbitmq-server.service` on package `configure`;
- `rabbitmqctl(8)` documents `add_user username password`,
  `change_password username newpassword`, `set_permissions [-p vhost] user conf write read`
  (default vhost `/`), `list_users`, and `list_user_permissions username`.

Deviations, trade-offs, and limits:

- **Idempotence and `change_password` trade-off:** `rabbitmqctl add_user` fails non-zero if
  `openstack` already exists, and `rabbitmqctl list_users` returns user names and tags only
  (never password hashes). In `apply()`, when `check()` returned `False` and `list_users`
  shows that `openstack` already exists (for example after an earlier partial run where
  `add_user` succeeded before `set_permissions`, or after `RABBIT_PASS` was updated in
  `secrets.json`), the step runs `rabbitmqctl change_password openstack {password}` before
  `set_permissions`. Trade-off: converging with `change_password` guarantees the broker's
  `openstack` password matches `RABBIT_PASS` in `secrets.json` rather than leaving a stale
  password or failing on a rerun, at the cost of one mutating `rabbitmqctl` call whenever
  `apply()` runs on a host where `openstack` already exists.
- **Password delivery and `/proc/<pid>/cmdline` (`ps`) exposure:** `rabbitmqctl(8)` on
  Ubuntu 24.04 (`3.12.1-1ubuntu1.2`) documents only positional `argv` (`add_user username
  password` and `change_password username newpassword`) — neither `--stdin` nor an
  environment variable is documented, and `rabbitmq-script-wrapper` interpolates
  `${CMDLINE}` into `su rabbitmq -s /bin/sh -c ...`. OpenStack Forge masks `{password}` as
  `********` in `build_shown_argv()`, `PlannedCommand`, dry-run output, `CommandResult.argv`,
  the audit log and error messages, and registers `RABBIT_PASS` with `register_secrets()`,
  but **local users on the host can read `RABBIT_PASS` from `/proc/<pid>/cmdline` (`ps`) for
  the duration of `rabbitmqctl add_user` or `change_password`**.
- **Permission regexes (`".*"`):** `*` is forbidden in command parameter values, so the
  three `".*"` arguments in `rabbitmqctl.set-permissions` are fixed literals in the
  `CommandSpec.argv` tuple, never parameters.
- **Hostname resolution (`hosts-file`):** RabbitMQ names its local Erlang node
  `rabbit@<short-hostname>` by default; if the short hostname does not resolve in
  `/etc/hosts`, `rabbitmq-server.service` and `rabbitmqctl` fail. Both dry-run notes,
  `verify()` details and `RABBITMQCTL_FAILURE_HINT` point to the `hosts-file` step (this
  step never edits `/etc/hosts`).
- **What `verify()` does not check:** no AMQP login test with `RABBIT_PASS` and no TCP
  check on port 5672 are performed, and the exact tabular output format of `rabbitmqctl
  list_users` / `list_user_permissions` is an assumption from `rabbitmqctl(8)` not confirmed
  on a live broker.

## Step: `memcached` (`openstack_forge.execution.memcached`)

**Unverified, and never run on a real host.** Source:
<https://docs.openstack.org/install-guide/environment-memcached-ubuntu.html>, which for
Ubuntu 18.04 and newer says `apt install memcached python3-memcache`, edit
`/etc/memcached.conf` to use the controller's management IP with `-l 10.0.0.11` ("Change
the existing line that had -l 127.0.0.1"), then `service memcached restart`.

| Phase | What happens |
| ----- | ------------ |
| `check()` | Pure first: if `/etc/memcached.conf` is missing or the edit would change it, `False` without any command. Otherwise `True` only if `dpkg-query` reports both `memcached` and `python3-memcache` installed. |
| `apply()` | `apt-get install --yes memcached`; `apt-get install --yes python3-memcache`; the file edit; `systemctl restart memcached`. |
| `verify()` | Pure check that the managed block contains exactly `-l <management_ip>` and no active `-l`/`--listen` line is left outside it; `python3-memcache` installed; `systemctl is-active memcached` printing `active`. |

The edit comments out every active `-l` / `--listen` line outside the managed block
`openstack-forge memcached` and puts `-l <management_ip>` in it, in one atomic edit
(`disable_lines_and_set_block`); other lines (including `--listen-backlog` and comments)
are untouched. memcached(1) says addresses may be given "by using -l multiple times", so
the old line has to be disabled rather than left next to the new one.

Established on an Ubuntu 24.04 development machine with read-only commands (`memcached`
`1.6.24-1ubuntu0.3` and `python3-memcache` in `noble/main`):

- `/usr/lib/systemd/system/memcached.service` (`dpkg -S`: `memcached`) runs
  `ExecStart=/usr/share/memcached/scripts/systemd-memcached-wrapper /etc/memcached.conf`,
  and `/usr/share/memcached/scripts/systemd-memcached-wrapper` reads `/etc/memcached.conf`,
  strips comments and whitespace, ignores `-d`, `-h` and `logfile`, and passes every
  remaining line as an argument to `/usr/bin/memcached`;
- `/etc/memcached.conf` is not in dpkg's conffile database: `/var/lib/dpkg/info/memcached.postinst`
  creates it on `configure` by copying `/usr/share/memcached/memcached.conf.default` if
  `/etc/memcached.conf` does not exist yet;
- `/usr/share/memcached/memcached.conf.default` (52 lines, MD5
  `feb97c5e3f225f42e852f1c568cc8ad7`) contains **two** default listen lines (lines 35–36),
  `-l 127.0.0.1` and `-l ::1` (whereas the OpenStack page mentions only `-l 127.0.0.1`; on
  the development machine `/etc/memcached.conf` had line 35 already edited to the machine's
  local IP — sanitized to the RFC 5737 documentation address `-l 192.0.2.170` for the
  repository — and line 36 still at `-l ::1`). Disabling every active `-l` /
  `--listen` line outside the managed block comments out both `-l 127.0.0.1` and `-l ::1`.

Deviations: two `apt-get install` commands instead of one `apt install`, and
`systemctl restart` instead of `service ... restart`.

`verify()` details always say that no TCP connection to `<management_ip>:11211` is
attempted, and what memcached actually listens on at runtime is not checked (unconfirmed on
a live service).

## Step: `etcd` (`openstack_forge.execution.etcd`)

**Unverified, and never run on a real host.** Source:
<https://docs.openstack.org/install-guide/environment-etcd-ubuntu.html>, which says
`apt install etcd` (needing the Universe repository since Ubuntu 18.04) and, "as for Ubuntu
24.04", `apt install etcd-server`; then nine keys in `/etc/default/etcd`; then
`systemctl enable etcd` and `systemctl restart etcd`.

```sh
ETCD_NAME="controller"
ETCD_DATA_DIR="/var/lib/etcd"
ETCD_INITIAL_CLUSTER_STATE="new"
ETCD_INITIAL_CLUSTER_TOKEN="etcd-cluster-01"
ETCD_INITIAL_CLUSTER="controller=http://<management_ip>:2380"
ETCD_INITIAL_ADVERTISE_PEER_URLS="http://<management_ip>:2380"
ETCD_ADVERTISE_CLIENT_URLS="http://<management_ip>:2379"
ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380"
ETCD_LISTEN_CLIENT_URLS="http://<management_ip>:2379"
```

`<management_ip>` is the controller's `management_ip` and `controller` is the alias the
`hosts-file` step writes to `/etc/hosts`; the data directory, token, state and ports are the
page's literal values. It is a single-node cluster, as on the page.

| Phase | What happens |
| ----- | ------------ |
| `check()` | Pure first: if `/etc/default/etcd` is missing or the edit would change it, `False` without any command. Otherwise `True` only if `etcd-server` is installed and `systemctl is-enabled etcd` prints `enabled`. |
| `apply()` | Precondition: `apt-cache policy etcd-server` must report a candidate (`policy.candidate is not None`); otherwise `PreconditionError` explains that on Ubuntu 24.04 `etcd-server` is in the `universe` repository (probably disabled) and enables no repository. Then `apt-get install --yes etcd-server`; the file edit; `systemctl enable etcd`; `systemctl restart etcd`. |
| `verify()` | Pure check that the managed block has exactly the nine values and no other active assignment of those keys exists in the file; `systemctl is-enabled etcd` printing `enabled`; `systemctl is-active etcd` printing `active`. |

The edit comments out active assignments (`KEY=` or `export KEY=`) of the nine keys outside
the managed block `openstack-forge etcd` and writes the block, in one atomic edit; other
`ETCD_*` variables and comments are kept.

Established on an Ubuntu 24.04 development machine with read-only commands (`etcd-server`
`3.4.30-1ubuntu0.24.04.3` in `noble-updates/universe`, no candidate for `etcd`):

- `/usr/lib/systemd/system/etcd.service` (`dpkg -S`: `etcd-server`) sets
  `Environment=DAEMON_ARGS=`, `Environment=ETCD_NAME=%H` and
  `Environment=ETCD_DATA_DIR=/var/lib/etcd/default`, then loads
  `EnvironmentFile=-/etc/default/%p` (`/etc/default/etcd`, overriding the unit's
  `Environment=` settings) before `ExecStart=/usr/bin/etcd $DAEMON_ARGS`;
- `/etc/default/etcd` (`dpkg -S`: `etcd-server`, dpkg conffile MD5
  `b7921986da4a49739814359cc296d2fb`) contains in the pristine package only a five-line
  `##` comment header (`## Use environment to override, for example: ETCD_NAME=default`) and
  **no** default `ETCD_*` assignments (on the development machine `/etc/default/etcd` had
  already been hand-edited to append the nine OpenStack guide `ETCD_*` lines after that
  header).
- What remains **unconfirmed** without starting `etcd` (`etcd(1)` lists only command-line
  flags, and `/usr/share/doc/etcd-server/op-guide/configuration.md.gz` is gzipped) is that
  `/usr/bin/etcd` honors the seven `ETCD_*` environment variables beyond `ETCD_NAME` and
  `ETCD_DATA_DIR` at runtime; `verify()` details state this along with the fact that no TCP
  connection to `http://<management_ip>:2379` is attempted and cluster health is not
  checked.

Read-only checks that go beyond the page: `apt-cache policy etcd-server` as an `apply()`
precondition (reusing the existing `apt-cache.policy` spec), and `systemctl is-enabled` (so
that a configured but disabled unit is not reported as done) in `check()` and `verify()`.

## Reusable MariaDB SQL provisioning brick (`openstack_forge.execution.sql`)

Every OpenStack controller service in the 2025.1 (Epoxy) Ubuntu installation guide
(Keystone, Glance, Placement, Nova, Neutron, Cinder) begins with a "Prerequisites" database
section that creates one or more MariaDB databases and grants `ALL PRIVILEGES` to
`'<user>'@'localhost'` and `'<user>'@'%'` identified by `<SERVICE>_DBPASS`.

- **Why SQL is delivered over `stdin` (`mariadb.execute-sql`), never `argv`:** passing
  `-p<password>` or `--execute="... IDENTIFIED BY '<password>' ..."` in `argv` exposes the
  database password to every local user in `/proc/<pid>/cmdline` (`ps`). Instead,
  `MARIADB_EXECUTE_SQL` invokes `/usr/bin/mariadb --batch --skip-column-names` with
  `stdin_param="sql"` (`"sql_stdin"`), piping the SQL over `stdin=subprocess.PIPE` and
  keeping `argv` completely free of SQL or passwords.
- **Why `mariadb.query-sql` uses a fixed literal `--execute` query in `argv`:**
  `MARIADB_QUERY_SQL` embeds a fixed literal read-only `SELECT` over
  `information_schema.SCHEMATA` and `mysql.db` in `argv` with `params={}` (`mutates=False`),
  requiring zero user parameters and never touching `stdin`. `service_database_ready()` and
  `service_database_problems()` reuse it across `check()` and `verify()` for any controller
  service.
- **Idempotence (`CREATE DATABASE IF NOT EXISTS`):** `build_service_database_sql(databases,
  user, password)` validates every database and user name against `^[a-z][a-z0-9_]*$` and
  `password` with `validate_secret_value()`, and emits:
  ```sql
  CREATE DATABASE IF NOT EXISTS `keystone`;
  GRANT ALL PRIVILEGES ON `keystone`.* TO 'keystone'@'localhost' IDENTIFIED BY '<password>';
  GRANT ALL PRIVILEGES ON `keystone`.* TO 'keystone'@'%' IDENTIFIED BY '<password>';
  FLUSH PRIVILEGES;
  ```
  whereas the guide pages write `CREATE DATABASE keystone;` interactively, which would fail
  on a rerun if a previous run created the database before a later command failed.
- **Ubuntu 24.04 package facts:** `dpkg -S /usr/bin/mariadb /usr/bin/mysql` maps both paths
  to `mariadb-client-core` (`1:10.11.14-0ubuntu0.24.04.1`, installed as a dependency of
  `mariadb-server`); `/usr/bin/mariadb` is the ELF binary and `/usr/bin/mysql -> mariadb` is
  a symlink. On Ubuntu, MariaDB's local `root` account uses the `unix_socket` authentication
  plugin by default, so `/usr/bin/mariadb` run as effective UID 0 connects over the local
  Unix socket without a root password.

## Reusable `/usr/bin/openstack` CLI provisioning brick (`openstack_forge.execution.openstack_cli`)

Every OpenStack controller service after Keystone (Glance, Placement, Nova, Neutron,
Cinder) provisions its Keystone identity resources through `/usr/bin/openstack`
(`python3-openstackclient`):

1. Create the shared `service` project (`keystone-users-ubuntu.html`):
   `openstack project create --domain default --description "Service Project" service`
2. Create the service user and assign the `admin` role in the `service` project:
   - `openstack user create --domain default --password-prompt <service>`
   - `openstack role add --project service --user <service> admin`
3. Create the service entity and its three `RegionOne` endpoints (`public`, `internal`,
   `admin`):
   - `openstack service create --name <name> --description "<desc>" <type>`
   - `openstack endpoint create --region RegionOne <type> public <url>`
   - `openstack endpoint create --region RegionOne <type> internal <url>`
   - `openstack endpoint create --region RegionOne <type> admin <url>`

Established on an Ubuntu 24.04 development machine with read-only commands
(`python3-openstackclient` `7.4.0-0ubuntu1~cloud0` in `noble-updates/epoxy/main`):

- `/usr/bin/openstack` is a symlink managed by `update-alternatives` in
  `python3-openstackclient.postinst` (`/usr/bin/openstack -> /etc/alternatives/openstack ->
  /usr/bin/python3-openstack`, owned by `python3-openstackclient`).
- Every `openstack.*` `CommandSpec` authenticates as `admin` against
  `http://controller:5000/v3` using fixed literal arguments (`--os-project-domain-name
  Default --os-user-domain-name Default --os-project-name admin --os-username admin
  --os-auth-url http://controller:5000/v3 --os-identity-api-version 3`) and passes
  `ADMIN_PASS` via `env_params={"admin_password": "OS_PASSWORD"}`. `ADMIN_PASS` therefore
  never appears in `argv` or `/proc/<pid>/cmdline` (`ps`).
- **Why `--password-prompt` cannot be used with `stdin_param`:** inspection of
  `/usr/lib/python3/dist-packages/openstackclient/identity/v3/user.py` (`CreateUser`,
  `SetUser`) and `/usr/lib/python3/dist-packages/osc_lib/utils/__init__.py`
  (`get_password`, lines 562–580) shows that `--password-prompt` checks `stdin.isatty()` and
  raises `CommandError("No terminal detected attempting to read password")` when `stdin` is
  a pipe (`subprocess.PIPE`), and `python-openstackclient` provides no environment variable
  for the target user's password (`OS_PASSWORD` is the authenticating admin's password).
  Therefore `openstack.user-create` and `openstack.user-set-password` pass `--password
  {user_password}` (`"secret"`) in `argv` (masked as `********` everywhere by OpenStack
  Forge, though visible in `/proc/<pid>/cmdline` for the duration of that subcommand), while
  `ADMIN_PASS` stays in `OS_PASSWORD` (`env_params`).
- **Idempotence and avoiding duplicate UUIDs in Keystone v3:**
  - `ensure_service_project(executor, admin_password)`: runs `openstack.project-list` (`-f
    json`) first; creates `service` only if absent (`409 Conflict` if called twice), and
    raises `ExecutionError` if duplicate `service` projects exist.
  - `ensure_service_user(executor, admin_password, username, user_password)`: runs
    `openstack.user-list` (`-f json`) first; runs `openstack.user-create` if absent or
    `openstack.user-set-password` if already present (converging the password to
    `secrets.json`); then runs `openstack.role-assignment-list` (`--names -f json`, parsing
    both `"<name>@Default"` and `"<name>"`) and runs `openstack.role-add-user-project` only
    if the `admin` role is not yet assigned.
  - `ensure_service_catalog(executor, admin_password, service_name, service_type, url)`:
    Keystone v3 `POST /v3/services` and `POST /v3/endpoints` do **not** enforce uniqueness
    and create duplicate entries with new UUIDs if `service create` or `endpoint create` is
    run unconditionally on a rerun. `ensure_service_catalog()` always queries
    `openstack.service-list` and `openstack.endpoint-list` (`-f json`) first, creates only
    what is missing, and fails with `ExecutionError` if duplicates, a disabled service or
    endpoint, a type mismatch, or a URL mismatch already exist.

## Step: `keystone` (`openstack_forge.execution.keystone`)

**Unverified, and never run on a real host.** Sources:

- <https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html>
  (`doc/source/install/keystone-install-ubuntu.rst`)
- <https://docs.openstack.org/keystone/2025.1/install/keystone-users-ubuntu.html>
  (`doc/source/install/common/keystone-users.inc`)
- <https://docs.openstack.org/keystone/2025.1/install/keystone-verify-ubuntu.html>
  (`doc/source/install/keystone-verify-ubuntu.rst`)
- <https://docs.openstack.org/keystone/2025.1/install/keystone-openrc-ubuntu.html>
  (`doc/source/install/common/keystone-openrc.inc`)

Together these four pages prescribe for Ubuntu:

1. Prerequisites — create the `keystone` MariaDB database and grant `ALL PRIVILEGES ON
   keystone.*` to `'keystone'@'localhost'` and `'keystone'@'%'` identified by
   `KEYSTONE_DBPASS`;
2. `apt install keystone`;
3. Edit `/etc/keystone/keystone.conf`:
   - `[database] connection = mysql+pymysql://keystone:KEYSTONE_DBPASS@controller/keystone`
   - `[token] provider = fernet`
4. Populate the Identity service database:
   `su -s /bin/sh -c "keystone-manage db_sync" keystone`;
5. Initialize Fernet key repositories:
   - `keystone-manage fernet_setup --keystone-user keystone --keystone-group keystone`
   - `keystone-manage credential_setup --keystone-user keystone --keystone-group keystone`
6. Bootstrap the Identity service:
   `keystone-manage bootstrap --bootstrap-password ADMIN_PASS --bootstrap-admin-url http://controller:5000/v3/ --bootstrap-internal-url http://controller:5000/v3/ --bootstrap-public-url http://controller:5000/v3/ --bootstrap-region-id RegionOne`;
7. Edit `/etc/apache2/apache2.conf` to configure `ServerName controller`;
8. Restart the Apache service (`service apache2 restart`);
9. Create the `service` project (`keystone-users-ubuntu.html`):
   `openstack project create --domain default --description "Service Project" service`
   (the demo `mydomain`/`myproject`/`myuser`/`myrole` examples on that page are
   explicitly omitted);
10. Write the administrative client environment script (`keystone-openrc-ubuntu.html`),
    written to `/etc/openstack-forge/admin-openrc` (parent directory mode `0700`, file mode
    `0600`, no `.orig` backup).

It runs only on the controller (constructing it for another role raises `ExecutionError`).

| Phase | What happens |
| ----- | ------------ |
| `check()` | Pure first (runs zero external commands if any fails): `KEYSTONE_DBPASS` and `ADMIN_PASS` must exist in `SecretStore(root)`; `/etc/keystone/keystone.conf` and `/etc/apache2/apache2.conf` must exist and need no edit; `/etc/openstack-forge/admin-openrc` must exist with mode `0600` (and parent mode `0700`) and match the rendered `ADMIN_PASS` script; and `/etc/keystone/fernet-keys/0` and `/etc/keystone/credential-keys/0` must exist and be non-empty. Otherwise `True` only if, in order: `dpkg-query --status keystone` reports it installed, `systemctl is-active apache2` prints `active`, `mariadb.query-sql` confirms the `keystone` database and `'keystone'@'localhost'` + `'keystone'@'%'` grants exist, and `openstack.project-list` confirms the `service` project exists in the `default` domain (which also verifies `/v3` token authentication against `http://controller:5000/v3` as `admin`). |
| `apply()` | Precondition: loads `KEYSTONE_DBPASS` and `ADMIN_PASS` from `SecretStore(root)` (`PreconditionError` telling the operator to run `openstack-forge secrets init`). Then provisions the `keystone` MariaDB database and grants via `mariadb.execute-sql` (`stdin`); runs `apt-get install --yes keystone`; edits `/etc/keystone/keystone.conf` (`ini_set_many`) and `/etc/apache2/apache2.conf` (`disable_lines_and_set_block`); runs `keystone-manage db_sync`, `keystone-manage fernet_setup --keystone-user keystone --keystone-group keystone`, `keystone-manage credential_setup --keystone-user keystone --keystone-group keystone`, and `keystone-manage bootstrap ...` (`OS_BOOTSTRAP_PASSWORD` via `env_params`); runs `systemctl restart apache2`; ensures the `service` project exists (`ensure_service_project`: `openstack.project-list` then `openstack.project-create-service` if missing, `OS_PASSWORD` via `env_params`); and writes `/etc/openstack-forge/admin-openrc` (mode `0600`, parent `0700`, no `.orig` backup). |
| `verify()` | Checks `KEYSTONE_DBPASS` and `ADMIN_PASS` in `SecretStore(root)`, `/etc/keystone/keystone.conf` (`[database] connection` and `[token] provider = fernet`), `/etc/apache2/apache2.conf` (`ServerName controller` in the managed block with no active `ServerName` outside it), `/etc/openstack-forge/admin-openrc` (content, mode `0600`, parent mode `0700`), `/etc/keystone/fernet-keys/0` and `/etc/keystone/credential-keys/0`, `keystone` installed, `apache2` active, the `keystone` MariaDB database and grants, and the `service` project in Keystone via `openstack.project-list` (authenticating over HTTP `/v3` against `http://controller:5000/v3` as `admin`). |

Established on an Ubuntu 24.04 development machine with read-only commands:

- `apt-cache policy keystone`: `2:27.0.0-0ubuntu1.1~cloud2` in
  `noble-updates/epoxy/main` (Ubuntu Cloud Archive for OpenStack 2025.1 Epoxy) and
  `2:25.0.0-0ubuntu1.4` in `noble-updates/main`.
- `apt-cache show keystone`: `Depends: adduser, apache2 | httpd, libapache2-mod-wsgi-py3,
  python3-keystone (= 2:27.0.0-0ubuntu1.1~cloud2), ssl-cert (>= 1.0.12)`; `python3-keystone`
  depends on `keystone-common` and `python3-pymysql`.
- `dpkg -S` confirms:
  - `python3-keystone: /usr/bin/keystone-manage`;
  - `keystone-common: /etc/keystone` (`/etc/keystone/keystone.conf` is in
    `keystone-common.conffiles`; `keystone-common.postinst` sets `/etc/keystone` to `0750`
    `keystone:keystone` and `/etc/keystone/keystone.conf` to `0640` `keystone:keystone`);
  - `keystone: /etc/apache2/sites-available/keystone.conf` (enabled by `keystone.postinst`
    via `apache2_invoke ensite keystone.conf` on port `5000`);
  - `apache2: /etc/apache2/apache2.conf` (`2.4.58-1ubuntu8.15`) and
    `/usr/lib/systemd/system/apache2.service`. The pristine `/etc/apache2/apache2.conf`
    conffile (MD5 `354c9e6d2b88a0a3e0548f853840674c`) contains no `ServerName` line (on the
    development machine `ServerName controller` had been added on line 1).
- Inspection of `/usr/lib/python3/dist-packages/keystone/` confirms:
  - `[fernet_tokens] key_repository` defaults to `/etc/keystone/fernet-keys/` and
    `[credential] key_repository` defaults to `/etc/keystone/credential-keys/`;
  - `FernetUtils.initialize_key_repository()` is idempotent (returns immediately with exit 0
    if key `0` already exists, and otherwise creates keys `0` and `1` owned by
    `--keystone-user` / `--keystone-group` with `umask(0o177)`);
  - `BootStrap.do_bootstrap()` in `/usr/lib/python3/dist-packages/keystone/cmd/cli.py`
    (lines 64–235) reads `self.password = os.environ.get('OS_BOOTSTRAP_PASSWORD') or
    CONF.command.bootstrap_password`, and `Bootstrapper.bootstrap()` is idempotent (ensures
    the `Default` domain, `admin` project, default roles, role assignments, `RegionOne`, and
    `http://controller:5000/v3/` `identity` endpoints, and creates or updates the `admin`
    user password).

Deviations, trade-offs, and scope boundary:

- **`keystone-manage db_sync` invoked directly as `root` instead of `su`:** the guide runs
  `su -s /bin/sh -c "keystone-manage db_sync" keystone`, whereas `CommandSpec` forbids `su`
  and `sudo`. Because `/etc/keystone/keystone.conf` is edited before `db_sync` to point
  `[database] connection` at MariaDB (`mysql+pymysql://...`), `db_sync` does not create a
  root-owned SQLite file in `/var/lib/keystone`. Caveat: if `log_file` or `log_dir` is
  enabled in `keystone.conf`, running `keystone-manage` as `root` can create a root-owned
  log file in `/var/log/keystone`.
- **Apache `ServerName controller` managed block:** `/etc/apache2/apache2.conf` is edited
  with `disable_lines_and_set_block` using `# BEGIN openstack-forge keystone-apache` /
  `ServerName controller` / `# END openstack-forge keystone-apache` while commenting out any
  active `ServerName` lines outside the block so no earlier `ServerName` directive conflicts
  with it.
- **Zero password exposure in `argv` (`ps`) during the `keystone` step:** `KEYSTONE_DBPASS`
  is sent to `/usr/bin/mariadb` over `stdin` (`stdin_param="sql"`) and written to
  `/etc/keystone/keystone.conf`. `ADMIN_PASS` is passed to `keystone-manage bootstrap` via
  `env_params={"password": "OS_BOOTSTRAP_PASSWORD"}` and to `/usr/bin/openstack` via
  `env_params={"admin_password": "OS_PASSWORD"}` (stored in `/proc/<pid>/environ`, readable
  only by `root`), and written to `/etc/openstack-forge/admin-openrc` (mode `0600`). Neither
  `KEYSTONE_DBPASS` nor `ADMIN_PASS` ever appears in `argv` or `/proc/<pid>/cmdline` (`ps`)
  during the `keystone` step.
- **Scope boundary (`keystone-users-ubuntu.html` and `keystone-openrc-ubuntu.html`):**
  only the `service` project required by OpenStack services and
  `/etc/openstack-forge/admin-openrc` (mode `0600`, no `.orig` backup) are created; the guide's
  illustrative `mydomain`, `myproject`, `myuser`, `myrole`, and `demo-openrc` examples are
  omitted.
- **What `verify()` checks and does not check:** `verify()` runs `openstack.project-list`
  against `http://controller:5000/v3` as `admin`, which exercises live HTTP `/v3` token
  issuance and confirms the `service` project exists in the `default` domain; the SQL schema
  version from `db_sync` is not queried directly.



