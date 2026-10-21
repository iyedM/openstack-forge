# Deployment plan (`openstack-forge plan`)

`openstack-forge plan` shows, for a validated cluster configuration, the ordered steps
each node would go through to prepare an OpenStack 2025.1 (Epoxy) deployment.

It is strictly read-only. It reads the configuration file and the step catalog bundled
with the package, and nothing else: it runs no command, opens no network connection,
connects to no node and changes nothing. In `openstack-forge apply`, three steps are
implemented: `hosts-file` (confined file operations only, no external command, no
network), `time-sync-controller` and `openstack-packages`. The last two run reviewed
system commands; OpenStack Forge's Python code opens no connection, but the apt commands
they run (`apt-get update`, `apt-get install`, `add-apt-repository`) use the network. All
three remain marked **unverified** in the catalog and none has been run on a real host;
see [`docs/engine/README.md`](../engine/README.md).

## Usage

```sh
openstack-forge plan --config cluster.yaml [--node HOSTNAME] [--json]
```

| Option | Meaning |
| ------ | ------- |
| `--config PATH` | Cluster configuration (default: `cluster.yaml`). Validated with the same validator as `config validate` before planning. |
| `--node HOSTNAME` | Only show the steps for this node (matched case-insensitively against `nodes[].hostname`). |
| `--json` | Emit the plan as JSON instead of text. |

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0    | The configuration is valid and the plan was printed. Unverified steps do not change the exit code. |
| 1    | The configuration is missing or invalid, `--node` names an unknown node, or the bundled catalog is invalid. |
| 2    | Command-line argument parsing error. |

## Reading the plan

Without `--node`, the plan lists every step on every node as one numbered sequence for
the whole cluster. Each entry shows:

- the sequence number, node, role and step id;
- the step title;
- `files`: the files the step would modify (`(none listed)` if the catalog lists none);
- `verification`: how a person can tell the step succeeded;
- `docs`: the official documentation page the step is based on;
- `waits for`: every step instance that must be finished first, as `<step> on <node>`;
- `[UNVERIFIED]` when the step has not been verified (see below).

With `--node`, only that node's entries are shown, but they keep their cluster-wide
sequence numbers and still show `waits for` entries on other nodes, so you can see where
the node's work fits in.

The summary counts the steps, nodes, verified and unverified steps, and a warning at
the top repeats the number of unverified steps.

### Ordering rules

- A step runs on every node whose role is listed in the step's `roles`.
- For each id in `depends_on`:
  - if the node's role also runs that step, the node waits for **its own** instance of it;
  - otherwise the node waits for that step on **every node that runs it** (for example,
    a compute step that depends on a controller-only step waits for it on the controller);
  - if no node in the cluster runs it (for example, an optional `object` role with no
    object node), the plan says so: `waits for <step> (not run by any node in this cluster)`.
- Among steps that are ready at the same time, catalog order comes first, then role
  (controller, compute, block, object), then the node's position in the configuration.
  The order is therefore deterministic, and controller steps come before the same step
  on other nodes.

## The step catalog

The catalog is data, not code. It ships inside the package as
[`src/openstack_forge/orchestration/data/steps.json`](../../src/openstack_forge/orchestration/data/steps.json)
and is validated against
[`schemas/steps.schema.json`](../../src/openstack_forge/orchestration/schemas/steps.schema.json)
every time it is loaded. Each step has exactly these fields:

| Field | Rule |
| ----- | ---- |
| `id` | Lowercase words separated by hyphens; unique in the catalog. |
| `title` | Short description. |
| `roles` | Non-empty subset of `controller`, `compute`, `block`, `object` (the configuration roles). |
| `depends_on` | Ids of other catalog steps, possibly for other roles. Must exist and must not form a cycle. |
| `files_modified` | Absolute paths of files the step would modify. May be empty. |
| `verification` | Human description of how to confirm the step worked. |
| `doc_url` | A page under `https://docs.openstack.org/`. |
| `status` | `verified` or `unverified`. |

Steps deliberately have no field for commands or file contents, and the schema rejects
unknown fields.

### Current content

All steps are **unverified**.

| Step | Roles | Files modified | Documentation |
| ---- | ----- | -------------- | ------------- |
| `hosts-file` | all | `/etc/hosts` | [Host networking](https://docs.openstack.org/install-guide/environment-networking.html) |
| `time-sync-controller` (chrony) | controller | `/etc/chrony/chrony.conf` | [NTP: controller node](https://docs.openstack.org/install-guide/environment-ntp-controller.html) |
| `time-sync-other` (chrony) | compute, block, object | `/etc/chrony/chrony.conf` | [NTP: other nodes](https://docs.openstack.org/install-guide/environment-ntp-other.html) |
| `openstack-packages` (enable `cloud-archive:epoxy`, install `python3-openstackclient`) | all | none listed | [OpenStack packages for Ubuntu](https://docs.openstack.org/install-guide/environment-packages-ubuntu.html) |
| `mariadb` | controller | `/etc/mysql/mariadb.conf.d/99-openstack.cnf` | [SQL database for Ubuntu](https://docs.openstack.org/install-guide/environment-sql-database-ubuntu.html) |
| `rabbitmq` | controller | none listed | [Message queue for Ubuntu](https://docs.openstack.org/install-guide/environment-messaging-ubuntu.html) |
| `memcached` | controller | `/etc/memcached.conf` | [Memcached for Ubuntu](https://docs.openstack.org/install-guide/environment-memcached-ubuntu.html) |
| `etcd` | controller | `/etc/default/etcd` | [Etcd for Ubuntu](https://docs.openstack.org/install-guide/environment-etcd-ubuntu.html) |

The `depends_on` chain follows the order of the "Environment" chapter of the installation
guide and is itself unverified:

- `hosts-file` → `time-sync-controller` → `openstack-packages` → each controller service;
- `time-sync-other` (on compute, block and object nodes) waits for `time-sync-controller`
  on the controller, because those nodes synchronize their clocks from it.

`openstack-packages` depends on `time-sync-controller` only. On the controller this is a
local wait; on every other node it is a wait for the controller's `time-sync-controller`.
**Non-controller nodes therefore do not wait for their own `time-sync-other` before
`openstack-packages`.** A step's `depends_on` applies to all of its roles, so making
`openstack-packages` depend on `time-sync-other` would leave the controller (which never
runs `time-sync-other`) waiting for a step it does not run. Supporting per-role
dependencies (for example `depends_on` keyed by role) is future work.

`time-sync-controller` and `openstack-packages` are implemented by `openstack-forge apply`
(see [`docs/engine`](../engine/README.md)); `time-sync-other` is not, and its documentation
page has not been read yet. `openstack-packages` takes its Ubuntu Cloud Archive pocket from
the supported release table, not from the catalog title.

### Open decision: network configuration

The catalog contains a note, not a step, about host networking. The official guide's
[controller networking page](https://docs.openstack.org/install-guide/environment-networking-controller.html)
says to edit `/etc/network/interfaces` on Ubuntu, but Ubuntu 24.04 configures networking
with netplan. How OpenStack Forge will handle network configuration has not been
decided, so no step modifies network configuration. `plan` prints this under
"Open decisions (not steps)".

## When a step becomes "verified"

`unverified` is the default and means only that the step is a reasonable outline taken
from the guide's structure. A step may be changed to `"status": "verified"` only after
someone has:

1. opened the official OpenStack 2025.1 documentation page for that step;
2. compared every field of the step with it: roles, dependencies, files modified and
   the verification description;
3. corrected anything that does not match;
4. recorded the exact page they compared against in `doc_url`.

The `doc_url` must be the page actually used for the comparison. Project guides have
release-specific URLs (for example `https://docs.openstack.org/keystone/2025.1/...`),
and a release-specific URL should be used whenever one exists. The installation guide's
"Environment" pages used by the current steps are published at a single unversioned
location (`https://docs.openstack.org/install-guide/...`), so whoever verifies a step
based on them must confirm the page applies to 2025.1 and say so when making the change.

The verification itself (who compared which page, and when) belongs in the pull request
or commit that changes the status. Nothing in the code can confirm a step against the
documentation, so `plan` never treats a step as verified unless the catalog says so.
