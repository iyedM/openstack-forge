# Security Policy

## Supported versions

This repository is in early development and does not yet provide a production support matrix. Security issues should still be treated seriously.

## Reporting a vulnerability

Please report vulnerabilities privately and responsibly. Do not disclose the issue publicly before the maintainers have had a reasonable chance to assess and address it.

The preferred path is to use GitHub's private security reporting workflow, if available for this repository. If not available, use the repository's maintainers' designated private contact process or open a confidential coordination channel that is not broadly public.

Please include:

- A concise description of the vulnerability.
- Affected version or branch if known.
- Steps to reproduce the issue.
- Potential impact and severity.
- Suggested mitigation or remediation ideas.

Do not include secrets, credentials, private keys, or sensitive deployment details in public issues or discussion threads.

## Disclosure policy

We ask that reporters allow a reasonable response window for investigation and remediation before disclosing details publicly.

## Security principles

- Do not ship secrets or credentials in examples or fixture data.
- Prefer explicit, reviewable automation paths.
- Validate configuration and deployment inputs before any infrastructure changes are attempted.
