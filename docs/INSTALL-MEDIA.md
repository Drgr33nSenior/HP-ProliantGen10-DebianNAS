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
repositories. The Marvell SATA path uses the kernel driver.
See [Debian's tg3 firmware package](https://packages.debian.org/trixie/all/firmware-misc-nonfree/filelist).

HPE system-ROM and NIC/controller flash updates are separate owner procedures.
This installer installs Debian firmware files and microcode; it does not flash
unknown hardware or change firmware trust settings.

Grafana, Prometheus and Alertmanager use Docker Compose on Debian under systemd;
the node exporter, NFS and ZFS run on the host. No Kubernetes node, virtual
machine, AI workload, embedding model or graph database is installed.

## Verify the AQC107 network card

The owner reports a Binardat RJ45 card with an Aquantia AQC107 controller.
Linux supports this controller through the in-tree `atlantic` driver; see the
[kernel driver documentation](https://docs.kernel.org/networking/device_drivers/ethernet/aquantia/atlantic.html).
The pinned Debian 13.7.0 image includes `atlantic.ko.xz` in both its installer
NIC package and its installed-kernel package. No vendor driver build is needed.

The AQC107 normally boots firmware from the card's onboard flash. This profile
does not need a separate AQC107 firmware file for a card with working factory
firmware. Linux 6.12's [firmware startup code](https://raw.githubusercontent.com/torvalds/linux/v6.12/drivers/net/ethernet/aquantia/atlantic/hw_atl/hw_atl_utils.c)
rejects a missing-firmware state rather than downloading a replacement. This
differs from the onboard Broadcom's Debian firmware files. If startup reports
missing or unsupported firmware, use the onboard NIC for installation and
investigate the exact card revision with its supplier before any flash update.

The minimal package list includes `ethtool` and `pciutils` for these read-only
checks. After installation, identify the actual interface with `ip -br link` and
replace `enpXsY` below with its name:

```sh
lspci -nnk -d 1d6a:
nic=enpXsY
ethtool -i "$nic"
ethtool "$nic"
ip link show dev "$nic"
```

Record the PCI ID, `Kernel driver in use: atlantic`, firmware version and link
status privately. A connected 10GbE switch port and suitable cable should show
`Speed: 10000Mb/s`, `Duplex: Full` and `Link detected: yes`. A lower negotiated
speed does not by itself show a driver failure; check the switch port and cable.
Leave auto-negotiation, MTU 1500 and driver offload/interrupt defaults in place
for the first test. Measure throughput and CPU use before tuning. PXE is
optional; this procedure boots from USB and then the SSD.

Module availability establishes software support, not physical qualification
of this Binardat board, its firmware, negotiated PCIe link or 10GbE throughput.
