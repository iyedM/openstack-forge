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

### Fixed

- Validation now works from a regular (non-editable) install; previously the schema
  was only found in a source checkout.
