# OpenStack Forge

OpenStack Forge is an early-stage toolkit for automating the deployment, configuration, enrollment, and validation of multi-node OpenStack private clouds on Ubuntu servers.

## Current status

This project is in early development. It is not production-ready and does not yet implement OpenStack installation, secure node enrollment, or complete infrastructure deployment workflows.

## Goals

- Provide a clear project structure for future OpenStack automation workflows.
- Model a multi-node cluster topology using a YAML configuration format.
- Establish configuration validation and preflight checks before deployment.
- Offer a Python CLI for initialization, plan generation, diagnostics, and validation.
- Keep the architecture modular enough for future Ansible orchestration.

## Non-goals

- Installing OpenStack from scratch in this initial repository snapshot.
- Performing remote node enrollment, authentication, or infrastructure modifications.
- Hardcoding credentials, secrets, or real deployment IPs in examples or documentation.
- Claiming production readiness before validation and testing are established.

## High-level architecture

The project is intentionally designed around a few clear layers:

- CLI layer: user-facing commands for initialization, validation, diagnostics, and planning.
- Configuration layer: YAML-based cluster definitions and schema validation.
- Inventory layer: node topology, roles, networks, and interfaces.
- Ansible layer: infrastructure orchestration and role-based configuration.
- Validation and diagnostics layer: preflight checks, health reporting, and planning feedback.

## Planned CLI commands

The CLI currently provides a scaffold for the following commands:

- `openstack-forge init` - initialize a new deployment workspace.
- `openstack-forge config validate [PATH]` - validate a cluster YAML file against the schema and semantic rules (implemented).
- `openstack-forge doctor` - perform environment diagnostics and preflight checks.
- `openstack-forge plan` - produce a deployment plan without making changes.

Apart from `config validate`, the implementation of these workflows is intentionally deferred.

## Repository structure

The repository is organized to keep project concerns separate:

- `ansible/` - Ansible inventories, playbooks, and role documentation.
- `docs/` - architecture, installation, configuration, security, and troubleshooting docs.
- `examples/` - example deployment configuration.
- `src/openstack_forge/` - Python package with CLI, validation, orchestration, and diagnostics modules. The configuration JSON Schema lives in `src/openstack_forge/config/schemas/`.
- `tests/` - unit, integration, acceptance, and fixtures placeholders.

## Development setup

Requirements:

- Python 3.10+
- `pip`
- `venv` support

Clone the repository and install the project in editable mode with development dependencies:

- python3 -m venv .venv
- . .venv/bin/activate
- python -m pip install --upgrade pip
- python -m pip install -e ".[dev]"

## Run tests and linting

- pytest
- ruff check .
- ruff format --check .

A convenience target is also available through the project `Makefile`:

- make install
- make test
- make lint
- make format
- make check

## Security principles

- Never commit secrets, credentials, tokens, SSH private keys, or real infrastructure IP addresses.
- Keep configuration validation explicit and schema-driven.
- Avoid unsafe automation by requiring explicit, user-reviewed actions before infrastructure changes.
- Keep the project modular and auditable so future security controls can be layered in cleanly.

## Roadmap

- Initial repository skeleton and configuration scaffolding.
- Example cluster model and schema draft.
- CLI foundations and command structure.
- Validation and preflight checking for YAML inputs.
- Ansible role and inventory structure.
- Secure token-based enrollment workflows.
- Deployment planning and diagnostics for multi-node OpenStack clusters.
- Full automation workflows for Ubuntu-based OpenStack distributions.

## Contributing

Contributions are welcome. Please review `CONTRIBUTING.md` for development workflow, code quality standards, and contribution expectations.

## License

This project is licensed under the Apache License 2.0. See `LICENSE` for details.
