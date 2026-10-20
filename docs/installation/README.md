# Installation

This section will describe installation and bootstrap procedures for the project itself.

## Current status

No deployment instructions are included yet because the project is still in an early prototype phase.

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
