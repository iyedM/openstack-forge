# Orchestration package

Read-only deployment planning. Nothing here runs commands, connects to hosts or changes
any system.

## Modules

- `catalog.py` - loads the bundled step catalog and validates it against its JSON Schema
  (structure) and its own rules (unique ids, existing dependencies, role-scoped
  dependencies limited to the step's roles, no duplicate dependency, no cycles).
- `plan.py` - `build_plan()` orders the catalog steps for each node of a validated
  configuration, resolving dependencies across nodes (only those that apply to the
  node's role); `format_plan_text()` and `format_plan_json()` render it.
- `errors.py` - `OrchestrationError`, `CatalogError` and `PlanError`.
- `data/steps.json` - the step catalog, shipped as package data.
- `schemas/steps.schema.json` - the catalog's JSON Schema, shipped as package data.

See [`docs/plan`](../../../docs/plan/README.md) for the plan format and for when a step
may be marked as verified.
