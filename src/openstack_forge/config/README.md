# Configuration package

Loads and validates YAML cluster definitions.

## Modules

- `loader.py` - reads the file and parses YAML safely (rejects duplicate keys).
- `schema.py` - validates structure against `schemas/cluster.schema.json` and turns
  JSON Schema errors into readable messages.
- `semantic.py` - checks rules the schema cannot express (uniqueness, subnet
  arithmetic, cross-references). It assumes schema-valid input.
- `validator.py` - `load_config()` / `validate_config()` combine the stages.
- `errors.py` - `ConfigError` and its subclasses, plus `ValidationIssue`.

See [`docs/configuration`](../../../docs/configuration/README.md) for the full list
of rules. No deployment or provisioning logic lives here.
