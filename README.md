# OpenStack Forge

OpenStack Forge is a Python CLI toolkit for validating, diagnosing, planning, applying, and verifying multi-node OpenStack 2025.1 (Epoxy) deployments on Ubuntu 24.04 servers following the official OpenStack Installation Guide.

## Current status

All 18 catalog steps (`hosts-file`, `time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`, `keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`, `neutron-controller`, `neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage`) are implemented in `openstack_forge.execution` and tested against a fake execution backend and temporary root directories. **All 18 steps remain marked `unverified` in the step catalog, and none has ever been run on a real host.** Remote multi-node SSH orchestration and automated secret distribution across nodes are not implemented; `openstack-forge` runs locally on each node.

## Goals

- Model a multi-node OpenStack 2025.1 (Epoxy) cluster on Ubuntu 24.04 using a validated YAML configuration format.
- Run read-only host preflight diagnostics (`doctor`) and deterministic cross-node execution plans (`plan`) before changing a machine.
- Confine all file edits under an explicit root directory (`--root`) and execute external commands exclusively through a reviewed 49-command allowlist (`DEFAULT_REGISTRY`) without shells or `sudo`.
- Manage OpenStack passwords in a local `0600` `SecretStore` (`/etc/openstack-forge/secrets.json`) with automatic redaction across diffs, logs, state, and CLI output.

## Non-goals

- Running arbitrary shell commands, `sudo`, `su`, `netplan`, or `ip link` mutations on the host.
- Performing remote SSH node enrollment or automated multi-node secret distribution.
- Hardcoding credentials, secrets, or real RFC 1918 deployment IPs in examples, tests, or documentation (RFC 5737 documentation addresses are used everywhere).
- Claiming production readiness or marking catalog steps `verified` before they have been validated on real Ubuntu 24.04 hosts.

## High-level architecture

The project is organized into four Python subpackages under `src/openstack_forge/` plus the CLI entrypoint:

- **CLI layer (`cli.py`):** user-facing commands (`init`, `config validate`, `doctor`, `plan`, `apply`, `verify`, `secrets`). Read-only commands (`init`, `config validate`, `doctor`, `plan`) never import `openstack_forge.execution`.
- **Configuration layer (`config/`):** YAML loading (`SafeLoader` with duplicate-key rejection), JSON Schema validation (`cluster.schema.json`), and semantic network/topology checks.
- **Diagnostics layer (`diagnostics/`):** read-only host preflight checks (`doctor`) comparing OS, CPU, RAM, virtualization flags, storage disks, and network interfaces/link state against `cluster.yaml`.
- **Orchestration layer (`orchestration/`):** the 18-step catalog (`steps.json`) and deterministic multi-node DAG planner (`plan`).
- **Execution layer (`execution/`):** confined file primitives, `SecretStore`, `StateStore` + `apply_lock`, `Engine`, `CommandExecutor` (`DEFAULT_REGISTRY`), and the 18 step implementations (lazy-imported only by `apply`, `verify`, and `secrets`).

The top-level `ansible/` directory is an unused documentation placeholder retained from the initial repository scaffold; all deployment logic lives in `src/openstack_forge/execution/`.

## CLI commands

The CLI provides the following commands:

- `openstack-forge init [--output PATH] [--force]` - write a starter `cluster.yaml` configuration file (using RFC 5737 documentation subnets with `controller`, `compute`, and `block` nodes) and validate it (implemented; refuses symlinks and existing files unless `--force` is given).
- `openstack-forge config validate [PATH]` - validate a cluster YAML file against the schema and semantic rules (implemented).
- `openstack-forge doctor --config PATH [--node HOSTNAME] [--json]` - run read-only local host preflight checks against the cluster configuration (implemented; see [`docs/doctor/README.md`](docs/doctor/README.md)).
- `openstack-forge plan --config PATH [--node HOSTNAME] [--json]` - show the ordered deployment steps each node would go through, without running anything (implemented, read-only; every step is currently unverified; see [`docs/plan/README.md`](docs/plan/README.md)).
- `openstack-forge apply --config PATH (--step STEP_ID | --all) [--node HOSTNAME] [--root PATH] [--yes]` - apply either a single implemented step (`--step STEP_ID`) or all planned catalog steps for the local node in topological order (`--all`): `hosts-file`, `time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`, `keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`, `neutron-controller`, `neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage`. All eighteen remain marked **unverified** in the step catalog and none has ever run on a real host (dry-run by default; writes, acquires `<root>/var/lib/openstack-forge/apply.lock`, and runs commands only with `--yes`; see [`docs/engine/README.md`](docs/engine/README.md)).
- `openstack-forge verify --config PATH [--step STEP_ID | --all] [--node HOSTNAME] [--root PATH] [--json]` - run read-only post-deployment verification (`CommandExecutor(..., read_only=True)`) for a single step or all planned steps on the local node, reporting recorded state from `state.json` and `PASS`/`FAIL` problem details in human-readable text or `--json` without modifying files, running mutating commands, or writing to `audit.log` or `state.json` (implemented; see [`docs/engine/README.md`](docs/engine/README.md)).
- `openstack-forge secrets init [--root PATH] [--file PATH]` - create missing OpenStack passwords in `<root>/etc/openstack-forge/secrets.json` (directory mode `0700`, file mode `0600`, never overwriting existing values) and print **secret names only**, never values (implemented; never run on a real host; see [`docs/engine/README.md`](docs/engine/README.md)).
- `openstack-forge secrets list [--root PATH] [--file PATH]` - list all known secret names in canonical `SECRET_SPECS` order along with their `present` or `missing` status and description (never printing secret values).
- `openstack-forge secrets path [--root PATH] [--file PATH]` - print the resolved filesystem path to `<root>/etc/openstack-forge/secrets.json` without reading or creating the file.
- `openstack-forge secrets show NAME [--root PATH] [--file PATH]` - print one secret value from the local `0600` store to `stdout` and nothing else (implemented; never run on a real host).

`apply` and `secrets` never use `sudo`, and multi-node secret distribution is not implemented (manual copy for now). OpenStack Forge's Python code opens no network connection, but the commands that `apply --yes` runs for `time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`, `memcached`, `etcd`, `keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`, `neutron-controller`, `neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage` (`apt-get update`, `apt-get install`, `add-apt-repository`) download from the node's apt sources and the Ubuntu Cloud Archive, so they use the network. `init`, `config validate`, `doctor`, `plan`, `verify`, `secrets`, and `hosts-file` open no external network repository connections.

## Implemented 18-Step OpenStack 2025.1 (Epoxy) Catalog

Every step implements a deterministic `check()`, `preview()`, `apply()`, and `verify()` lifecycle confined under `--root`:

| # | Step ID | Category | Target Roles | Summary |
|---|---------|----------|--------------|---------|
| 1 | `hosts-file` | environment | `controller`, `compute`, `block` | Render `/etc/hosts` entries for all cluster nodes on the management network |
| 2 | `time-sync-controller` | environment | `controller` | Install and configure `chrony` on the controller node |
| 3 | `time-sync-other` | environment | `compute`, `block` | Configure `chrony` on non-controller nodes to sync with the controller |
| 4 | `openstack-packages` | environment | `controller`, `compute`, `block` | Enable the Ubuntu Cloud Archive (`cloud-archive:epoxy`) and install `python3-openstackclient` |
| 5 | `mariadb` | environment | `controller` | Install, configure (`99-openstack.cnf`), and harden MariaDB on the management IP |
| 6 | `rabbitmq` | environment | `controller` | Install RabbitMQ and configure the `openstack` message broker user and permissions |
| 7 | `memcached` | environment | `controller` | Install and bind Memcached to the controller management IP |
| 8 | `etcd` | environment | `controller` | Install and configure `etcd` on the controller management IP |
| 9 | `keystone` | core-service | `controller` | Bootstrap the Keystone Identity service, database, Fernet tokens, and Apache WSGI |
| 10 | `glance` | core-service | `controller` | Deploy the Glance Image service, database, Keystone endpoints, and local filesystem store |
| 11 | `placement` | core-service | `controller` | Deploy the Placement API service, database, Keystone endpoints, and Apache WSGI |
| 12 | `nova-controller` | core-service | `controller` | Deploy Nova controller services (`nova-api`, `nova-scheduler`, `nova-conductor`, `nova-novncproxy`) and Cells v2 |
| 13 | `nova-compute` | core-service | `compute` | Deploy `nova-compute` (KVM/QEMU via libvirt) and trigger `nova-manage cell_v2 discover_hosts` |
| 14 | `neutron-controller` | core-service | `controller` | Deploy Neutron server, ML2 plugin, L3/DHCP/metadata agents, and Open vSwitch bridge mappings |
| 15 | `neutron-compute` | core-service | `compute` | Deploy Neutron Open vSwitch agent and OVS provider bridge on compute nodes |
| 16 | `horizon` | dashboard | `controller` | Deploy the Horizon web dashboard under Apache (`local_settings.py`) |
| 17 | `cinder-controller` | optional-service | `controller` | Deploy `cinder-api` and `cinder-scheduler` with Keystone `v3` volume endpoints |
| 18 | `cinder-storage` | optional-service | `block` | Provision LVM physical/volume groups (`cinder-volumes`) and deploy `cinder-volume` + `tgt` |

## Quickstart workflow

```bash
# 1. Generate a starter cluster.yaml (uses RFC 5737 documentation subnets)
openstack-forge init --output cluster.yaml

# 2. Validate schema and network/topology semantics
openstack-forge config validate cluster.yaml

# 3. Run read-only host preflight diagnostics for a node
openstack-forge doctor --config cluster.yaml --node ctrl-01

# 4. Inspect the ordered multi-node deployment DAG
openstack-forge plan --config cluster.yaml

# 5. Initialize the 0600 secrets store in a staging root and preview changes (dry-run)
openstack-forge secrets init --root /tmp/forge-preview
openstack-forge apply --config cluster.yaml --node ctrl-01 --root /tmp/forge-preview --all

# 6. Run read-only post-deployment verification
openstack-forge verify --config cluster.yaml --node ctrl-01 --root /tmp/forge-preview --all
```

## Repository structure

The repository is organized to keep project concerns separate:

- `ansible/` - unused documentation placeholders from the initial repository scaffold (deployment uses `src/openstack_forge/execution/`).
- `docs/` - architecture, installation, configuration, doctor, plan, engine, security, and troubleshooting documentation.
- `examples/` - example deployment configuration (`examples/cluster.example.yaml`).
- `src/openstack_forge/` - Python package with `cli`, `config`, `diagnostics`, `orchestration`, and `execution` modules. The configuration JSON Schema lives in `src/openstack_forge/config/schemas/`.
- `tests/` - unit, integration, and acceptance test suites plus recorded fixtures.

## Development setup

Requirements:

- Python 3.10+
- `pip`
- `venv` support

Clone the repository and install the project in editable mode with development dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run tests and linting

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

A convenience target is also available through the project `Makefile`:

- `make install`
- `make test`
- `make lint`
- `make format`
- `make check`

## Security principles

- Never commit secrets, credentials, tokens, SSH private keys, or real infrastructure IP addresses (enforced by automated test checks).
- Confine all filesystem reads/writes under `--root` with ancestor symlink rejection and `O_NOFOLLOW` atomic replacement.
- Execute external commands exclusively through a fixed 49-command allowlist (`DEFAULT_REGISTRY`) with `shell=False`, sanitized environment variables, and secret delivery via `stdin` or `0600` files (never in process `argv`).
- Redact secret values automatically across all diffs, audit logs, state files, and CLI output.

## Roadmap

- Live validation and end-to-end convergence testing on Ubuntu 24.04 multi-node KVM/bare-metal environments to graduate catalog steps from `unverified` to `verified`.
- Automated post-deployment smoke testing (`launch-an-instance` verification workflow).
- Secure multi-node secret distribution and remote node orchestration over SSH.
- Optional OpenStack Object Storage (Swift) catalog steps.

## Contributing

Contributions are welcome. Please review `CONTRIBUTING.md` for development workflow, code quality standards, and contribution expectations.

## License

This project is licensed under the Apache License 2.0. See `LICENSE` for details.
