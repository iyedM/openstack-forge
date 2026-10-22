# Architecture

This directory captures the high-level design of OpenStack Forge.

## Package layers

- **`openstack_forge.config`**: YAML parsing (`SafeLoader` with duplicate-key detection), JSON Schema validation (`cluster.schema.json`), and semantic network/role validation.
- **`openstack_forge.diagnostics`**: read-only host preflight checks (`openstack-forge doctor`) comparing `/etc/os-release`, `nproc`, `/proc/meminfo`, `/proc/cpuinfo`, `systemd-detect-virt`, `lsblk`, and `ip -j addr` (including provider interface link state) against the declared node configuration.
- **`openstack_forge.orchestration`**: the 18-step deployment catalog (`steps.json`) and deterministic multi-node dependency planner (`openstack-forge plan`).
- **`openstack_forge.execution`**: confined file primitives, `SecretStore` (`/etc/openstack-forge/secrets.json`), `StateStore` (`/var/lib/openstack-forge/state.json`) and `apply_lock` (`/var/lib/openstack-forge/apply.lock`), `Engine`, `CommandExecutor` (`DEFAULT_REGISTRY` with 49 reviewed commands), and the 18 step implementations (`hosts-file` through `cinder-storage`). Imported lazily only by `apply`, `verify`, and `secrets`.

The top-level `ansible/` directory is an unused placeholder from the initial scaffold.

## Current status

All 18 catalog steps are implemented in `openstack_forge.execution` and tested against a fake backend and temporary root directories, but **all 18 steps remain marked `unverified` in the step catalog and none has ever been run on a real host.** See [`docs/engine/README.md`](../engine/README.md) and [`docs/plan/README.md`](../plan/README.md).
