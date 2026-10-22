# Deployment plan (`openstack-forge plan`)

`openstack-forge plan` shows, for a validated cluster configuration, the ordered steps
each node would go through to prepare an OpenStack 2025.1 (Epoxy) deployment.

It is strictly read-only. It reads the configuration file and the step catalog bundled
with the package, and nothing else: it runs no command, opens no network connection,
connects to no node and changes nothing. In `openstack-forge apply` (`--step STEP_ID` or
`--all`) and `openstack-forge verify` (`--step STEP_ID` or `--all`), all eighteen catalog
steps are implemented: `hosts-file` (confined file operations only, no external command,
no network), `time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`,
`rabbitmq`, `memcached`, `etcd`, `keystone`, `glance`, `placement`, `nova-controller`,
`nova-compute`, `neutron-controller`, `neutron-compute`, `horizon`, `cinder-controller`,
and `cinder-storage`. All but `hosts-file` run
reviewed system commands; OpenStack Forge's Python code opens no connection, but the apt
commands they run (`apt-get update`, `apt-get install`, `add-apt-repository`) use the
network. All eighteen remain marked **unverified** in the catalog and none has been run on a
real host; see [`docs/engine/README.md`](../engine/README.md).

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
- Each `depends_on` entry is either a step id, which applies on every node that runs the
  step, or a **role-scoped** entry `{"step": "<id>", "roles": [...]}`, which applies only
  on nodes whose role is listed, and is ignored on the others. Its roles must be among the
  step's own `roles`.
- For each dependency that applies on the node:
  - if the node's role also runs that step, the node waits for **its own** instance of it;
  - otherwise the node waits for that step on **every node that runs it** (for example,
    a compute step that depends on a controller-only step waits for it on the controller);
  - if no node in the cluster runs it (for example, an optional `object` role with no
    object node), the plan says so: `waits for <step> (not run by any node in this cluster)`.
- Role scopes exist for steps that run on several roles but must wait for different
  steps depending on the role. Without them, a plain id that the node's role does not run
  makes the node wait for it on every node that runs it: listing both
  `time-sync-controller` and `time-sync-other` as plain ids on `openstack-packages` would
  make the controller wait for every other node's `time-sync-other`.
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
| `depends_on` | Other catalog steps, possibly for other roles: each entry is a step id, or `{"step": "<id>", "roles": [...]}` to apply it only on those roles (a non-empty subset of the step's `roles`). Steps must exist, must not be listed twice and must not form a cycle (role-scoped entries included). |
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
| `keystone` | controller | `/etc/keystone/keystone.conf`, `/etc/apache2/apache2.conf`, `/etc/openstack-forge/admin-openrc` | [Keystone for Ubuntu (2025.1)](https://docs.openstack.org/keystone/2025.1/install/keystone-install-ubuntu.html) |
| `glance` | controller | `/etc/glance/glance-api.conf` | [Glance for Ubuntu (2025.1)](https://docs.openstack.org/glance/2025.1/install/install-ubuntu.html) |
| `placement` | controller | `/etc/placement/placement.conf` | [Placement for Ubuntu (2025.1)](https://docs.openstack.org/placement/2025.1/install/install-ubuntu.html) |
| `nova-controller` | controller | `/etc/nova/nova.conf` | [Nova controller for Ubuntu (2025.1)](https://docs.openstack.org/nova/2025.1/install/controller-install-ubuntu.html) |
| `nova-compute` | compute | `/etc/nova/nova.conf`, `/etc/nova/nova-compute.conf` | [Nova compute for Ubuntu (2025.1)](https://docs.openstack.org/nova/2025.1/install/compute-install-ubuntu.html) |
| `neutron-controller` | controller | `/etc/neutron/neutron.conf`, `/etc/neutron/plugins/ml2/ml2_conf.ini`, `/etc/neutron/plugins/ml2/openvswitch_agent.ini`, `/etc/neutron/l3_agent.ini`, `/etc/neutron/dhcp_agent.ini`, `/etc/neutron/metadata_agent.ini`, `/etc/nova/nova.conf` | [Neutron controller for Ubuntu (2025.1)](https://docs.openstack.org/neutron/2025.1/install/controller-install-ubuntu.html) |
| `neutron-compute` | compute | `/etc/neutron/neutron.conf`, `/etc/neutron/plugins/ml2/openvswitch_agent.ini`, `/etc/nova/nova.conf` | [Neutron compute for Ubuntu (2025.1)](https://docs.openstack.org/neutron/2025.1/install/compute-install-ubuntu.html) |
| `horizon` | controller | `/etc/openstack-dashboard/local_settings.py`, `/etc/apache2/conf-available/openstack-dashboard.conf` | [Horizon for Ubuntu (2025.1)](https://docs.openstack.org/horizon/2025.1/install/install-ubuntu.html) |
| `cinder-controller` | controller | `/etc/cinder/cinder.conf`, `/etc/nova/nova.conf` | [Cinder controller for Ubuntu (2025.1)](https://docs.openstack.org/cinder/2025.1/install/cinder-controller-install-ubuntu.html) |
| `cinder-storage` | block | `/etc/lvm/lvm.conf`, `/etc/cinder/cinder.conf`, `/etc/tgt/conf.d/cinder.conf` | [Cinder storage node for Ubuntu (2025.1)](https://docs.openstack.org/cinder/2025.1/install/cinder-storage-install-ubuntu.html) |

The `depends_on` chain follows the order of the "Environment" chapter of the installation
guide and the Keystone, Glance, Placement, Nova, Neutron, Horizon, and Cinder 2025.1 guides,
and is itself unverified:

- `hosts-file` → `time-sync-controller` → `openstack-packages` → each controller environment
  service (`mariadb`, `rabbitmq`, `memcached`, `etcd`), `keystone` waits for `mariadb`
  and `memcached` on the controller, `glance` and `placement` wait for `mariadb`,
  `memcached`, and `keystone` on the controller, `nova-controller` waits for
  `mariadb`, `rabbitmq`, `memcached`, `keystone`, `glance`, and `placement` on the
  controller, `neutron-controller` waits for `mariadb`, `rabbitmq`, `memcached`,
  `keystone`, and `nova-controller` on the controller, `horizon` waits for `memcached`,
  `keystone`, `glance`, `nova-controller`, and `neutron-controller` on the controller, and
  `cinder-controller` waits for `mariadb`, `rabbitmq`, `memcached`, `keystone`, and
  `nova-controller` on the controller;
- `hosts-file` → `time-sync-other` → `openstack-packages` on compute, block and object
  nodes, where `time-sync-other` also waits for `time-sync-controller` on the controller,
  because those nodes synchronize their clocks from it. It waits for the node's own
  `hosts-file` because its `server controller iburst` line needs the `controller` alias
  that `hosts-file` writes to `/etc/hosts`. On compute nodes, `nova-compute` waits for
  that compute node's `openstack-packages` and for `nova-controller` on the controller, and
  `neutron-compute` waits for that compute node's `openstack-packages` and `nova-compute`
  and for `neutron-controller` on the controller. On block storage nodes, `cinder-storage`
  waits for that block node's `openstack-packages` and for `cinder-controller` on the
  controller.

`openstack-packages` uses role-scoped dependencies:

```json
"depends_on": [
  { "step": "time-sync-controller", "roles": ["controller"] },
  { "step": "time-sync-other", "roles": ["compute", "block", "object"] }
]
```

So on the controller it waits only for `time-sync-controller on <controller>` and never
for another node; on a compute, block or object node it waits for `time-sync-other` on
that same node (which in turn waited for the controller's `time-sync-controller`).

`time-sync-controller`, `time-sync-other`, `openstack-packages`, `mariadb`, `rabbitmq`,
`memcached`, `etcd`, `keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`,
`neutron-controller`, `neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage`
are implemented by `openstack-forge apply` (see
[`docs/engine`](../engine/README.md)). `mariadb`, `rabbitmq`, `memcached`, `etcd`,
`keystone`, `glance`, `placement`, `nova-controller`, `nova-compute`, `neutron-controller`,
`neutron-compute`, `horizon`, `cinder-controller`, and `cinder-storage` do not update the
package lists themselves: they rely on running
after `openstack-packages`, as their `depends_on` chain says; `rabbitmq` requires
`RABBIT_PASS`, `keystone` requires `KEYSTONE_DBPASS` and `ADMIN_PASS`, `glance` requires
`ADMIN_PASS`, `GLANCE_DBPASS`, and `GLANCE_PASS`, `placement` requires `ADMIN_PASS`,
`PLACEMENT_DBPASS`, and `PLACEMENT_PASS`, `nova-controller` requires `ADMIN_PASS`,
`RABBIT_PASS`, `NOVA_DBPASS`, `NOVA_PASS`, and `PLACEMENT_PASS`, `nova-compute` requires
`RABBIT_PASS`, `NOVA_PASS`, and `PLACEMENT_PASS`, `neutron-controller` requires
`ADMIN_PASS`, `RABBIT_PASS`, `NEUTRON_DBPASS`, `NEUTRON_PASS`, `NOVA_PASS`, and
`METADATA_SECRET`, `neutron-compute` requires `RABBIT_PASS` and `NEUTRON_PASS`,
`cinder-controller` requires `ADMIN_PASS`, `RABBIT_PASS`, `CINDER_DBPASS`, and `CINDER_PASS`,
and `cinder-storage` requires `RABBIT_PASS`, `CINDER_DBPASS`, and `CINDER_PASS` (`horizon`
reads no secrets) in
`/etc/openstack-forge/secrets.json` (`openstack-forge secrets init`).
`memcached` and `etcd` listen unauthenticated on the management IP and `rabbitmq` listens
for AMQP without TLS, so the management network must be trusted (see the security section of
[`docs/engine`](../engine/README.md)). `openstack-packages` takes its Ubuntu Cloud Archive
pocket from the supported release table, not from the catalog title. `plan` only orders the
steps; `apply` runs one step on one node and does not check that the steps it waits for were
applied.

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
