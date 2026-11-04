# HPE ProLiant Gen10 Debian NAS

This installer profile configures a separate HPE ProLiant MicroServer Gen10.
Use the official Debian installer for the OS, then the repository-owned
[Ansible playbook](ansible/microserver.yml) for services.
Run the profile from a full reviewed checkout on the controller.
The playbook does not partition disks, create/import pools, join Kubernetes,
change firmware, or migrate workstation data. Source checks do not establish
physical installation, recovery, NFS access or monitoring performance.

## Hardware and placement

| Item | Owner-reported configuration and intended use |
| --- | --- |
| Platform | Original MicroServer Gen10; exact Opteron CPU remains to be discovered |
| RAM | 24 GB; retain headroom for the OS and ZFS cache |
| OS storage | 256 GB SSD: Debian, container images and active monitoring databases |
| Bulk storage | Four new 2 TB Seagate IronWolf HDDs; supplied model ST2000VN004; verify each drive |
| Network | 10GbE card in the electrically x8 slot; dedicated GPU removed |
| Pool | Owner-created four-disk RAIDZ2, about 4 TB / 3.64 TiB before filesystem overhead and free-space reserve |

The [Seagate datasheet](https://www.seagate.com/content/dam/seagate/en/content-fragments/products/datasheets/ironwolf-12tb/ironwolf-DS1904-22-2404US-en_US.pdf)
lists ST2000VN004 as a 2 TB, CMR, 5900 RPM NAS drive. Keep actual serials,
disk IDs, SSH identities and addresses in a private install journal outside Git.
The owner supplied four HDD serials; their device-reported identities and bay
mapping remain to be verified on the physical server.

Keep active models, coding workspaces and active Graphiti/database storage on
workstation SSDs. Use NFS for documents, datasets, model archives and backups.
RAIDZ2 tolerates two disk failures; it does not provide a separate backup or
protect availability when the whole MicroServer is down.

Grafana, Prometheus and Alertmanager run independently of workstation K3s.
The profile records immutable image pins in `observability/versions.json`.
Its initial workstation dashboards and pins came from the workstation project;
this repository owns their subsequent review and updates. See [NOTICE](NOTICE).
A separate MicroServer dashboard and NAS-specific rules cover host metrics.
Workstation dashboards remain incomplete until telemetry cutover is qualified.
Cluster-specific workstation absence/GPU rules are not reused as NAS health rules.

## 1. Install Debian on the SSD

Use verified official [Debian 13 AMD64 installation media](https://www.debian.org/releases/trixie/).
Identify the 256 GB SSD by model, serial and capacity. Disconnect the four HDDs
during OS installation where practical. Install a minimal system with ext4 on
the SSD, SSH and a named administrator; a desktop and ZFS root are unnecessary.
Do not select the HDDs for OS partitioning or LVM.

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
package installation or update all host packages.

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
on PATH. The CI workflow prepares the pinned validation tools in temporary
directories. Docker Compose only validates its configuration; no Docker daemon,
container execution or access to a real inventory is needed.

The checks exercise target classification, pool GUID matching, mounted-dataset
and directory guards, exact NFS client validation, private credential metadata,
container limits, alert firing/recovery and configuration parsing. Physical
installation, service lifecycle and data recovery still require the owner-run
hardware qualification above.
