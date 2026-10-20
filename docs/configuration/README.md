# Configuration

This section documents the cluster configuration model and how it is validated.

## Current status

The YAML format is still a draft and will evolve. It is described by
[`schemas/cluster.schema.json`](../../schemas/cluster.schema.json), and
[`examples/cluster.example.yaml`](../../examples/cluster.example.yaml) is a complete example.

## Validating a configuration

```sh
openstack-forge config validate examples/cluster.example.yaml
```

If no path is given, `cluster.yaml` in the current directory is used. Exit codes:

| Code | Meaning |
| ---- | ------- |
| 0    | The configuration is valid. |
| 1    | The file is missing or unreadable, is not valid YAML, or fails validation. |
| 2    | The command line itself is invalid (argument parsing error). |

Validation output never echoes raw YAML source lines, so a malformed line that
contains a secret is not printed.

## Validation stages

Validation runs in three stages. Each stage only runs if the previous one succeeded.

1. **Loading.** The file is read as UTF-8 and parsed with a PyYAML `SafeLoader`
   (no arbitrary Python object construction). Empty files, multiple YAML
   documents and duplicate mapping keys are rejected; standard YAML parsers would
   otherwise silently keep the last duplicate key.
2. **Schema validation.** The data is checked against the JSON Schema (draft
   2020-12), including the `ipv4` format. All schema errors are reported at once,
   each with its location (for example `nodes[1].role`).
3. **Semantic validation.** Rules that JSON Schema cannot express. These assume
   the structure is already valid and do not repeat schema rules.

### Semantic rules

| Rule | Example error |
| ---- | ------------- |
| `networks.<name>.subnet` must be a real IPv4 network with no host bits set. | `networks.tenant.subnet: invalid IPv4 network: 203.0.113.5/24 has host bits set` |
| A network's `gateway` must be a usable host address inside its subnet. | `networks.management.gateway: 198.51.100.1 is outside subnet 192.0.2.0/24` |
| Network subnets must not overlap. | `networks.storage.subnet: 192.0.2.128/25 overlaps network 'management' (192.0.2.0/24)` |
| `hostname`, `fqdn` and `management_ip` must each be unique across nodes. Names are compared case-insensitively. | `nodes[1].hostname: duplicate hostname 'CMP-01'; already used by nodes[0]` |
| The first label of a node's `fqdn` must equal its `hostname`. | `nodes[0].fqdn: 'ctl-02.cluster.test' does not start with hostname 'ctl-01'` |
| Every key in `nodes[].interfaces` must name a network defined under `networks`. | `nodes[0].interfaces.provider: refers to undefined network 'provider' (defined networks: management, storage)` |
| If a network named `management` exists, each `management_ip` must be a usable host address in it and must not be its gateway. | `nodes[0].management_ip: 203.0.113.10 is outside subnet 192.0.2.0/24 (the 'management' network)` |

For `/31` and `/32` subnets every address is usable (RFC 3021), so the
network/broadcast address checks are skipped.

## Schema changes in this milestone

- `nodes[].hostname` must now be a single DNS label (RFC 1123: letters, digits
  and inner hyphens, at most 63 characters).
- `nodes[].fqdn` must now be a dotted name with at least two such labels.

**Reason:** these values become system host names and DNS records, so values
with spaces, underscores or leading hyphens would only fail later during
deployment. The pattern is used instead of JSON Schema's `hostname` format,
because `jsonschema` checks that format only when the optional `fqdn` package
is installed. The example configuration already conforms.

`description` fields were also added to patterned properties so that errors can
explain the expected format. They do not change what is accepted.

## Known limitations

- The schema is read from `schemas/cluster.schema.json` in the source tree, so
  validation works from a checkout or an editable install (`pip install -e .`)
  but not from a regular wheel install. Packaging the schema is future work.
- The relationship between `networks.<name>.interface` and the per-node
  `nodes[].interfaces` mapping is not yet defined, so they are not cross-checked.
- Node roles are limited to `controller`, `compute` and `storage`; no rule yet
  requires any particular role to be present.
