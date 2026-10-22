# Installation

This section describes installing the `openstack-forge` CLI on Ubuntu 24.04 and the local per-node workflow.

## Current status

All 18 OpenStack 2025.1 (Epoxy) catalog steps are implemented in `openstack_forge.execution` (`apply` and `verify`), but **all 18 steps remain marked `unverified` in the step catalog and none has ever been run on a real host.** Review [`docs/engine/README.md`](../engine/README.md) and dry-run previews before running `apply --yes` on any machine.

## Installing the CLI on Ubuntu 24.04

The CLI needs Python 3.10+, `jsonschema` 4.10.3 or newer and `PyYAML` 6.0 or newer.
Ubuntu 24.04 packages both, so they can come from apt. Because Ubuntu marks the system
Python as externally managed (PEP 668), install OpenStack Forge itself into a virtual
environment:

```sh
sudo apt install python3-venv python3-jsonschema python3-yaml
python3 -m venv --system-site-packages ~/.venvs/openstack-forge
~/.venvs/openstack-forge/bin/pip install .
```

`pipx install .` also works; it installs the dependencies from PyPI instead of apt.

## Typical workflow (read-only first)

1. Generate a starter configuration: `openstack-forge init --output cluster.yaml`
2. Edit `cluster.yaml` for your nodes and validate it: `openstack-forge config validate cluster.yaml`
3. Bring each node's provider network interface link `UP` on the host (for example via netplan; OpenStack Forge never runs `netplan` or `ip link`) and run preflight diagnostics: `openstack-forge doctor --config cluster.yaml`
4. Inspect the cross-node execution plan: `openstack-forge plan --config cluster.yaml`
5. Preview changes in dry-run mode (writes nothing, runs nothing): `openstack-forge apply --config cluster.yaml --all`
6. Verify step state read-only: `openstack-forge verify --config cluster.yaml --all`
