# Contributing

Thank you for your interest in contributing to OpenStack Forge.

## Development workflow

1. Create a feature branch from the default branch.
2. Keep changes focused and reviewable.
3. Add or update tests for behavior changes.
4. Run linting and test checks before submitting a pull request.
5. Update documentation when project behavior changes.

## Code quality expectations

- Follow PEP 8 and project Ruff configuration.
- Use type hints for public Python functions and classes.
- Keep business logic separate from the CLI layer.
- Avoid circular dependencies and unnecessary abstractions.
- Never commit secrets, credentials, private keys, or actual infrastructure addresses.

## Local validation

Before opening a pull request, run:

- python -m pytest
- python -m ruff check .
- python -m ruff format --check .

If a change affects the example configuration or schema, validate the YAML against the project schema as part of the review.

## Pull requests

- Provide a brief summary of the change.
- Explain why the change is needed.
- Link related issues where appropriate.
- Keep the description factual and specific.

## Community standards

Please keep discussions respectful and constructive. We expect all contributors to follow the repository's Code of Conduct.
