# HPE ProLiant Gen10 Debian NAS

This installer profile configures a separate HPE ProLiant MicroServer Gen10.
Use the [USB media builder](docs/INSTALL-MEDIA.md) for a minimal Debian installer,
then the repository-owned [Ansible playbook](ansible/microserver.yml) for services.
Run the profile from a full reviewed checkout on the controller.
The playbook does not partition disks, create/import pools, join Kubernetes,
change firmware, or migrate workstation data. Source checks do not establish
physical installation, recovery, NFS access or monitoring performance.

## Hardware and placement

| Item | Owner-reported configuration and intended use |
| --- | --- |
| Platform | Original MicroServer Gen10; owner-confirmed dual-core Opteron X3216 (1.6–3.0 GHz) |
| RAM | 24 GB; retain headroom for the OS and ZFS cache |
| OS storage | SanDisk SSD PLUS 240 GB (223.6 GiB reported): Debian, container images and active monitoring databases |
| Bulk storage | Four 2 TB Seagate IronWolf ST2000VN004 HDDs; unassigned LVM PVs await identity checks and owner approval for reuse |
| Network | Binardat RJ45 card, owner-reported Aquantia AQC107; in the electrically x8 slot; dedicated GPU removed |
| Pool | Planned owner-created four-disk RAIDZ2, about 4 TB / 3.64 TiB before filesystem overhead and free-space reserve |

The [Seagate datasheet](https://www.seagate.com/content/dam/seagate/en/content-fragments/products/datasheets/ironwolf-12tb/ironwolf-DS1904-22-2404US-en_US.pdf)
lists ST2000VN004 as a 2 TB, CMR, 5900 RPM NAS drive. Keep actual serials,
disk IDs, SSH identities and addresses in a private install journal outside Git.
The owner supplied four HDD serials; their device-reported identities and bay
mapping remain to be verified on the physical server.
The AQC107 uses Debian's `atlantic` kernel driver. The pinned installer includes
the module; actual card identity, firmware and link speed need first-boot checks.
See the [NIC verification procedure](docs/INSTALL-MEDIA.md#verify-the-aqc107-network-card).

Keep active models, coding workspaces and active Graphiti/database storage on
workstation SSDs. Use NFS for documents, datasets, model archives and backups.
RAIDZ2 tolerates two disk failures; it does not provide a separate backup or
protect availability when the whole MicroServer is down.

Workstation backups use client-side Restic processing: Threadripper deduplicates,
compresses and encrypts before transmission. The MicroServer stores the encrypted
repository; usable backup decryption secrets stay off the NAS. Ordinary NFS files
remain ordinary shared files. See the [backup design and qualification](docs/BACKUPS.md).

Grafana, Prometheus and Alertmanager run in Docker Compose containers, managed
by systemd on Debian, independently of workstation K3s. The native node exporter,
NFS and ZFS run on the host. The MicroServer's scope is monitoring, storage and
backup receiving; AI services and CPU-intensive processing stay on Threadripper.
The profile records immutable image pins in `observability/versions.json`.
Its initial workstation dashboards and pins came from the workstation project;
this repository owns their subsequent review and updates. See [NOTICE](NOTICE).
A separate MicroServer dashboard and NAS-specific rules cover host metrics.
Workstation dashboards remain incomplete until telemetry cutover is qualified.
Cluster-specific workstation absence/GPU rules are not reused as NAS health rules.

## 1. Install Debian on the SSD

Prepare the reviewed image with the [USB builder](docs/INSTALL-MEDIA.md), or use
verified official [Debian 13 AMD64 installation media](https://www.debian.org/releases/trixie/).
Identify the SanDisk 240 GB SSD by model, serial and capacity. Disconnect the four HDDs
during OS installation where practical. Install a minimal system on the SSD
with XFS or ext4, SSH and a named administrator; a desktop and ZFS root are unnecessary.
Do not select the HDDs for OS partitioning or LVM.
Use the custom MicroServer boot entry, which includes the owner-required
`iommu=pt` SATA workaround for installation and the installed GRUB configuration.
Verify it in `/proc/cmdline` after the first SSD boot and check disk visibility.

The current installation uses XFS on SSD LVM: 37.25 GiB for `/`, 130.38 GiB
for `/var`, 40.98 GiB for `/var/log`, 9.31 GiB for `/tmp` and 3.75 GiB swap.
`/home` shares the root filesystem, and the SSD VG has no unallocated space.
The owner reported about 128 GiB available in `/var`, sufficient for the initial
monitoring budget. See the [installed SSD layout](docs/SSD-LVM.md).
This replaces the first installation's small `/var`; the previous ext4 resize
procedure does not apply. The latest LVM output lists no HDD volume group or
LVs, but each HDD still has a partition with an LVM signature. Verify identities
and obtain owner approval before reuse; unassigned PVs do not prove erased data.

Use Debian's kernel and signed package repositories. Enable Debian `contrib`
for ZFS, with matching kernel headers; follow the
[OpenZFS Debian instructions](https://openzfs.github.io/openzfs-docs/Getting%20Started/Debian/index.html).
This profile does not add third-party repositories or disable package checks.
Resolve DKMS/module-signing requirements locally if firmware Secure Boot is
enabled. Do not disable firmware verification to make module loading succeed.

Establish the actual hostname, CPU, NIC/link, SSD mount layout, HDD identities
and SSH host fingerprint. Restrict inbound access with the owner's host/network
firewall before service setup: named-admin SSH and TCP 2049 only from the chosen
NFS clients. Do not expose monitoring ports to the LAN or Internet yet.
The profile does not rewrite the firewall or the NIC configuration.

## 2. Prepare the private target configuration

Copy [the inventory example](ansible/microserver.inventory.example.ini)
and [the variable example](ansible/microserver.config.example.yml)
outside Git. Add exactly one host, its verified SSH address and administrator.
Explicitly classify it as `dev`, `tst` or `int`; the default `unmapped` is rejected.
Keep SSH host-key checking enabled. Do not place passwords or private keys in
either file. The examples deliberately contain no usable target or pool GUID.

For a production NAS, keep the classification `prd`. The owner must launch
[owner-install.yml](ansible/owner-install.yml) manually with
`microserver_owner_install: true`; the normal playbook continues to reject
production targets. Both entry points use the same role and storage checks.
The owner entry point is not permission for an agent to mutate production.
In the commands below, production owners substitute `ansible/owner-install.yml`
for `ansible/microserver.yml` and add `-e '{"microserver_owner_install": true}'`.

Run the following as the owner, substituting the private paths and host alias:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml --ask-become-pass \
  --tags packages ansible/microserver.yml
```

This explicit bootstrap stage installs Debian ZFS/DKMS, headers, NFS,
SMART tools, Docker/Compose and the native node exporter. Package service starts
are suppressed during the apt transaction; complete configuration before reboot.
Matching headers for the running Debian kernel must be available. If they are
unavailable, update and boot the supported Debian kernel before retrying bootstrap.
No user is added to the Docker group. The default configure run does not repeat
the full package bootstrap or upgrade installed packages. Its TuneD stage ensures
the CPU policy tools are present.
Recommended packages are disabled. AMD microcode, onboard Broadcom firmware,
AppArmor and the ZFS event daemon are explicit dependencies. The custom install
profile selects no desktop task or standard task bundle; only its small explicit
base package list and installer-detected firmware are added.
Hardware tools include `ethtool`, `pciutils`, `lm-sensors`, `iperf3`,
`linux-cpupower` and PowerTOP. The base image explicitly includes AMD graphics
firmware for the onboard console and `systemd-timesyncd` for persistent time
sync. No separate AQC107 driver or firmware download is required for a card with
working onboard firmware. See [host tools and power qualification](docs/HOST-TOOLS.md).

Bootstrap keeps the iperf3 server and PowerTOP auto-tuning unit disabled and
stopped. The configure stage enables time sync and, by default, `irqbalance`;
set `microserver_irqbalance_enabled: false` if measurements favor manual/default
IRQ placement. The profile installs `nftables` for reviewed firewall setup but
does not enable it or change existing rules. TuneD selects the `performance`
CPU governor through the local `microserver-performance` profile. Disk sleep,
PCIe power policy and `iommu=pt` remain separately managed.

The owner can apply only CPU tuning with `--tags tuning` using the same reviewed
inventory and target boundary. That stage installs only TuneD/CPU diagnostics and
configures the profile; it does not require or change the data pool. It is also
part of normal configure. Actual TuneD selection, CPU frequency-policy availability
and the effective governor on every policy must pass verification. See the
[performance-mode procedure](docs/HOST-TOOLS.md#performance-mode).

To install the remaining packages and stage SSD-hosted monitoring while disk
tests run, use `--tags packages,host,tuning,monitoring` with the same reviewed
inventory and variables. This selection configures time sync, interrupt balancing
and CPU policy, and stages monitoring without selecting storage tasks. Keep
`microserver_monitoring_enabled: false` until the owner prepares the Grafana
credential. Then use `--tags monitoring` with that variable set to `true` to
activate monitoring independently of pool creation. The full `configure` stage
retains its storage checks before configuring NFS and monitoring.

An owner-run production preparation command uses:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml --ask-become-pass \
  -e '{"microserver_owner_install": true}' \
  --tags packages,host,tuning,monitoring ansible/owner-install.yml
```

Set the private inventory's lifecycle to `prd` before this command. It installs
packages and stages monitoring while the data pool is being qualified. Enable
`microserver_dimm_limits_enabled` only for the
[qualified DIMMs and sensor adapter](docs/HOST-TOOLS.md#dimm-temperature-limits).

## 3. Create and record the data pool locally

Pool creation is an owner-controlled destructive step on the MicroServer.
Identify all four whole HDDs through `/dev/disk/by-id/`, matching each model,
serial and capacity against `lsblk` and `smartctl`. Check mounts, existing
signatures and SMART health; confirm that none is the OS SSD or has required
data. New-drive reports do not replace device identity checks.

Create the reviewed four-drive RAIDZ2 pool with the owner-selected persistent
IDs. Use the drives directly rather than a hardware RAID virtual disk or mdadm
under ZFS. Choose encryption/unlock and recovery requirements before creation;
this profile neither creates encryption keys nor changes an existing policy.
Do not use force flags, automatic pool import, broad disk selectors or a copied
`/dev/sdX` list. Stop if an existing pool or unexpected data is discovered.

Record the pool name, `zpool get -H -o value guid POOL`, `zpool status -P POOL`
and the disk identities privately. The role requires the exact configured GUID
and an `ONLINE` pool. It creates only the missing `POOL/shared` dataset, with
LZ4 compression and `/srv/microserver/shared` as its mountpoint. An existing
directory at that path blocks dataset creation. An existing dataset must already
be mounted at the exact expected path; the role does not force-mount it.
LZ4 remains useful for ordinary shared files. For a future dedicated encrypted
backup dataset, use `compression=off` and `dedup=off`: Restic has already processed
the content on the workstation. This profile does not create that backup dataset
or change existing dataset properties.

Set the private configuration's pool GUID, exact private IPv4 NFS client
addresses and the owner's numeric share UID/GID. Use matching numeric identities
on the server and Linux client. Share-root ownership changes are nonrecursive.
NFSv4 exports use `root_squash`, synchronous writes and a mounted-filesystem
guard. These restrictions do not encrypt NFS traffic or authenticate users
against a directory; use only the reviewed trusted storage network.

## 4. Configure the share and stage monitoring

First use the owner-run remote check, then apply the same reviewed inputs:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml --ask-become-pass \
  --tags configure --check ansible/microserver.yml

ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml --ask-become-pass \
  --tags configure ansible/microserver.yml
```

Check mode still reads pool/dataset metadata; it does not create a pool, dataset
or activate services. The apply configures NFSv4/TCP and stages monitoring files
and the systemd unit. `microserver_monitoring_enabled: false` keeps initial
container activation opt-in; it does not stop a previously enabled stack.

Verify the exported dataset, exact client access, owner UID/GID and root squash
before moving data. Verify that an unlisted client cannot mount it. Initial NFS
client mounts are owner steps; the workstation installer and its PVCs are unchanged.

## 5. Activate and qualify local monitoring

Prepare `/etc/microserver/private/grafana-admin-password` in a private local
root session on the MicroServer. Enter a unique password of at least 16
characters through a hidden terminal prompt, without putting it in command-line
arguments, history, Ansible variables or a transcript. Require an absent target
and shell noclobber before writing it; never overwrite an existing credential.
Keep the parent directory root-owned mode 0700 and the regular password file
root-owned, numeric group 472, mode 0440. Never send its contents to an agent.
The role checks only file metadata, without checksums or credential retrieval.

Set `microserver_monitoring_enabled: true` in the private variable file and
repeat the configure apply. It validates Compose and starts the native exporter,
Docker and `microserver-monitoring.service`, then checks four local readiness
endpoints. Runtime containers use non-root UIDs, dropped capabilities, read-only
roots, bounded memory/CPU/PIDs, bounded Docker logs and immutable images.
The three container memory limits total 2304 MiB, excluding Docker, the native
exporter, OS and ZFS cache; actual usage remains to be measured.
Initial CPU ceilings for the dual-core X3216 are 0.5 CPU for Prometheus,
0.5 for Grafana and 0.1 for Alertmanager, totaling 1.1 CPUs. These ceilings
are not reservations or a measured performance guarantee. Prometheus query
concurrency starts at two; qualify ingestion and dashboard latency on hardware.
The exporter includes available hardware temperature/fan readings and network
interface counters through its hwmon, netdev and netclass collectors. Sensor
availability still depends on the actual hardware and kernel driver.

Linux host networking lets containers reach the loopback-only native exporter.
Grafana 3000, Prometheus 9090, Alertmanager 9093 and node exporter 9100 bind only
to `127.0.0.1`. Use owner SSH forwarding to inspect the UIs. Prometheus starts
with 14-day/24-GB retention on the SSD; this is not a quota on the whole SSD.
The local Alertmanager UI receives NAS alerts; no external notification receiver
is configured and no messages are sent.

Before LAN telemetry cutover, review actual addresses, TLS/authentication and
firewall scope, then qualify Alloy forwarding, node labels and workstation
alerts against both machines. Keep existing collection until the new path and
dashboards pass. Do not put the active Prometheus database on an NFS mount.
See [Prometheus storage requirements](https://prometheus.io/docs/prometheus/latest/storage/).

## Recovery and qualification

On failure, retain the task result and inspect which stage completed. Do not
rerun OS installation or pool creation as recovery. If a service reload failed,
correct its cause and have the owner explicitly reload the monitoring unit;
the installer does not provide a separate resume engine. Use the same pool GUID
for a configuration retry. Never force-import a pool to make this play pass.

The play preserves existing service data directories. Grafana's initial password
file is not a password-rotation mechanism. Owner-controlled service stops must
retain `/var/lib/microserver`; do not use Compose volume deletion as cleanup.

Remaining hardware acceptance includes repeated configuration/idempotence,
DKMS/module loading and reboot, pool import/mount, SMART self-tests, scheduled
scrubs/snapshots, separate backup and restore, NFS permissions, measured SSD/RAM/
CPU use, 10GbE throughput, and private telemetry/notification cutover. SMB,
Loki/Tempo and joining K3s are follow-ups, not enabled by this profile.

The third-party [Ubuntu MicroServer project](https://github.com/aamkye/ubuntu_on_HPE_ProLiant_MS_GEN10)
is a hardware reference only. No third-party playbooks, credentials, kernel PPA,
storage commands or trust settings are imported or executed by this profile.

## Source validation

Use a private controller Python environment with the hashed wheel closure in
`tests/requirements.lock`. `MICROSERVER_PYTHON` selects its interpreter. Run
`make check` with Ansible, Docker Compose, ShellCheck, shfmt, promtool and amtool
on PATH, plus GnuPG, jq and xorriso for media checks. The CI workflow prepares the
pinned validation tools in temporary directories. Docker Compose only validates
its configuration; no Docker daemon,
container execution or access to a real inventory is needed.
Media tests also verify a real Debian signature, reject modified inputs, exercise
the public payload/late hook and test USB identity/failure handling with OS adapters
replaced by file-only fixtures. They never open a real device. A manually dispatched
CI run with `build_media: true` also builds the full pinned ISO without writing USB.

The checks exercise target classification, pool GUID matching, mounted-dataset
and directory guards, exact NFS client validation, private credential metadata,
container limits, alert firing/recovery and configuration parsing. Physical
installation, service lifecycle and data recovery still require the owner-run
hardware qualification above.
