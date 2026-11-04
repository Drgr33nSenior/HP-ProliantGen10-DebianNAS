# Adjust the installed SSD LVM allocation

This owner-run procedure applies only to the recorded fresh Debian installation
on the SanDisk SSD PLUS 240 GB. It preserves the OS and filesystem UUIDs.
No command addresses the separate `virt` volume group or the installation USB.
Do not run the NAS package bootstrap until `/var` has adequate capacity.

## Check the exact target and recovery copy

The recorded `microserver-vg` has one PV on the OS SSD and almost no free
extents. Its ext4 `/home` uses about 2 MiB of a 184.30 GiB LV. Confirm those
facts still hold with `lsblk`, `pvs`, `lvs` and `df` before proceeding. Stop if
the VG spans another disk, names/types differ or additional data has appeared.

The target allocation is:

| Volume | Target |
| --- | --- |
| `/` | 40 GiB ext4 |
| `/var` | 144 GiB ext4 |
| `/home` | 16 GiB ext4 |
| `/tmp` | Existing 2.62 GiB, unchanged |
| Swap | Existing 11.25 GiB, unchanged |
| VG free space | Approximately 7.8 GiB |

Keep the existing EFI and `/boot` partitions. Run each block separately from
an owner root terminal. Stop on any error; do not paste subsequent stages after
a failed command. No force, automatic confirmation or lazy-unmount flags are
part of this procedure.

Save work, end other `/home` sessions and stop jobs that write there before
taking the recovery copy. Prepare a private directory on the root filesystem,
then back up the
SSD VG metadata and `/home`. Keep the same terminal for the variable below:

```sh
cd /root || exit 1
umask 077
resize_backup=$(mktemp -d /root/microserver-ssd-resize.XXXXXXXX) &&
  vgcfgbackup -f "$resize_backup/microserver-vg.vg" microserver-vg &&
  tar --acls --xattrs --numeric-owner --one-file-system -cpf "$resize_backup/home.tar" -C / home &&
  tar --acls --xattrs --numeric-owner --compare -f "$resize_backup/home.tar" -C /
```

Require a successful archive comparison. Retain a private copy off the SSD
before shrinking; the root-filesystem copy covers damage to `/home`, not loss
of the SSD. Do not send the archive to an agent or put it in Git or shared
storage. LVM metadata alone is not a backup of file contents.

## Shrink home before reducing its LV

End other sessions and jobs using `/home`, then run:

```sh
cd /root || exit 1
umount /home
```

If the mount is busy, stop. Move other shells out of `/home` and end their
sessions normally before trying this step again. Do not force the unmount or
run a filesystem shrink while it is mounted.

Check the unmounted filesystem:

```sh
e2fsck -f /dev/microserver-vg/home
```

Require exit status 0. If it reports corrected errors (status 1), repeat only
this check and require a clean result. For any other status, stop and inspect
the failure before continuing.

First shrink ext4 to 15 GiB, leaving a margin inside the proposed 16 GiB LV:

```sh
resize2fs /dev/microserver-vg/home 15G
```

Only after that command succeeds, reduce the LV. Review its displayed target
and confirm only `microserver-vg/home` at 16 GiB:

```sh
lvreduce --size 16G /dev/microserver-vg/home
```

Grow ext4 to the new LV boundary and remount using the existing fstab entry:

```sh
resize2fs /dev/microserver-vg/home && mount /home
```

## Grow root and service storage

After `/home` is successfully remounted, extend each LV and its ext4 filesystem:

```sh
lvextend --resizefs --size 40G /dev/microserver-vg/root
```

```sh
lvextend --resizefs --size 144G /dev/microserver-vg/var
```

Verify `df -hT / /var /home /tmp`, `lvs microserver-vg` and `vgs microserver-vg`.
Check normal account access and retain the recovery copy through reboot
verification. A failed stage can leave `/home` unmounted or the filesystem
smaller than its LV. Stop and inspect that stage; do not restart the whole
procedure, format a volume or delete an LV as recovery.

These operations have not been run by the controller. The owner must qualify
the actual result before installing or activating services. References:
[resize2fs](https://manpages.debian.org/trixie/e2fsprogs/resize2fs.8.en.html),
[lvreduce](https://manpages.debian.org/trixie/lvm2/lvreduce.8.en.html),
[lvextend](https://manpages.debian.org/trixie/lvm2/lvextend.8.en.html).
