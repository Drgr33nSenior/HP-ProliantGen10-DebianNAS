# Release and build-out readiness

This record separates the commissioned NAS from the public installer and the
remaining workstation/Kubernetes rollout. It contains no private target
addresses, device identifiers, credentials or operational logs. Retain those
in the owner's private install journal.

## Commissioned NAS

Owner acceptance and read-only checks through 7 November 2026 establish:

| Area | Observed result |
| --- | --- |
| Storage | The owner-created four-disk RAIDZ2 pool is ONLINE. OpenZFS 2.4.4 is loaded. The isolated write, snapshot, rollback and cleanup test passed. |
| Management | Cockpit and the 45Drives ZFS interface were accepted in the browser. Houston alert delivery passed the owner test. |
| Monitoring | The NAS receivers, workstation dashboards and alert metadata passed acceptance. Workstation collectors were redirected; the old local backends were retired with five PVCs retained. Historical data was not migrated. |
| HTTPS | Grafana, Prometheus, Alertmanager and the management UI passed HTTPS checks. Certificate keys and DNS credentials remain protected on the NAS. |
| Network | The AQC107 negotiated 10000 Mb/s full duplex. The existing NAS address and gateway moved to VLAN 30 on the new interface. Its startup profile was saved and the rollback timer disabled. |
| Network measurement | Four-stream TCP tests measured 9.19 Gb/s from NAS to workstation and 5.83 Gb/s from workstation to NAS, with ten measured seconds per direction. The slower direction had more retransmissions and higher NAS CPU use; its bottleneck is not established. |

The TCP test measures the network path. It does not establish NFS or pool I/O
performance. The new interface's saved startup configuration has not yet been
qualified by a reboot after the cutover. Network configuration remains
owner-managed; the service playbook does not rewrite interface definitions.

## Source and publication checks

Run the repository's required source gate from a clean public checkout:

```sh
make check
```

The Linux CI environment installs the checksum-pinned validation tools from
`tests/tools-linux.json` and hash-pinned Python requirements from
`tests/requirements.lock`. Optional native receiver and Houston fixtures are
separate checks. Source validation does not authorize applying the playbook to
a real target or establish physical installation.

Review the exact staged files and the media payload before committing. Private
inventories, owner transaction scripts, captured logs, certificate material and
`test-results/` do not belong in the published checkout. The release media
includes only paths listed in `installer/media/payload.files`.

Use the [media builder](INSTALL-MEDIA.md) to build and inspect the pinned Debian
installer after the source checks pass. Keep the generated ISO, hashes and build
identity as local artifacts until publication is explicitly authorized.

## Final build-out gates

1. During a maintenance window, reboot the NAS and verify the 10Gb link, VLAN
   address/default route, pool import and service startup. Check management
   access and the web interfaces before releasing the old physical connection.
2. Select separate dataset/export purposes for shared files, application claims
   and encrypted backups. Record numeric UID/GID ownership, quotas, exact client
   addresses and recovery requirements before applying storage configuration.
3. Qualify workstation NFS access, ownership and an isolated read/write test.
   Retain synchronous writes, root squashing, the exact client allowlist and the
   mounted-filesystem guard. Test recovery after a server outage.
4. Add a non-default retained Kubernetes NFS storage class or a static pilot
   claim only after the host path passes. Preserve existing claims. Keep active
   databases and frequently used model caches on workstation SSD/NVMe until
   their separate storage requirements are qualified.
5. Keep the commissioned collector destinations and retired backend replicas
   recorded in private declarative configuration. The generic workstation
   telemetry profile can deploy local backends; a rebuild must deliberately
   preserve the NAS topology and the retained history instead of reactivating
   the old servers by assumption.
6. Configure client-side encrypted backups and test restoration with credentials
   held outside the NAS. Retain independent recovery copies. Review workstation
   and Kubernetes startup policy before relying on unattended reboot recovery.

No pool recreation, historical-data deletion, claim migration or storage-class
activation is implied by this checklist. See [Kubernetes storage](KUBERNETES-STORAGE.md),
[backup qualification](BACKUPS.md) and [workstation telemetry](WORKSTATION-TELEMETRY.md)
for the corresponding owner procedures.
