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

The NAS is currently a single server. Its redundant disks do not protect against
NAS power, network or motherboard failure. Local volumes also bind a workload to
its workstation and need independent backups. Replicated CSI storage becomes
useful when multiple storage nodes and a tested failover requirement justify its
CPU, memory, network and operational cost.

Before moving a workload, measure the storage path, confirm permissions, retain
existing claims and test backup restoration.
