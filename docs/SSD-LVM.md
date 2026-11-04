# Installed SSD layout

The owner reported this layout after changing the SSD allocation and rearranging
the disks. It replaces the earlier ext4 layout with a small `/var` and large
`/home`. No SSD resize is required for the initial service setup. The earlier
ext4 shrink procedure has been retired; do not apply it to the current XFS
filesystems.

## OS and service storage

The SanDisk SSD PLUS 240 GB reports 223.6 GiB. Its `microserver-vg` volume
group uses one SSD PV and has no unallocated extents. Device letters changed
after the rearrangement; identify disks by model, serial and persistent ID
before any future storage operation.

| Mount or use | Reported LV size | Filesystem or type |
| --- | --- | --- |
| `/boot/efi` | 976 MiB partition | FAT |
| `/boot` | 977 MiB partition | ext4 |
| `/` | 37.25 GiB | XFS |
| `/var` | 130.38 GiB | XFS |
| `/var/log` | 40.98 GiB | XFS |
| `/tmp` | 9.31 GiB | XFS |
| Swap | 3.75 GiB | Linux swap |

`/home` is a directory on the root filesystem, not a separate LV. Home data
therefore shares root capacity. `/var/log` has its own LV. Docker images,
container logs under `/var/lib/docker` and the active monitoring databases use
`/var`; the separate log filesystem does not bound Docker's storage use.

The owner reported about 128 GiB available in `/var`, which accommodates the
initial 24 GB Prometheus retention budget plus container images and other
service data. Retention is not a filesystem quota. Monitor free space on `/`,
`/var` and `/var/log` as services are activated. Do not reduce an LV to create
free extents without a separate reviewed filesystem and recovery procedure.

## Read-only qualification

Run these checks on the NAS when confirming the current layout:

```sh
lsblk -e7 -o NAME,SIZE,MODEL,TYPE,FSTYPE,MOUNTPOINTS
df -hT / /var /var/log /home /tmp
pvs --units g -o pv_name,vg_name,pv_size,pv_free
vgs --units g -o vg_name,vg_size,vg_free
lvs -a --units g -o vg_name,lv_name,lv_size,lv_attr,devices
```

The latest owner output lists only the SSD VG and its five LVs. Each of the
four ST2000VN004 HDDs has an unassigned LVM PV on a partition; no HDD VG or
LV is listed. Those signatures do not prove that old data has been erased.
Verify the persistent identities, required data disposition and drive health
before any owner-run signature removal or ZFS pool creation. Keep disk IDs,
serials and addresses in a private install journal outside Git.
