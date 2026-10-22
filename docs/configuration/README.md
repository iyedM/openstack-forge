# Configuration

This section documents the cluster configuration model and how it is validated.

## Current status

The YAML format is still a draft and will evolve. Its structure is defined by the JSON
Schema [`cluster.schema.json`](../../src/openstack_forge/config/schemas/cluster.schema.json),
which ships inside the Python package. [`examples/cluster.example.yaml`](../../examples/cluster.example.yaml)
is a complete, valid example.

## Validating a configuration

```sh
openstack-forge config validate examples/cluster.example.yaml
```

If no path is given, `cluster.yaml` in the current directory is used. A valid file prints
a summary including the OpenStack release and the Ubuntu Cloud Archive repository it
will be installed from. Exit codes:

| Code | Meaning |
| ---- | ------- |
| 0    | The configuration is valid. |
| 1    | The file is missing or unreadable, is not valid YAML, or fails validation. |
| 2    | The command line itself is invalid (argument parsing error). |

Validation output never echoes raw YAML source lines, so a malformed line that
contains a secret is not printed.

## Configuration model

### `cluster`

| Field | Rule |
| ----- | ---- |
| `name` | Non-empty string. |
| `ubuntu_version` | `"24.04"`, the only supported Ubuntu release. Quote it: unquoted `24.04` is read as a number. |
| `openstack_release` | `"2025.1"` (Epoxy), the only supported OpenStack release. |
| `topology` | Non-empty string. |
| `ntp_servers` | Optional. List of at least one hostname, FQDN or IPv4 address, with no duplicates (compared case-insensitively). When absent, the controller keeps the distribution's default NTP pool. |

Supported releases come from a table in
[`releases.py`](../../src/openstack_forge/config/releases.py):

| Ubuntu | OpenStack | Codename | apt repository |
| ------ | --------- | -------- | -------------- |
| 24.04  | 2025.1    | Epoxy    | `cloud-archive:epoxy` |

Ubuntu 24.04's own archive ships OpenStack 2024.1 (Caracal); Epoxy is installed from the
Ubuntu Cloud Archive, enabled with `add-apt-repository cloud-archive:epoxy`. Supporting
another pair is a one-line addition to `SUPPORTED_RELEASES`. The schema only checks that
both values look like version numbers (`NN.NN` and `YYYY.N`).

### `networks`

A mapping of network name to network. Every network has `subnet` (IPv4 CIDR),
`gateway` (IPv4), `interface` (non-empty) and optional `dns` (list of IPv4).

`provider` is **required**. It is the external network that instances reach through
floating IPs, and it additionally has:

| Field | Rule |
| ----- | ---- |
| `floating_ip_range` | Required. `{start, end}` IPv4 addresses. |
| `dhcp_range` | Optional. `{start, end}` IPv4 addresses served by DHCP on this network. |

These range fields are only allowed on `provider`.

A network named `management` is optional, but when present it is used to check node
management addresses (see below).

### `nodes`

A non-empty list. Every node has:

| Field | Rule |
| ----- | ---- |
| `role` | One of `controller`, `compute`, `block`, `object`. |
| `hostname` | A single DNS label (RFC 1123), at most 63 characters. |
| `fqdn` | A dotted DNS name whose first label is the hostname. |
| `management_ip` | IPv4 address. |
| `vcpus` | Integer >= 1: the number of CPUs the node is expected to have. |
| `ram_gb` | Integer >= 1: the memory the node is expected to have, in GB. |
| `interfaces` | Mapping of network name to interface name, at least one entry. |
| `tags` | Optional list of strings. |
| `allow_shared_provider_interface` | Optional boolean (`default: false`). Explicit opt-in override allowing `neutron-controller` and `neutron-compute` to attach the provider interface to `br-provider` even when it equals the management interface or carries `management_ip`. |
| `allow_lvm_root_filter_override` | Optional boolean (`default: false`). Explicit opt-in override allowing `cinder-storage` to write the single-disk LVM filter to `/etc/lvm/lvm.conf` even when the block node's root filesystem `/` is detected on LVM. |

`vcpus` and `ram_gb` are declarations of expected host capacity; `openstack-forge doctor`
compares them against the local machine (see [`docs/doctor/README.md`](../doctor/README.md)).
The operator must bring each node's provider network interface link `UP` (`LOWER_UP`) on the
host (for example via netplan) before deployment; OpenStack Forge never runs `netplan` or
`ip link` commands.

Disks depend on the role:

| Role | Disk field |
| ---- | ---------- |
| `block` (Cinder) | `disk`: required, one device path, e.g. `/dev/sdb`. |
| `object` (Swift) | `disks`: required, a list of at least one device path, no duplicates. |
| `controller`, `compute` | Neither `disk` nor `disks` is allowed. |

A device path must be absolute, under `/dev/`, and contain no `.` or `..` segments.
Stable paths such as `/dev/disk/by-id/...` are accepted and recommended, since
`/dev/sdX` names can change between boots.

## Validation stages

Validation runs in three stages. Each stage only runs if the previous one succeeded.

1. **Loading.** The file is read as UTF-8 and parsed with a PyYAML `SafeLoader`
   (no arbitrary Python object construction). Empty files, multiple YAML
   documents and duplicate mapping keys are rejected; standard YAML parsers would
   otherwise silently keep the last duplicate key.
2. **Schema validation.** The data is checked against the JSON Schema (draft
   2020-12), including the `ipv4` format and the role-dependent disk fields. All schema
   errors are reported at once, each with its location (for example `nodes[1].role`).
3. **Semantic validation.** Rules that JSON Schema cannot express. These assume
   the structure is already valid and do not repeat schema rules.

### Semantic rules

| Rule | Example error |
| ---- | ------------- |
| `ubuntu_version` and `openstack_release` must be a supported pair. | `cluster.ubuntu_version: unsupported Ubuntu release '22.04'; supported: 24.04` |
| Exactly one `controller`, at least one `compute`, at most one `block` and at most one `object` node. | `nodes: requires at least 1 'compute' node; found 0` |
| `networks.<name>.subnet` must be a real IPv4 network with no host bits set. | `networks.tenant.subnet: invalid IPv4 network: 198.51.100.5/24 has host bits set` |
| A network's `gateway` must be a usable host address inside its subnet. | `networks.management.gateway: 198.51.100.1 is outside subnet 192.0.2.0/24` |
| Network subnets must not overlap, except that `provider` may use exactly the same subnet as `management`. | `networks.storage.subnet: 192.0.2.128/25 overlaps network 'management' (192.0.2.0/24)` |
| Each provider range must have `start <= end`, and both ends must be usable host addresses in the provider subnet. | `networks.provider.floating_ip_range: start 203.0.113.199 is after end 203.0.113.100` |
| Provider ranges must not include the provider gateway or any node's `management_ip`. | `networks.provider.floating_ip_range: 192.0.2.1-192.0.2.30 includes nodes[0].management_ip 192.0.2.11` |
| `dhcp_range` must not overlap `floating_ip_range`. | `networks.provider.dhcp_range: 192.0.2.25-192.0.2.60 overlaps floating_ip_range 192.0.2.1-192.0.2.30` |
| `cluster.ntp_servers` entries made only of digits and dots must be valid IPv4 addresses, and entries must be unique case-insensitively. | `cluster.ntp_servers[1]: duplicate NTP server 'ntp.example'; already listed at cluster.ntp_servers[0]` |
| `hostname`, `fqdn` and `management_ip` must each be unique across nodes. Names are compared case-insensitively. | `nodes[2].hostname: duplicate hostname 'CMP-01'; already used by nodes[1]` |
| The first label of a node's `fqdn` must equal its `hostname`. | `nodes[0].fqdn: 'ctl-02.cluster.test' does not start with hostname 'ctl-01'` |
| Every key in `nodes[].interfaces` must name a network defined under `networks`. | `nodes[0].interfaces.external: refers to undefined network 'external' (defined networks: ...)` |
| If a network named `management` exists, each `management_ip` must be a usable host address in it and must not be its gateway. | `nodes[0].management_ip: 203.0.113.10 is outside subnet 192.0.2.0/24 (the 'management' network)` |

For `/31` and `/32` subnets every address is usable (RFC 3021), so the
network/broadcast address checks are skipped. The gateway and node-address checks are
also applied to `dhcp_range`, because a static address inside a DHCP pool is a conflict.

## Changes to the public format

### Milestone 8

| Change | Reason |
| ------ | ------ |
| Optional `cluster.ntp_servers`: a non-empty list of hostnames, FQDNs or IPv4 addresses (format checked by the schema; IPv4 validity and case-insensitive duplicates by the semantic rules). | The `time-sync-controller` step writes `server <name> iburst` lines for them. The installation guide notes that by default the controller synchronizes with a pool of public servers; when the list is absent that default is kept. IPv6 addresses and trailing-dot names are not accepted. |

### Milestone 2

| Change | Reason |
| ------ | ------ |
| `ubuntu_version` must be `"24.04"`; the schema pattern no longer accepts point releases such as `"24.04.1"`. | Project decision to target a single LTS release. The point release does not change which packages are installed. |
| `openstack_release` must be `"2025.1"` and match `YYYY.N`. | Project decision to target Epoxy, the release available for 24.04 from `cloud-archive:epoxy`. |
| Role `storage` is replaced by `block` and `object`. | Cinder (block) and Swift (object) nodes need different services and disk layouts, which one generic role could not describe. |
| Role counts are enforced (see semantic rules). | The initial deployment model is a single controller; multiple block/object nodes are not supported yet. |
| `vcpus` and `ram_gb` are required on every node. | Declared capacity, so later checks can compare expectations with real hosts. |
| `disk` (block) and `disks` (object) are required by role and rejected elsewhere. | Storage services need to know which devices they may use; a disk on another role is almost certainly a mistake. |
| `networks.provider` is required, with `floating_ip_range` and optional `dhcp_range`. | Instances need an external network and a floating IP pool that cannot collide with infrastructure addresses. |
| `networks` no longer has `minProperties: 1`. | It is implied by the required `provider` network. |
| The schema moved from `schemas/cluster.schema.json` to `src/openstack_forge/config/schemas/cluster.schema.json`. | It ships as package data, so regular (non-editable) installs can validate. |

**Migrating a milestone 1 file:**

1. Set `ubuntu_version: "24.04"` and `openstack_release: "2025.1"`.
2. Change each `storage` node to `block` (adding `disk: "/dev/..."`) or `object`
   (adding `disks: [...]`).
3. Add `vcpus` and `ram_gb` to every node.
4. Add a `networks.provider` section with a `floating_ip_range`.

### Milestone 1

| Change | Reason |
| ------ | ------ |
| `hostname` must be a single RFC 1123 DNS label; `fqdn` a dotted name of such labels. | These values become host names and DNS records; invalid values would only fail later during deployment. A pattern is used because `jsonschema` only checks the `hostname` format when the optional `fqdn` package is installed. |
| `description` fields were added to patterned properties. | Lets error messages explain the expected format. They do not change what is accepted. |

## Known limitations

- The relationship between `networks.<name>.interface` and the per-node
  `nodes[].interfaces` mapping is not yet defined, so they are not cross-checked.
  Nodes are not required to have an interface on the provider network.
- `management_ip` is the only address declared per node, so it is the only node
  address checked against provider ranges.
- A provider network may share the management subnet only when the subnets are
  identical; the two gateways are not compared.
