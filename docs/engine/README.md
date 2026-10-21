# Execution engine and `openstack-forge apply`

`openstack_forge.execution` contains the confined file-editing primitives, redaction helper,
state store, step engine, and the first implemented deployment step (`hosts-file`).

**Only `hosts-file` is implemented, it remains marked `unverified` in the step catalog, and
it has not been run on a real VM.** Every other catalog step is rejected as "not
implemented" by `openstack-forge apply`. Nothing in this package or in `apply` runs an
external command (`subprocess`), uses `sudo`, or touches the network: all work is pure
Python file operations confined to the target root directory.

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
openstack-forge apply --config cluster.yaml --step hosts-file [--node HOSTNAME] [--root PATH] [--yes]
```

| Option | Meaning |
| ------ | ------- |
| `--config PATH` | Cluster configuration (default: `cluster.yaml`), validated before use. |
| `--step STEP_ID` | Catalog step ID to apply (required). Only steps registered in `IMPLEMENTED_STEPS` (`hosts-file`) can run; any other catalog step exits with code 1 and `"not implemented"`. |
| `--node HOSTNAME` | Target node hostname. If omitted, matched against the local machine's hostname (`socket.gethostname()`). |
| `--root PATH` | Target filesystem root (default: `/`). Used by tests to confine all writes to a temporary directory. |
| `--yes` | Write changes and update the state file. Without `--yes`, `apply` runs in **dry-run** mode: it prints the redacted unified diff, writes nothing, and exits 0. |

- **Catalog status warning:** `apply` always prints a visible warning to `stderr` when a
  step is marked `unverified` in `steps.json`. `hosts-file` remains `unverified` in the
  catalog and has not been run on a real VM.
- **Root check:** writing to the real `/` root (`--yes` without `--root`, or `--root /`)
  requires effective UID 0 (`os.geteuid() == 0`); otherwise `apply` fails with a clear
  message and exits 1. Passing `--root PATH` (where `PATH` does not resolve to `/`)
  bypasses the UID check for non-root tests while still confining every write under `PATH`.
- **State file location:** `<root>/var/lib/openstack-forge/state.json` (so `/var/lib/openstack-forge/state.json`
  on `/`, or `<tmp>/var/lib/openstack-forge/state.json` under `--root <tmp>`), written
  atomically with mode `0600` only when `--yes` is given.

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
