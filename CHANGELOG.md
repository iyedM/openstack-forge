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
