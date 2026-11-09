# ZFS management through Cockpit

Cockpit provides native Linux administration and authentication. The maintained
[45Drives cockpit-zfs plugin](https://github.com/45Drives/cockpit-zfs) adds pool,
dataset and snapshot management. The older `cockpit-zfs-manager` repository is
archived. Grafana continues to provide monitoring.

The reviewed Debian 13 installation uses Cockpit from Debian and these vendor
packages: `cockpit-zfs=1.3.0-1trixie`, `python3-libzfs=2.4.1-4trixie` and
`cockpit-alerts=4.0.25-2trixie`. The binding requires ZFS 2.4. The installation
therefore has two separate owner-run stages.

## Upgrade ZFS packages

Use `ansible/owner-zfs-upgrade.yml` with the pinned owner inventory, existing NAS
configuration, and `microserver_zfs_upgrade_operation=stage`. This playbook is
specific to the commissioned Debian 13 NAS, kernel
`6.12.115+deb13.1-amd64`, and ZFS `2.3.9-0+deb13u1`.

The playbook downloads the installed packages through authenticated APT and
retains them under `/var/lib/microserver/zfs-upgrade-2.4/debs`. It records pool
identity, enabled features, mounted datasets, NFS state and module signing
metadata. It adds the signed Debian contrib backports source, pins ZFS to the
2.4 branch, and installs `2.4.4-1~bpo13+1`. The package plan permits only the seven
selected packages and replacement of the old `libzfs6linux` ABI library.

A pool whose root has `mountpoint=none` can have no mounted filesystems. The
upgrade records that empty mount baseline without creating or mounting a dataset.
If filesystems are mounted at preparation, acceptance requires each original
dataset to remain mounted at its original path.

Package work runs at low priority: the role installs
`/etc/apt/apt.conf.d/90microserver-low-priority`, so dpkg and the DKMS build
it runs inherit nice 19 and the idle I/O class. This applies to every APT
run, including a kernel update by hand, which also rebuilds `zfs-dkms`. The
build takes longer but leaves CPU and disk to the registry, NFS and
monitoring on this two-core host.

The running module remains at 2.3.9 until reboot. The playbook checks that DKMS
installed the new module for the current kernel. If Secure Boot is enabled, the
new module must retain the prior signing identity. It does not disable Secure
Boot or enrol keys.

After staging passes, reboot the NAS from your own administrator session during
a maintenance window. This interrupts NAS storage access and monitoring. Run
the same playbook with `microserver_zfs_upgrade_operation=verify` after the NAS
returns. Verification checks the running module, original dataset mounts, pool
health and features, active NAS monitoring services and trusted HTTPS endpoints.
It accepts the commissioned inactive NFS baseline, but rejects a failed NFS unit
or loss of a previously active NFS service. An enabled NFS service can start
normally during reboot.

Do not run `zpool upgrade`, `zfs upgrade`, or enable new pool features during this
transition. A package upgrade does not require an on-disk feature upgrade.

If staging fails, retain the owner log and upgrade directory. Do not reboot
until the installed module and package state have been reviewed. If recovery
requires reverting packages, use the NAS console or an owner SSH session:

```sh
cd /var/lib/microserver/zfs-upgrade-2.4/debs
sudo sha256sum --check SHA256SUMS
sudo apt-get --simulate --allow-downgrades install ./*.deb
```

Review the simulated removals and downgrades before running the corresponding
interactive install. Remove only the dedicated ZFS backports policy if it
prevents the reviewed recovery. Rebuild and check DKMS for the boot kernel
before rebooting again. Selecting the old kernel alone does not restore the old
ZFS module. Keep a separate backup of important data; retained Debian packages
provide software recovery, not a data backup.

## Install the management UI

After post-reboot verification passes, use `ansible/owner-cockpit.yml` with the
retained UI, HTTPS and receiver profile. Set `microserver_cockpit_enabled: true`
and `microserver_cockpit_admin` to the existing non-root NAS administrator. The
playbook requires the reviewed current nginx checksum in
`microserver_cockpit_previous_nginx_sha256` and refuses an existing Cockpit
installation.

The endpoint is `https://microserver.<microserver_ui_domain>`, covered by the
existing wildcard certificate. Cockpit listens only on `127.0.0.1:9091`;
Prometheus retains port 9090. The existing nginx proxy provides TLS and retains
the configured intranet network allowlist. Forwarded client and protocol headers
are replaced by nginx. Cockpit accepts only its configured HTTPS origin, and
remote-host login is disabled.

Sign in with the selected NAS Linux account. The packaged PAM stack and root
login restriction remain in place. An additional PAM allowlist restricts access
to the selected account. Cockpit requests administrative access when required
for management operations. No separate application password or shared root
credential is created.

The selected administrator must have an existing login session during
installation. The installer refreshes that user's systemd manager and starts
`dbus.socket` and `dbus.service` before enabling Cockpit. These user units can be
added by package installation after the user's manager has already started.
Cockpit requires the administrator's D-Bus socket at `/run/user/<uid>/bus`.

The vendor repository is trusted only for the three selected packages. Its
public signing key fingerprint is
`3FBD7E034E80BAF63D5AA9BB9889DBBC731C472B`. Other packages from that repository
have APT priority -1. Recommended packages, including Cockpit NetworkManager
integration, are omitted.

The plugin has a hard dependency on the vendor alerts package. Its maintainer
script reloads/reexecutes systemd and attempts to start notification services and
restart ZED. Before installation, the playbook adds unit conditions that keep the
four new Houston notification units inactive while
`/etc/microserver/cockpit-management-only` exists. It disables those units at boot
and verifies their inactive state. The existing NAS alerting remains responsible
for notifications. The seven new vendor notification/LED ZED hooks have their
execute permission removed; existing ZED hooks are retained. Recheck those modes
after a vendor package upgrade. Debian permission overrides record these modes
before installation and preserve them across package extraction. Pool and
dataset commands use the ZFS binding;
vendor notification services remain unavailable in the UI.

If UI qualification fails, the playbook stops Cockpit and restores the previous
public proxy and TLS metadata. It retains installed packages and the protected
backup directory for diagnosis. It does not automatically downgrade ZFS.

After successful installation, retain `microserver_cockpit_enabled: true` in
future monitoring, UI and TLS profiles. The preservation guard rejects a proxy
render that would remove a commissioned management route. Certificate renewal
also verifies the management hostname and anonymous session denial.

Open the UI and confirm that the existing pool, datasets and snapshots appear
before making a storage change. UI management actions can destroy data or enable
irreversible pool features; review the selected target and recovery implications
for each operation. This installation does not change pool properties, features,
datasets, snapshots, exports or workstation services.

## Integrate local 45Drives notifications

The optional owner workflow `ansible/owner-houston.yml` connects the existing
Alertmanager to the installed 45Drives Alerts UI. Set
`microserver_houston_alerts_enabled: true` and retain the commissioned Cockpit,
HTTPS and monitoring profile. The workflow requires the reviewed current
Alertmanager checksum in `microserver_houston_previous_alertmanager_sha256`.
Run it from the owner's administrator terminal with the pinned NAS inventory.

The receiver listens on `127.0.0.1:5002`. Its dedicated system account can call
only Houston's `ForwardMessage` D-Bus method. Root and the selected NAS
administrator can use the other Houston methods. The bridge preserves the vendor
UI API and database, but disables email functions. Its service also has no IP
network access. This workflow sends local UI notifications only.

Prometheus retains its existing loopback Alertmanager target. Alertmanager keeps
its current grouping intervals and adds the Houston receiver. Only Alertmanager
is recreated to refresh its configuration bind mount; its data remains in place.
The two duplicate vendor storage-alert units and seven vendor ZED hooks remain
disabled. The existing NAS rules remain responsible for generating ZFS alerts.

The vendor status checks use conventional native-service configuration paths.
Root-owned symlinks expose the canonical Ansible-managed Prometheus and
Alertmanager files at those paths. Use the existing Ansible workflows for
configuration changes. Do not run the vendor configuration wizard: its save and
reload actions assume native services, while this NAS uses containers.

The bridge checks three vendor source checksums before startup. Review the
adapter after a vendor package update; changed code stops the bridge instead of
silently removing the local-only restrictions. Existing notification rows are
preserved. A different notification table schema requires a separate migration.

Acceptance posts one synthetic alert named `MicroserverHoustonAcceptance` to
Alertmanager. A route for that name permits immediate firing delivery and a
five-second resolved interval. Readback selects only the unique test identifier
from Houston's notification database. The test resolves its own alert even when
readback fails. It does not change a Prometheus rule or create a real storage
fault. Local acceptance notifications remain visible in the UI.

If integration qualification fails, the playbook stops the two new notification
services and restores the prior Alertmanager route, D-Bus policy and service
overrides. It retains the public recovery directory, helper files, receiver
account and any notification rows for diagnosis. Retain the owner log. Do not
repeat first commissioning after a failure without reviewing that state.

Retain `microserver_houston_alerts_enabled: true` in later monitoring profiles.
The preservation guard rejects a render that would remove commissioned delivery.
Use `microserver_acceptance_operation=verify` to repeat notification acceptance.

## Run the isolated storage write test

First commissioning also runs the owner-authorized storage test. After successful
integration, use `microserver_acceptance_operation=write` to repeat it. This test
requires the commissioned `nas` pool GUID, an ONLINE pool, no active scrub or
resilver, no reported data errors, and at least 256 MiB free.

The test creates one filesystem named `nas/acceptance-<random identifier>` with
64 MiB quota and reference quota. Its private mountpoint is under `/run`.
NFS and SMB sharing, execution, setuid and device interpretation are disabled.
The test writes 4 MiB of random data, flushes it and checks its SHA-256 digest.
It creates `@baseline`, replaces the file, verifies the snapshot's original data,
and rolls back only that snapshot. It checks the original digest again.

Before rollback and cleanup, the test verifies the dataset GUID, ownership
property, quotas, mountpoint, file inventory and absence of extra descendants,
snapshot holds or clones. Successful cleanup destroys the exact test snapshot
and dataset without recursive or force options. Final checks require the
original dataset inventory, enabled pool features and healthy pool. Writing to
ZFS can activate an already enabled feature; the test does not enable features.

On failure, retain the isolated dataset and the reported
`/var/lib/microserver/zfs-write-test.*` state directory. The quota bounds retained
test data. Do not remove an unexpected descendant, hold, clone or file to make
cleanup pass. Review the exact target and ownership state before manual recovery.
Existing datasets, exports, workstation volumes and pool configuration are
outside this test. This verifies the owner CLI storage path; signed-in browser
checks separately verify UI presentation and administrative access.
