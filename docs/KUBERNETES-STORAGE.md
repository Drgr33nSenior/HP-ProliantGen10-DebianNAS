# Kubernetes storage for this workstation and NAS

Use different storage for shared files and latency-sensitive application state.
The current workstation source uses retained local-path volumes for PostgreSQL
and service state. Preserve populated claims and their recovery workflow when
adding NAS storage.

| Workload | Choice | Reason |
| --- | --- | --- |
| Shared documents, datasets, model archives and outputs | NAS NFS (`nas-nfs` StorageClass) | Several nodes or pods can access the same files. |
| Model files used during inference | Workstation SSD/NVMe cache, populated from NAS | Avoid repeated model-load traffic and dependence on NAS latency. |
| PostgreSQL, vector indexes and other write-heavy state | Workstation SSD/NVMe with application-consistent backups to NAS | Keep random-write latency local; qualify each database before changing its storage. |
| Prometheus database | Local filesystem on the machine running Prometheus | Prometheus explicitly does not support NFS storage. |
| Container images | NAS [registry](REGISTRY.md) as mirror and private store; containerd's working store stays local | overlayfs cannot run on NFS. |
| Encrypted backup repositories | Dedicated NAS destination with independent recovery copies | Keep backups separate from active claims and test restoration. |

NFS supports Kubernetes ReadWriteMany volumes. The maintained
[NFS CSI driver](https://github.com/kubernetes-csi/csi-driver-nfs) provisions
directories on an existing export. Installing a CSI driver does not create
another copy of the NAS data or provide NAS failover.
See [Kubernetes access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
and [Prometheus filesystem requirements](https://prometheus.io/docs/prometheus/latest/storage/#operational-aspects).

## Kubernetes NFS export

`ansible/owner-k8s-storage.yml` adds a dedicated dataset for application claims,
separate from the ordinary shared files:

- Dataset `nas/k8s` at `/srv/microserver/k8s`: lz4, `atime=off`, 1 TiB quota by
  default (`microserver_k8s_quota`). It is created only when missing and never
  mounted over an existing directory.
- Export to exactly one node (`microserver_k8s_nfs_client`), NFSv4 over TCP:
  `rw,sync,all_squash,anonuid=2000,anongid=2000,no_subtree_check,mountpoint,sec=sys`.
- `all_squash` maps every client UID/GID to the non-login `k8s-nfs` account
  (2000:2000). It is stricter than root squashing, lets the CSI driver create
  per-volume directories, and gives pods running as any UID one numeric owner
  on the NAS. Volumes are separated by directory only, which suits one trusted
  node. `sec=sys` NFS traffic is not encrypted.

Run it with the commissioning inventory and configuration, the latest owner
profile and `microserver_k8s_storage_enabled: true`,
`microserver_k8s_nfs_client: '<node IPv4>'`. The play verifies the active export.

## Per-user sandbox homes

`ansible/owner-sandbox-homes.yml` gives each listed person a dataset of their
own for the assistant's coding workspace. It needs the Kubernetes NFS export
above and reuses its client address.

| Item | Value |
| --- | --- |
| Dataset | `nas/k8s/sandbox/<name>`, mounted at `/srv/microserver/sandbox/<name>` |
| Parent | `nas/k8s/sandbox`: `canmount=off`, lz4, `atime=off`, `xattr=sa`, `setuid=off`, `devices=off`, total quota `microserver_sandbox_quota` (200G) |
| Space | `refquota` = the listed quota (live files); `quota` = twice that (live files plus snapshots) |
| Account | System user and group `sandbox`, UID/GID `microserver_sandbox_uid`/`_gid` (2002); no login shell, home, password or other groups |
| Export | `rw,sync,all_squash,anonuid=2002,anongid=2002,no_subtree_check,mountpoint,sec=sys` to `microserver_k8s_nfs_client` only |
| Owner | Home root `2002:2002` (`sandbox`), mode `2770` |
| Snapshots | sanoid: 24 hourly, 14 daily, 4 weekly, pruned automatically |

Design notes:

- **Datasets, not directories.** The `nas-nfs` class separates claims by
  directory only, with no per-claim limit or snapshot. A dataset per person adds
  an enforced size limit and independent snapshots and rollback.
- **`refquota` is the visible limit.** A plain `quota` also counts snapshots, so
  a full home could stay full after its files are deleted, until the snapshots
  expire. With `refquota`, deleting files frees space at once. The doubled
  `quota` caps one home's snapshot growth so it cannot use the other homes'
  space; the parent quota caps all homes together, inside the 1 TiB of `nas/k8s`.
- **Mounted outside the `nas-nfs` export.** The homes are children of `nas/k8s`
  for accounting, but mount under `/srv/microserver/sandbox`. The CSI
  provisioner and the claims it serves therefore cannot reach them.
- **UID mapping.** Workspace pods run as UID/GID 2002. `all_squash` with
  `anonuid=2002,anongid=2002` makes every file belong to 2002:2002 whichever
  client identity wrote it, so nothing runs as root to fix ownership and tools
  that check file ownership, such as Git, accept the home.
- **A dedicated account, not a login.** On the NAS, UID 1000 is the
  administrator's login, so homes squashed to 1000 would be that account's
  files; 1001 is left for the next person, and 2000 and 2001 are `k8s-nfs` and
  `zot-registry`. The play creates the `sandbox` system account and group with
  no login shell, home, password or supplementary groups. Before changing
  anything it stops if the UID, the GID or the name `sandbox` is already held in
  any other way, or if the group has members (they could write every home); it
  never renumbers an existing account. The workstation's
  `lib/workstation/sandbox_homes.py nas-vars` passes the same UID/GID the pods
  run as. `setuid=off` and `devices=off` stop a file written by a workspace
  from gaining an identity or device access if it were ever run on the NAS; do
  not execute files from these datasets on the NAS.
- **Mode `2770`** is what the kubelet sets for a pod `fsGroup` of 2002, so its
  ownership pass finds nothing to change when the pod uses
  `fsGroupChangePolicy: OnRootMismatch`.
- **sanoid** is the Debian-packaged (2.2.0, GPL-3) snapshot scheduler. It is
  preferred to a local timer script because retention pruning destroys
  snapshots, and sanoid's pruning is widely used and limited to its own
  `autosnap_*` names. Its dependencies are Perl modules plus `pv`, `lzop` and
  `mbuffer`. The role owns `/etc/sanoid/sanoid.conf` and enables the packaged
  `sanoid.timer` (every 15 minutes). Homes added later are covered without a
  configuration change. Snapshots stay on the same pool: they recover deleted
  or overwritten files, not a lost pool.

The play takes the homes and their owner as variables. The workstation
repository's `lib/workstation/sandbox_homes.py nas-vars` prints them as a JSON
extra-vars file from the workstation's private user mapping, so the names,
quotas and owner have one source. Keep that file outside Git:

```json
{
  "microserver_sandbox_homes_enabled": true,
  "microserver_sandbox_uid": 2002,
  "microserver_sandbox_gid": 2002,
  "microserver_sandbox_homes": [
    {"name": "example-parent", "quota": "20G"},
    {"name": "example-child", "quota": "20G"}
  ]
}
```

Names are lowercase DNS labels of at most 32 characters; quotas are whole
gibibytes from `1G` to `500G`. Check first, then apply the same inputs:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml -e @/path/to/private/sandbox-homes.json \
  --ask-become-pass --check --diff ansible/owner-sandbox-homes.yml
```

Check mode reads the account database, pool and dataset metadata and reports
the account and datasets it would create; it creates nothing. The apply creates
the `sandbox` account if missing, only missing datasets, refuses to
mount over an existing path, adjusts changed quotas, reloads the export table
with `exportfs -ra` (the NFS server is not restarted, so mounted volumes do not
stall) and verifies each active export. Lowering a quota below the space in
use fails without changing data.

Verify afterwards on the NAS:

```sh
getent passwd sandbox; getent group sandbox   # 2002, /usr/sbin/nologin, no members
zfs list -o name,used,refquota,quota,mountpoint -r nas/k8s/sandbox
sudo exportfs -v | grep -A1 /srv/microserver/sandbox
systemctl list-timers sanoid.timer
zfs list -t snapshot -r nas/k8s/sandbox      # after the first timer run
```

Removing a person from the list removes only their export. The dataset and its
snapshots stay, and sanoid keeps pruning them to the retention counts. Destroy a
retired home by hand after review: `zfs destroy -r nas/k8s/sandbox/<name>`, then
remove the empty `/srv/microserver/sandbox/<name>` directory.

To restore, copy files out of
`/srv/microserver/sandbox/<name>/.zfs/snapshot/<snapshot>/` on the NAS, or stop
the person's workspace and run `zfs rollback` to a named snapshot. Rollback
discards everything written after that snapshot.

The NAS is currently a single server. Its redundant disks do not protect against
NAS power, network or motherboard failure. Local volumes also bind a workload to
its workstation and need independent backups. Replicated CSI storage becomes
useful when multiple storage nodes and a tested failover requirement justify its
CPU, memory, network and operational cost.

Before moving a workload, measure the storage path, confirm permissions, retain
existing claims and test backup restoration.
