# Kubernetes storage for this workstation and NAS

Use different storage for shared files and latency-sensitive application state.
The current workstation source uses retained local-path volumes for PostgreSQL
and service state. Preserve populated claims and their recovery workflow when
adding NAS storage.

| Workload | Initial choice | Reason |
| --- | --- | --- |
| Shared documents, datasets and model archives | NAS NFS | Several nodes or pods can access the same files. |
| Model files used during inference | Workstation SSD/NVMe cache, populated from NAS | Avoid repeated model-load traffic and dependence on NAS latency. |
| PostgreSQL, vector indexes and other write-heavy state | Workstation SSD/NVMe with application-consistent backups to NAS | Keep random-write latency local; qualify each database before changing its storage. |
| Prometheus database | Local filesystem on the machine running Prometheus | Prometheus explicitly does not support NFS storage. |
| Encrypted backup repositories | Dedicated NAS destination with independent recovery copies | Keep backups separate from active claims and test restoration. |

NFS supports Kubernetes ReadWriteMany volumes. The maintained
[NFS CSI driver](https://github.com/kubernetes-csi/csi-driver-nfs) can provision
directories on an existing export if dynamic provisioning is needed. Installing
a CSI driver does not create another copy of the NAS data or provide NAS failover.
See [Kubernetes access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
and [Prometheus filesystem requirements](https://prometheus.io/docs/prometheus/latest/storage/#operational-aspects).

Keep the existing NFS export's synchronous writes, root squashing, exact client
allowlist and mounted-filesystem guard. Qualify numeric ownership for the actual
pod UID/GID. Root squashing can prevent a provisioner or init container from
changing server-side ownership; prepare permissions deliberately rather than
disabling that restriction. Use a dedicated export or dataset for application
claims when isolation, quotas or retention differ from ordinary shared files.

The NAS is currently a single server. Its redundant disks do not protect against
NAS power, network or motherboard failure. Local volumes also bind a workload to
its workstation and need independent backups. Replicated CSI storage becomes
useful when multiple storage nodes and a tested failover requirement justify its
CPU, memory, network and operational cost. It is not required to add shared NFS
storage to this setup.

No claims, exports or storage classes are migrated by this recommendation.
Before an owner-run rollout, select the actual workloads, measure the storage
path, confirm permissions, retain existing claims and test backup restoration.
