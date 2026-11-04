# Prepare the Debian installation USB

The builder adds a minimal MicroServer install entry and a public setup payload
to a pinned official Debian 13 AMD64 netinst image. It preserves the vendor
kernel, initrd and EFI boot components and checks BIOS/UEFI boot records.
The resulting ISO is a custom image; Debian's signature authenticates the
original ISO, not the derived image. Physical boot remains a separate check.

The installer uses wired networking and needs Internet access for packages.
Accounts, passwords, partitioning and OS-disk selection stay interactive.
No HDD serial, private inventory, credential or automatic partition recipe is
included. Disconnect the four HDDs during OS installation where practical.

## Build on the controller

Run as an ordinary user from the reviewed checkout on macOS or Linux. Require
Bash, jq, curl, GnuPG (`gpg` and `gpgv`), SHA-256/MD5 tools and xorriso. The CI
tool preparer builds checksum-pinned xorriso privately; it does not install it
globally. Its upstream detached signature was checked against the fingerprint
published by [GNU xorriso](https://www.gnu.org/software/xorriso/).

```sh
bash installer/media/media.sh fetch artifacts/debian-13.7.0
bash installer/media/media.sh build artifacts/debian-13.7.0 artifacts/microserver-usb
```

Each output directory must be new. Failed or interrupted attempts remain for
inspection; choose another directory after resolving the cause. Reuse an
authenticated source download with `media.sh verify SOURCE_DIR`; rebuilding
does not require downloading it again.

`installer/media/debian.json` records the release URL, exact image digest, public
key digest and Debian CD primary-key fingerprint. Verification uses a temporary
isolated GnuPG directory and never imports into the owner's keyring. Both the
signed checksum entry and ISO bytes must match the pin. Updates require review
against [Debian's authenticity page](https://www.debian.org/CD/verify.html).

The build directory contains:

- `microserver-debian-13.7.0-amd64.iso` and its adjacent `SHA256SUMS`.
- Original signed Debian checksums, `BUILD-IDENTITY.json`, boot-layout checks
  and the build log.
- A retained `work/` directory with the exact public payload and its per-file
  SHA-256 manifest. Only paths in `installer/media/payload.files` enter the image.

Input pins and payload hashes identify the build. Byte-identical ISO output
across different toolchains is not an acceptance claim.

## Preview and write the USB on macOS

```sh
bash installer/media/usb.sh list
bash installer/media/usb.sh write artifacts/microserver-usb/microserver-debian-13.7.0-amd64.iso /dev/diskN
```

Replace `/dev/diskN` with the exact owner-reviewed external physical USB disk.
Check its media name, capacity and device identity against the intended stick.
Keep the ISO and checksum on the Mac's local disk. Listing and preview do not
unmount, format or write anything. The writer rejects internal/system disks,
APFS, CoreStorage, RAID, partitions, ambiguous identities and source-on-target.

The owner then runs the concrete reviewed command in a local terminal:

```sh
sudo bash installer/media/usb.sh --execute write artifacts/microserver-usb/microserver-debian-13.7.0-amd64.iso /dev/diskN
```

Writing erases the selected USB's contents. The script requires a typed `ERASE`
confirmation with device, capacity and image-hash prefix. It rechecks identity
around unmount/open/write/readback, writes through a held descriptor, compares
exact image bytes and ejects only after a match. There is no force-unmount,
automatic device selection or automatic retry. A failed readback is not usable
installation media. USB readback does not establish a successful physical boot.

## Install and check the minimal system

1. Boot the MicroServer and select **MicroServer minimal install**. The Debian
   recovery and standard installer entries remain available.
2. Confirm the 256 GB SSD using its actual identity. Select its partitioning
   interactively; the preseed has no disk selector or partition confirmations.
3. Create the named administrator and password locally. Root login is disabled;
   no passwords are present on the USB.
4. Finish installation and remove the USB. Confirm Debian boots from the SSD,
   the correct network interface works, and the administrator has sudo access.
5. Review the staged source at `/opt/microserver-installer`. Its late hook checks
   the source manifest, copies public setup files and installs a no-recommends
   APT policy. It does not run Ansible, create/import a pool or start monitoring.
6. Follow the [NAS setup procedure](../README.md#2-prepare-the-private-target-configuration)
   with discovered hardware, verified SSH identity and private configuration.

The base has no desktop or selected task bundles. It explicitly includes SSH,
sudo, Python for remote Ansible, CA certificates, AppArmor, AMD microcode and
`firmware-misc-nonfree` for the onboard Broadcom/tg3 firmware. Official installer
firmware lookup remains enabled, with Debian `contrib` and `non-free-firmware`
repositories. The Marvell SATA path uses the kernel driver. Check the 10GbE
card's exact model/PCI ID before claiming its driver and firmware are qualified.
See [Debian's tg3 firmware package](https://packages.debian.org/trixie/all/firmware-misc-nonfree/filelist).

HPE system-ROM and NIC/controller flash updates are separate owner procedures.
This installer installs Debian firmware files and microcode; it does not flash
unknown hardware or change firmware trust settings.

Grafana, Prometheus and Alertmanager use Docker Compose on Debian under systemd;
the node exporter, NFS and ZFS run on the host. No Kubernetes node, virtual
machine, AI workload, embedding model or graph database is installed.
