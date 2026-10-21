# apt fixtures (synthetic)

Every `policy_*.txt` file here is **synthetic, modeled on real `apt-cache policy` output**:
the output of `apt-cache policy python3-openstackclient` and `apt-cache policy apt`, read
(read-only, `LC_ALL=C`) on an Ubuntu 24.04 development machine with apt 2.8 on
2026-10-21. Mirror host names were replaced with `archive.ubuntu.com`; the version strings
and the line layout were kept.

The Ubuntu Cloud Archive suite naming used here (`noble-updates/epoxy`, component `main`,
served from `ubuntu-cloud.archive.canonical.com`) is an **assumption to be confirmed on a
real host where the `openstack-packages` step runs**. The step's `verify()` details say so.

| File | Models |
| ---- | ------ |
| `policy_client_noble_installed.txt` | Client installed from Ubuntu 24.04's own archive, Cloud Archive not enabled (the case where the package alone would wrongly say "done"). |
| `policy_client_noble_not_installed.txt` | Fresh host: client not installed, Cloud Archive not enabled. |
| `policy_client_uca_installed.txt` | `cloud-archive:epoxy` enabled and the client installed from it. |
| `policy_client_uca_not_installed.txt` | `cloud-archive:epoxy` enabled, client not installed. |
| `policy_client_other_pockets.txt` | Only other Cloud Archive pockets (`epoxy-proposed`, `dalmatian`), not `cloud-archive:epoxy`. |

The unknown-package case is modeled inline in the tests as empty stdout: on the same
machine, apt-cache printed nothing on stdout for an unknown package, exited 0 and wrote an
`N: Unable to locate package ...` notice on stderr.
