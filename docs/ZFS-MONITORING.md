# ZFS visibility on the MicroServer

The `MicroServer / ZFS` Grafana dashboard uses the existing Prometheus datasource
and loopback node exporter. Native ZFS metrics already supply pool state and
Adaptive Replacement Cache (ARC) statistics. A small host collector adds pool
capacity, device errors and the latest scrub or resilver record.

The collector runs once a minute. It reads only `zpool list` and `zpool status`
for the configured pool. It does not import pools, change properties, clear
errors, take snapshots or start scrubs. It requires the OpenZFS 2.3 JSON status
format, which was qualified against the commissioned NAS's OpenZFS 2.3.9.
See the [OpenZFS status reference](https://openzfs.github.io/openzfs-docs/man/v2.3/8/zpool-status.8.html).

## Owner activation

For an existing commissioned NAS, run `ansible/owner-zfs.yml` with the same
protected inventory and configuration used for commissioning. The play checks
the NAS hostname, Debian version, pool GUID, existing services and protected
file paths. An existing rule file must match the retained NAS rules or the new
combined rules. Drift stops the update before component installation.

The play installs the collector, its timer, the dashboard and seven additional
alerts. It preserves the six existing NAS rules in their original group and
retains workstation rules. It recreates only Prometheus when the alert file
changes, so its file bind mount reads the new inode. Its database is retained.
Grafana detects the new dashboard file through its existing provisioning scan.
The final check selects only the new dashboard's public metadata and loopback
Prometheus datasource, and verifies that all seven ZFS rules have evaluated.
The play does not render Compose, change exports or update workstation services.

The normal monitoring role installs the same collector and dashboard for future
commissioning and full applies. Monitoring remains inactive until the existing
`microserver_monitoring_enabled` setting is enabled.

After activation, open `/d/microserver-zfs` on the existing Grafana host. Allow
one scrape interval for new metrics and the provisioning interval for the page.
The dashboard includes:

- Pool and device health, allocated/free space and fragmentation.
- Read, write and checksum error counts, plus pool and scan data errors.
- ARC size, target size and cache hit ratio.
- The latest scrub/resilver function, state, examined bytes and completion time.
- Host disk throughput, including the OS disk, with separate device series.

Device series use stable vdev GUIDs rather than disk serials or device paths.
Map a GUID with `zpool status -g POOL`. Parent and leaf error counts overlap;
do not sum them to produce a pool error total.

## Collection and alerts

The service has no capabilities or IP network access. It can access `/dev/zfs`
and write only its existing textfile metrics directory. CPU is capped at 10%,
memory at 64 MiB, and runtime at 20 seconds. Each read command has a six-second
timeout. The collector accepts at most 64 vdevs and a 1 MiB response per command.

Atomic replacement of `zfs.prom` prevents partial snapshots. A failed collection
replaces old health samples with a failure marker. Collection older than three
minutes is stale; the unavailable alert has a five-minute hold while the node
exporter is up. Pool/device faults and data errors have a two-minute hold.
Allocation above 80% warns after 15 minutes; above 90% is critical after five.
Alerts use the existing local Alertmanager route. External notifications require
separate delivery configuration.

No scan record means that ZFS has no scan information to report. A newer
resilver can replace the previous scrub record, so the dashboard does not claim
to preserve full scrub history or prove a scrub schedule. Scrub scheduling,
SMART tests, snapshots and independent backups remain separate operations.

Useful owner checks are:

```sh
systemctl status microserver-zfs-metrics.timer --no-pager
systemctl show microserver-zfs-metrics.service -p Result -p ExecMainStatus
sudo /usr/bin/python3 /usr/local/libexec/microserver-zfs-metrics.py --pool nas --stdout
```

The last command reads current metrics without writing a file or changing ZFS.
