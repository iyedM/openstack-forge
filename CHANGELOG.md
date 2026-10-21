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
  When this entry was written no real installation step existed: there was no `apply`
  command, no catalog step was executed, and nothing ran commands, used `sudo` or the
  network. (Superseded: see the `time-sync-controller` and `openstack-packages` entries
  below; their apt commands use the network.)
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
- `openstack-packages` step (`openstack_forge.execution.openstack_packages`), for every
  role. **Unverified and never run on a real host.** Following the official "OpenStack
  packages for Ubuntu" page: `add-apt-repository --yes --cloud <pocket>`, then
  `apt-get install --yes python3-openstackclient`; no upgrade, dist-upgrade or reboot, no
  other package requested. The pocket comes from the supported release table
  (`cloud-archive:epoxy` for Ubuntu 24.04 / 2025.1), never from the step code.
  - `check()` requires both the installed client (`dpkg-query --status`) and an apt source
    for the pocket in `apt-cache policy python3-openstackclient`, because Ubuntu 24.04's
    own archive also ships the client.
  - `apply()` first requires `software-properties-common` (which provides
    `add-apt-repository`) and fails with `PreconditionError` naming it if it is missing;
    it never installs it.
  - `verify()` reports which part failed and always says the repository check (the Ubuntu
    Cloud Archive suite naming `<series>-updates/<name>`) is unconfirmed on a real host.
- Three reviewed command specs: `apt-get update` (uses the network), `add-apt-repository
  --yes --cloud {pocket}` (non-interactive; it updates the package lists itself, per
  add-apt-repository(1) `--no-update`) and `apt-cache policy {package}` (read-only), with
  paths from `dpkg -S`, man page justifications and failure hints.
- `openstack_forge.execution.apt_output`: pure parsers for `dpkg-query --status` and
  `apt-cache policy` output, with synthetic fixtures in `tests/fixtures/apt/` modeled on
  real output read on an Ubuntu 24.04 machine.
- `PreconditionError` for steps whose host prerequisites are missing.
- `time-sync-other` step (`openstack_forge.execution.chrony`) for `compute`, `block` and
  `object` nodes; constructing it for the controller raises `ExecutionError`. **Unverified
  and never run on a real host.** Following the official "Other nodes" NTP page:
  `apt-get update`, `apt-get install --yes chrony`, then in `/etc/chrony/chrony.conf`
  (which must already exist) every active `pool`/`server` line outside the
  `openstack-forge time-sync` managed block is commented out and the block holds
  `server controller iburst`, then `systemctl restart chrony`. `check()` is the pure file
  check plus `dpkg-query --status chrony`; `verify()` checks the block, the absence of
  other active sources and `systemctl is-active chrony`, and always says that actual time
  synchronization is not checked. It reuses the existing command specs; the registry is
  unchanged.
- Role-scoped catalog dependencies: a `depends_on` entry may be
  `{"step": <id>, "roles": [...]}`, which applies only on nodes with one of those roles
  (validated: existing step, roles within the step's own roles, no duplicate dependency,
  no cycle). `Step.dependencies_for(role)` exposes the result.
- Synthetic fixture `policy_client_noble_installed_uca_candidate.txt`: client installed
  from `noble/main` while `cloud-archive:epoxy` offers a newer candidate.
- `ini_set_many(root, target, settings, ...)` in `openstack_forge.execution.files`: applies
  `(section, key, value)` settings in order in one edit, with one combined redacted diff;
  a new file is created without a `.orig` backup, an existing file that changes gets one
  `.orig` of its previous content, an unchanged file is not written, and the parent
  directory is never created. `ini_set()` is now built on the same pure line editor.
- `mariadb` step (`openstack_forge.execution.mariadb`, controller only). **Unverified and
  never run on a real host.** Following the official SQL database page for Ubuntu: install
  `mariadb-server` and `python3-pymysql`, write the guide's six `[mysqld]` keys to
  `/etc/mysql/mariadb.conf.d/99-openstack.cnf` (`bind-address` = the controller's
  management IP), then `systemctl restart mariadb`. `verify()` checks the file,
  `python3-pymysql` and the service; it does not check SQL connectivity or the 3306
  listener. `mysql_secure_installation` is not run (interactive; the guide notes Ubuntu
  uses `unix_socket` authentication for local root). No new command spec was added.
- `memcached` step (controller only). **Unverified and never run on a real host.** From
  the official Memcached page: install `memcached` and `python3-memcache`, change the
  listen address in `/etc/memcached.conf` to the controller's management IP (every active
  `-l`/`--listen` line is commented out and `-l <ip>` is written in a managed block, in one
  atomic edit), `systemctl restart memcached`. Verified from `memcached 1.6.24-1ubuntu0.3`
  on Ubuntu 24.04: `memcached.service` passes `/etc/memcached.conf` to
  `systemd-memcached-wrapper`, and `memcached.postinst` copies
  `/usr/share/memcached/memcached.conf.default` (which has both `-l 127.0.0.1` and
  `-l ::1`) to `/etc/memcached.conf`.
- `etcd` step (controller only, single node). **Unverified and never run on a real host.**
  From the official Etcd page: check `apt-cache policy etcd-server` for an installation
  candidate (failing with an actionable `PreconditionError` if Universe is disabled),
  install `etcd-server` (Ubuntu 24.04 has no `etcd` candidate), write the page's nine
  `ETCD_*` keys to `/etc/default/etcd` from the controller's management IP and the
  `controller` alias, then `systemctl enable etcd` and `systemctl restart etcd`. Verified
  from `etcd-server 3.4.30-1ubuntu0.24.04.3` on Ubuntu 24.04: `etcd.service` sets
  `Environment=ETCD_NAME=%H` and `Environment=ETCD_DATA_DIR=/var/lib/etcd/default` before
  `EnvironmentFile=-/etc/default/%p`, and the pristine `/etc/default/etcd` conffile has
  only a five-line comment header and no default `ETCD_*` assignments.
- Command specs `systemctl.enable` (required by the etcd page) and `systemctl.is-enabled`
  (read-only, so a disabled unit is not reported as done); the registry test pins them.
- `disable_lines_and_set_block()`: comment out matching lines and set a managed block in a
  single atomic edit.
- CI job running the test suite in an `ubuntu:24.04` container with the distribution's
  `python3-jsonschema` and `python3-yaml` (venv with system site packages, non-root user).
- Secrets management brick (`openstack_forge.execution.secrets`, **never run on a real
  host**; no new step and no new command in `DEFAULT_REGISTRY`):
  - `SECRET_NAMES` / `SECRET_DESCRIPTIONS` for the 15 named passwords from
    `https://docs.openstack.org/install-guide/environment-security.html` (`ADMIN_PASS`,
    `CINDER_DBPASS`, `CINDER_PASS`, `DASH_DBPASS`, `DEMO_PASS`, `GLANCE_DBPASS`,
    `GLANCE_PASS`, `KEYSTONE_DBPASS`, `METADATA_SECRET`, `NEUTRON_DBPASS`, `NEUTRON_PASS`,
    `NOVA_DBPASS`, `NOVA_PASS`, `PLACEMENT_PASS`, `RABBIT_PASS`).
  - `generate_secret()` using `secrets.choice` over `[A-Za-z0-9]` with minimum length 32,
    safe without escaping in `.ini` files, database/broker URLs and `CommandSpec`
    parameters.
  - `SecretStore` storing JSON under `<root>/etc/openstack-forge/secrets.json` (directory
    mode `0700`, file mode `0600`, atomic write, symlinks refused, owner and mode verified
    on both `load()` and `ensure()`, idempotent `ensure()` that generates only missing
    keys and never overwrites existing values). Multi-node secret distribution is not
    implemented.
  - Exact-value redaction (`register_secret` / `register_secrets`) automatically populated
    by `SecretStore` so any registered secret value is replaced with `********` across
    diffs, command output, `VerifyResult` details, error messages, the state file and the
    audit log even without a `password=` key (rejecting strings shorter than 8 characters
    or containing whitespace).
  - `CommandSpec` support for `sensitive_params` / `"secret"` parameter type and opt-in
    `stdin_param` delivery via `stdin=subprocess.PIPE` (keeping `stdin=DEVNULL` as the
    default), with documentation of the OS process-table (`ps`) limitation when a tool only
    accepts passwords in `argv`.
  - CLI subcommands `openstack-forge secrets init [--root PATH] [--file PATH]` (prints
    names only, never values) and `openstack-forge secrets show NAME [--root PATH] [--file
    PATH]` (prints one secret value to stdout and nothing else).
- `SecretSpec` and `EXTRA_SECRET_SPECS` (`openstack_forge.execution.secrets`) so future
  documented secrets (such as `SWIFT_PASS`) can be added explicitly with a required
  description and `https://docs.openstack.org/...` source URL (never invented silently),
  plus `PLACEMENT_DBPASS` citing the Placement 2025.1 Ubuntu installation guide
  (`https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html`; 16 secrets in
  `SECRET_NAMES`).
- `CommandSpec(env_params=...)` (`openstack_forge.execution.commands`) as a third explicit
  delivery mode for sensitive parameters via environment variables added to
  `SANITIZED_ENV` (validated as strict POSIX uppercase names `^[A-Z][A-Z0-9_]*$` and
  prevented from colliding with `SANITIZED_ENV` keys), with full masking everywhere and
  documentation of `/proc/<pid>/environ` vs `/proc/<pid>/cmdline`. No spec in
  `DEFAULT_REGISTRY` uses `env_params` yet.
- `rabbitmq` step (`openstack_forge.execution.rabbitmq`, controller only). **Unverified and
  never run on a real host.** Following
  `https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html`: requires
  `RABBIT_PASS` in `SecretStore` (`PreconditionError` telling the operator to run
  `openstack-forge secrets init`), installs `rabbitmq-server`, creates the `openstack` user
  with `RABBIT_PASS` (or converges its password with `rabbitmqctl change_password` if
  `rabbitmqctl list_users` shows `openstack` already exists), sets permissions `".*" ".*"
  ".*"` in vhost `/`, and verifies the package, `rabbitmq-server.service` active state,
  user presence and permissions. Documents the `/proc/<pid>/cmdline` (`ps`) limitation for
  `rabbitmqctl`, the `hosts-file` hostname resolution requirement, and unencrypted AMQP on
  port 5672.
- Five reviewed `/usr/sbin/rabbitmqctl` command specs in `DEFAULT_REGISTRY`:
  `rabbitmqctl.add-user`, `rabbitmqctl.change-password`, `rabbitmqctl.set-permissions`,
  `rabbitmqctl.list-users` and `rabbitmqctl.list-user-permissions`, with `RABBITMQCTL_FAILURE_HINT`.
- Reusable MariaDB SQL provisioning brick (`openstack_forge.execution.sql`) and `"sql_stdin"`
  parameter type in `openstack_forge.execution.commands`:
  - `"sql_stdin"` parameter type (printable ASCII plus `\n` up to 4096 characters, restricted
    exclusively to `stdin_param` by `CommandSpec.__post_init__` and automatically marked
    sensitive so rejected SQL is masked in `ParameterError`).
  - `build_service_database_sql()`, `provision_service_database()`,
    `service_database_ready()` and `service_database_problems()` using
    `CREATE DATABASE IF NOT EXISTS` and `GRANT ALL PRIVILEGES ... IDENTIFIED BY '<password>'`
    for `'user'@'localhost'` and `'user'@'%'` sent via `stdin` to `mariadb.execute-sql`, and
    queried read-only via `mariadb.query-sql` (`params={}`, `mutates=False`).
- `keystone` step (`openstack_forge.execution.keystone`, controller only) in the catalog and
  `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html`: requires
  `KEYSTONE_DBPASS` and `ADMIN_PASS` in `SecretStore`, provisions the `keystone` MariaDB
  database and grants via `stdin`, installs `keystone`, edits `/etc/keystone/keystone.conf`
  (`[database] connection` and `[token] provider = fernet`) and `/etc/apache2/apache2.conf`
  (`ServerName controller` in the `openstack-forge keystone-apache` managed block while
  commenting out any outside `ServerName` lines), runs `keystone-manage db_sync`,
  `fernet_setup`, `credential_setup`, and `bootstrap`, and restarts `apache2`.
- Six reviewed command specs in `DEFAULT_REGISTRY`: `mariadb.execute-sql`,
  `mariadb.query-sql`, `keystone-manage.db-sync`, `keystone-manage.fernet-setup`,
  `keystone-manage.credential-setup`, and `keystone-manage.bootstrap`, with
  `MARIADB_SQL_FAILURE_HINT` and `KEYSTONE_MANAGE_FAILURE_HINT`.
- Reusable `/usr/bin/openstack` CLI provisioning brick
  (`openstack_forge.execution.openstack_cli`) and three new parameter types in
  `openstack_forge.execution.commands`:
  - `"openstack_name"` (`^[a-z][a-z0-9_-]{0,63}$`), `"endpoint_interface"`
    (`public|internal|admin`), and `"endpoint_url"`
    (`^http://controller:(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])(?:/[A-Za-z0-9._/-]+)?$`
    with no `..` or `//`, also permitting the literal `%\(tenant_id\)s` token).
  - Eleven reviewed `/usr/bin/openstack` command specs in `DEFAULT_REGISTRY`
    (`openstack.project-list`, `openstack.project-create-service`, `openstack.user-list`,
    `openstack.user-create`, `openstack.user-set-password`,
    `openstack.role-assignment-list`, `openstack.role-add-user-project`,
    `openstack.service-list`, `openstack.service-create`, `openstack.endpoint-list`,
    `openstack.endpoint-create`), all authenticating against `http://controller:5000/v3` as
    `admin` with `env_params={"admin_password": "OS_PASSWORD"}` and
    `OPENSTACK_CLI_FAILURE_HINT`.
  - Pure JSON parsers (`parse_project_list`, `parse_user_list`,
    `parse_role_assignment_list`, `parse_service_list`, `parse_endpoint_list`), pure state
    verifiers, and idempotent convergence helpers (`ensure_service_project`,
    `ensure_service_user`, `ensure_service_catalog`, `service_project_ready`,
    `service_user_ready`, `service_catalog_ready`, and `*_problems`) that query `-f json`
    first to prevent duplicate UUIDs in Keystone v3.
- Completed the `keystone` controller step (`keystone-users-ubuntu.html`,
  `keystone-verify-ubuntu.html`, and `keystone-openrc-ubuntu.html`):
  - creates and verifies the `service` project in the `default` domain via
    `ensure_service_project` / `service_project_ready` / `service_project_problems`
    (verifying HTTP `/v3` token issuance against `http://controller:5000/v3` as `admin`);
  - writes and verifies `<root>/etc/openstack-forge/admin-openrc` (parent directory mode
    `0700`, file mode `0600`, no `.orig` backup, redacted diff).
- `glance` step (`openstack_forge.execution.glance`, controller only) in the catalog and
  `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/glance/2025.1/install/install-ubuntu.html`: requires
  `ADMIN_PASS`, `GLANCE_DBPASS`, and `GLANCE_PASS` in `SecretStore`, provisions the `glance`
  MariaDB database and grants via `stdin`, converges the `glance` Keystone user, `admin`
  role on `service`, `glance` (`image`) service entity and three `RegionOne` endpoints at
  `http://controller:9292`, installs `glance`, edits `/etc/glance/glance-api.conf` (14 keys
  across `[database]`, `[keystone_authtoken]`, `[paste_deploy]`, `[DEFAULT]`,
  `[glance_store]`, and `[fs]`; optional `[oslo_limit]` unified limits omitted and
  documented), runs `/usr/bin/glance-manage db_sync`, and restarts `glance-api`.
- `placement` step (`openstack_forge.execution.placement`, controller only) in the catalog
  and `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html`: requires
  `ADMIN_PASS`, `PLACEMENT_DBPASS`, and `PLACEMENT_PASS` in `SecretStore`, provisions the
  `placement` MariaDB database and grants via `stdin`, converges the `placement` Keystone
  user, `admin` role on `service`, `placement` (`placement`) service entity and three
  `RegionOne` endpoints at `http://controller:8778`, installs `placement-api`, edits
  `/etc/placement/placement.conf` (10 keys across `[placement_database]`, `[api]`, and
  `[keystone_authtoken]`), runs `/usr/bin/placement-manage db sync` (two separate tokens
  `"db"` and `"sync"`), and restarts `apache2`.
- Two reviewed command specs in `DEFAULT_REGISTRY`: `glance-manage.db-sync`
  (`("/usr/bin/glance-manage", "db_sync")`) and `placement-manage.db-sync`
  (`("/usr/bin/placement-manage", "db", "sync")`), with `GLANCE_MANAGE_FAILURE_HINT` and
  `PLACEMENT_MANAGE_FAILURE_HINT`.
- `nova-controller` step (`openstack_forge.execution.nova`, controller only) in the catalog
  and `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/nova/2025.1/install/controller-install-ubuntu.html`: requires
  `ADMIN_PASS`, `RABBIT_PASS`, `NOVA_DBPASS`, `NOVA_PASS`, and `PLACEMENT_PASS` in
  `SecretStore`, provisions the three `nova_api`, `nova`, and `nova_cell0` MariaDB databases
  and grants for `nova@localhost` and `nova@%` via `stdin`, converges the `nova` Keystone
  user, `admin` role on `service`, `nova` (`compute`) service entity and three `RegionOne`
  endpoints at `http://controller:8774/v2.1`, installs `nova-api`, `nova-conductor`,
  `nova-novncproxy`, and `nova-scheduler`, edits `/etc/nova/nova.conf` (37 keys across
  `[api_database]`, `[database]`, `[DEFAULT]`, `[api]`, `[keystone_authtoken]`,
  `[service_user]`, `[vnc]`, `[glance]`, `[oslo_concurrency]`, `[placement]`, and
  `[scheduler]`, documenting `[service_user] auth_url = http://controller:5000/` instead of
  the upstream doc's DevStack TLS `https://controller/identity`), runs
  `/usr/bin/nova-manage api_db sync` and `/usr/bin/nova-manage cell_v2 map_cell0`, checks
  `/usr/bin/nova-manage cell_v2 list_cells` before running
  `/usr/bin/nova-manage cell_v2 create_cell --name=cell1 --verbose` (because `create_cell`
  exits 2 if `cell1` already exists), runs `/usr/bin/nova-manage db sync`, and restarts
  `nova-api`, `nova-scheduler`, `nova-conductor`, and `nova-novncproxy`.
- `nova-compute` step (`openstack_forge.execution.nova`, compute nodes only; depends on
  `openstack-packages` and `nova-controller`) in the catalog and `IMPLEMENTED_STEPS`.
  **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/nova/2025.1/install/compute-install-ubuntu.html`: requires only
  `RABBIT_PASS`, `NOVA_PASS`, and `PLACEMENT_PASS` in `SecretStore` (least-privilege:
  `NOVA_DBPASS` and `ADMIN_PASS` are never required on compute nodes), installs
  `nova-compute`, edits `/etc/nova/nova.conf` (35 keys across `[DEFAULT]`, `[api]`,
  `[keystone_authtoken]`, `[service_user]`, `[vnc]`, `[glance]`, `[oslo_concurrency]`, and
  `[placement]`, with `[vnc] server_listen = 0.0.0.0`,
  `server_proxyclient_address = $my_ip`, and
  `novncproxy_base_url = http://controller:6080/vnc_auto.html`), inspects `<root>/proc/cpuinfo`
  in pure Python to set `[libvirt] virt_type = kvm` (when `vmx` or `svm` is present) or
  `qemu` (otherwise) in `/etc/nova/nova-compute.conf`, and restarts `nova-compute`.
- Five reviewed `/usr/bin/nova-manage` command specs in `DEFAULT_REGISTRY`:
  `nova-manage.api-db-sync`, `nova-manage.cell-v2-map-cell0`,
  `nova-manage.cell-v2-list-cells` (`mutates=False`), `nova-manage.cell-v2-create-cell1`,
  and `nova-manage.db-sync`, with `NOVA_MANAGE_FAILURE_HINT`.
- `ini_comment_out_keys(text, keys, *, prefix=DISABLED_LINE_PREFIX)` and the `comment_out`
  keyword parameter on `ini_set_many()` in `openstack_forge.execution.files` to comment out
  active `(section, key)` lines (used by `neutron-compute` to disable the active
  `[database] connection = sqlite:////var/lib/neutron/neutron.sqlite` line shipped in
  Ubuntu's `/etc/neutron/neutron.conf` in a single atomic write and `.orig` backup).
- `"interface_name"` parameter type in `openstack_forge.execution.commands` (`PARAM_TYPES`)
  and five reviewed Neutron / Open vSwitch command specs in `DEFAULT_REGISTRY`:
  `neutron-db-manage.upgrade-head`, `ovs-vsctl.add-br`, `ovs-vsctl.add-port`,
  `ovs-vsctl.br-exists` (`mutates=False`, `allowed_exit_codes={0, 2}`), and
  `ovs-vsctl.list-ports` (`mutates=False`), with `NEUTRON_DB_MANAGE_FAILURE_HINT` and
  `OVS_VSCTL_FAILURE_HINT`.
- `neutron-controller` step (`openstack_forge.execution.neutron`, controller only; depends
  on `mariadb`, `rabbitmq`, `memcached`, `keystone`, and `nova-controller`) in the catalog
  and `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/neutron/2025.1/install/controller-install-ubuntu.html` and
  `controller-install-option2-ubuntu.html` (with Open vSwitch in place of the removed
  Linuxbridge agent; see `https://docs.openstack.org/neutron/2025.1/admin/deploy-ovs-selfservice.html`
  and `ovn/openvswitch_to_ovn_migration.html`): requires `ADMIN_PASS`, `RABBIT_PASS`,
  `NEUTRON_DBPASS`, `NEUTRON_PASS`, `NOVA_PASS`, and `METADATA_SECRET` in `SecretStore`,
  provisions the `neutron` MariaDB database and `'neutron'@'localhost'` + `'neutron'@'%'`
  grants over `stdin`, converges the `neutron` Keystone user (`admin` role in `service`) and
  `neutron` (`network`) service with three `RegionOne` endpoints at
  `http://controller:9696`, installs `neutron-server`, `neutron-plugin-ml2`,
  `neutron-openvswitch-agent`, `neutron-l3-agent`, `neutron-dhcp-agent`, and
  `neutron-metadata-agent`, edits all seven configuration files (`/etc/neutron/neutron.conf`,
  `/etc/neutron/plugins/ml2/ml2_conf.ini`,
  `/etc/neutron/plugins/ml2/openvswitch_agent.ini`, `/etc/neutron/l3_agent.ini`,
  `/etc/neutron/dhcp_agent.ini`, `/etc/neutron/metadata_agent.ini`, and the `[neutron]`
  section of `/etc/nova/nova.conf`), restarts `openvswitch-switch`, idempotently creates
  `br-provider` and attaches the controller's provider interface via `ovs-vsctl --may-exist`,
  runs `/usr/bin/neutron-db-manage --config-file /etc/neutron/neutron.conf --config-file
  /etc/neutron/plugins/ml2/ml2_conf.ini upgrade head`, and restarts `nova-api`,
  `neutron-server`, `neutron-openvswitch-agent`, `neutron-dhcp-agent`,
  `neutron-metadata-agent`, and `neutron-l3-agent`.
- `neutron-compute` step (`openstack_forge.execution.neutron`, compute nodes only; depends on
  `openstack-packages`, `nova-compute`, and `neutron-controller`) in the catalog and
  `IMPLEMENTED_STEPS`. **Unverified and never run on a real host.** Following
  `https://docs.openstack.org/neutron/2025.1/install/compute-install-ubuntu.html` and
  `compute-install-option2-ubuntu.html` (with Open vSwitch): requires only `RABBIT_PASS` and
  `NEUTRON_PASS` in `SecretStore` (least-privilege: `ADMIN_PASS`, `NEUTRON_DBPASS`,
  `NOVA_PASS`, and `METADATA_SECRET` are never required on compute nodes), installs
  `neutron-openvswitch-agent`, comments out `[database] connection` and sets `[DEFAULT]
  transport_url` and `[oslo_concurrency] lock_path` in `/etc/neutron/neutron.conf`, edits
  `/etc/neutron/plugins/ml2/openvswitch_agent.ini` and the `[neutron]` section of
  `/etc/nova/nova.conf`, restarts `openvswitch-switch`, idempotently creates `br-provider`
  and attaches the compute node's provider interface via `ovs-vsctl --may-exist`, and
  restarts `nova-compute` and `neutron-openvswitch-agent`.

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
- `time-sync-controller` now runs `apt-get update` before `apt-get install --yes chrony`,
  because a fresh image may have stale or empty package lists.
- The registry test deliberately pins seven reviewed commands (adds `apt-get.update`,
  `add-apt-repository.cloud-archive`, `apt-cache.policy`).
- `apply` dry runs label the read-only commands as those `check()`, `apply()` (for its
  preconditions) and `verify()` may run; `parse_dpkg_status` moved to `apt_output` (still
  importable from `chrony` and `openstack_forge.execution`).
- Documentation no longer says that the project or `apply` uses no network: the Python
  code opens no connection, but the apt commands it runs (`apt-get update`, `apt-get
  install`, `add-apt-repository`) do.
- `openstack-packages` `check()` and `verify()` now also require the installed
  `python3-openstackclient` version (`Installed:` in `apt-cache policy`) to be a version
  listed under a source of the pocket (`installed_from()` in `apt_output`). A client
  installed from `noble/main` before the pocket was enabled, or left there because
  `apply()` stopped after `add-apt-repository`, is no longer reported as done; `verify()`
  names the installed version and the version(s) the pocket offers.
- `openstack-packages` now depends on `time-sync-controller` on the controller only and on
  the node's own `time-sync-other` on `compute`, `block` and `object` nodes (role-scoped
  dependencies), replacing the documented limitation that other nodes did not wait for
  their own time synchronization. The controller does not wait for other nodes.
- `time-sync-other` now also depends on `hosts-file` (on the same node), because its
  `server controller iburst` line needs the `controller` alias in `/etc/hosts`; before,
  it waited only for the controller's `time-sync-controller`.
- The catalog's `time-sync-other` verification text now describes what the step checks
  (chrony installed and active, `server controller iburst` as the only pool/server
  source) instead of a synchronized time source, which is not checked yet.
- `IMPLEMENTED_STEPS` pin deliberately updated to four steps; `time-sync-other` is no
  longer used as the example of an unimplemented step in tests.
- Replaced the development machine's local RFC 1918 IP in `memcached`/`etcd`
  modules, tests, `_service_fakes.py` and `docs/engine/README.md` with the RFC 5737
  documentation address `192.0.2.170`.
- `IMPLEMENTED_STEPS` pin deliberately updated to all fifteen catalog steps (adds `rabbitmq`,
  `keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`,
  `neutron-controller`, and `neutron-compute`), and the `DEFAULT_REGISTRY` test deliberately
  pins forty-three reviewed commands (adds the five `/usr/sbin/rabbitmqctl`, two
  `/usr/bin/mariadb`, four `/usr/bin/keystone-manage`, eleven `/usr/bin/openstack`,
  `glance-manage.db-sync`, `placement-manage.db-sync`, five `/usr/bin/nova-manage`,
  `neutron-db-manage.upgrade-head`, and four `/usr/bin/ovs-vsctl` specs).
- `keystone-manage.bootstrap` now delivers `ADMIN_PASS` via
  `env_params={"password": "OS_BOOTSTRAP_PASSWORD"}` instead of `--bootstrap-password
  {password}` in `argv` (confirmed in `/usr/lib/python3/dist-packages/keystone/cmd/cli.py`),
  so neither `KEYSTONE_DBPASS` (`stdin_param`) nor `ADMIN_PASS` (`env_params`) ever appears
  in `argv` or `/proc/<pid>/cmdline` (`ps`) during the `keystone` step.

### Fixed

- Validation now works from a regular (non-editable) install; previously the schema
  was only found in a source checkout.
