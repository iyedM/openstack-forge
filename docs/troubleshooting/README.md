# Troubleshooting

This section provides guidance for diagnosing configuration, preflight, and execution errors. **All 18 deployment steps remain marked `unverified` in the step catalog and none has ever been run on a real host.**

## Common diagnostics and errors

- **Configuration validation errors (`openstack-forge config validate`):** schema and semantic errors report the exact YAML field path (for example `nodes[0].management_ip` or `networks.provider.floating_ip_range`) without echoing raw YAML lines. See [`docs/configuration/README.md`](../configuration/README.md).
- **Preflight check warnings or failures (`openstack-forge doctor`):**
  - `provider_interface_link` (`WARN`): the provider interface link is operationally down (`DOWN` / no `LOWER_UP`). OpenStack Forge never runs `netplan` or `ip link`; bring the interface up on the host (for example `ip link set dev <iface> up` or via netplan) before deploying Neutron.
  - `provider_interface` (`WARN`): the provider interface is the same as the management interface or carries `node.management_ip`. `neutron-controller` and `neutron-compute` will refuse `apply` with `PreconditionError` to avoid cutting management connectivity when attaching the port to `br-provider` (override with `allow_shared_provider_interface: true` on the node only if intentional).
  - See [`docs/doctor/README.md`](../doctor/README.md) for all checks.
- **Concurrent `apply` lock error (`StateError`):** `apply --yes` acquires an exclusive advisory lock at `<root>/var/lib/openstack-forge/apply.lock`. If another `openstack-forge apply` process holds the lock, wait for it to finish before retrying.
- **Missing secrets (`PreconditionError`):** service steps read passwords from `<root>/etc/openstack-forge/secrets.json` (mode `0600`, parent `0700`). Run `openstack-forge secrets init` on the controller and copy the required subset of secrets to compute/block nodes with `0700`/`0600` permissions; inspect present names with `openstack-forge secrets list`.
- **`cinder-storage` root-on-LVM refusal (`PreconditionError`):** if `/` is mounted from an LVM logical volume (`/dev/mapper/...`, `/dev/dm-*`, `/dev/<vg>/<lv>`), writing `filter = [ "a/<disk>/", "r/.*/"]` to `/etc/lvm/lvm.conf` would hide the OS volume group on reboot. Use a non-LVM root disk or manage `/etc/lvm/lvm.conf` externally and set `allow_lvm_root_filter_override: true` on the block node.
- **Keystone HTTP `/v3` readiness timeout (`PreconditionError`):** after restarting `apache2`, `keystone` and `placement` retry the read-only `openstack.project-list` probe up to 5 times with exponential backoff. If Keystone still does not respond at `http://controller:5000/v3`, check `systemctl status apache2` and `/var/log/apache2/` on the controller.
- **Post-deployment verification (`openstack-forge verify`):** run `openstack-forge verify --config cluster.yaml --all` (or `--json`) to inspect recorded step state from `state.json` and live read-only verification details without modifying the host. See [`docs/engine/README.md`](../engine/README.md).
