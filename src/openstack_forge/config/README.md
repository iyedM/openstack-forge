# Configuration package

Loads and validates YAML cluster definitions.

## Modules

- `loader.py` - reads the file and parses YAML safely (rejects duplicate keys).
- `schema.py` - validates structure against the bundled `schemas/cluster.schema.json`
  and turns JSON Schema errors into readable messages.
- `semantic.py` - checks rules the schema cannot express (supported releases, role
  counts, uniqueness, subnet and range arithmetic, cross-references). It assumes
  schema-valid input.
- `releases.py` - the supported Ubuntu / OpenStack release table.
- `validator.py` - `load_config()` / `validate_config()` combine the stages.
- `errors.py` - `ConfigError` and its subclasses, plus `ValidationIssue`.
- `schemas/cluster.schema.json` - the JSON Schema, shipped as package data.

See [`docs/configuration`](../../../docs/configuration/README.md) for the full list
of rules. No deployment or provisioning logic lives here.
