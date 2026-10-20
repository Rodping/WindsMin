# WindsMin Windows user backup
![Last commit](https://img.shields.io/github/last-commit/Rodping/WindsMin)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue)](https://polyformproject.org/licenses/noncommercial/1.0.0/)

This project contains a script for copying user-profile files from Windows
disks while running Alpine Linux from a bootable USB drive. It is intended for
authorized backups and recovery.

## What the backup script does

`backup_users_to_usb.py`:

1. Uses `lsblk` to inspect the computer's partitions.
2. Looks for partitions whose root contains a `Users` folder. A volume is
   identified as a Windows installation when its root contains both `Windows`
   and `Users`.
3. If a supported partition is not already mounted, temporarily mounts it
   read-only while checking it. Temporary mounts are unmounted when the script
   finishes.
4. Writes a tree of the detected `Users` folders and copies profile files to
   the backup drive.

It searches supported local disk partitions, not just Windows drive letters.
Windows letters such as `C:`, `D:`, and `H:` are not used by Alpine; Linux
identifies partitions by device names such as `/dev/sda2` or `/dev/nvme0n1p3`.
The script can find a `Users` folder at the root of another attached volume,
but it does not search arbitrary paths such as `D:\Profiles`.

Windows does not store local and domain-account profiles in separate top-level
folders by account type. A domain user's profile that is stored on the PC is
normally one of the folders under `Users`, so the script copies it along with
the other profiles. It does not connect to a server or copy network shares,
roaming profiles, or network home folders. Those need to be mounted and backed
up separately.

## What is copied and excluded

The script copies files under each detected `Users` folder, except directories
whose names match one of these exclusions:

- `AppData`
- `Windows`
- `Program Files`
- `ProgramData`
- `$Recycle.Bin`
- `All Users`
- `Default User`
- `Public`

The exclusions apply wherever those directory names appear in the profile
tree. Excluded directories are omitted from the copy and the tree files. Tree
files record names and directory structure only; they do not contain file
contents. The script also skips symbolic links and junctions.

## Backup destination

The script automatically selects the larger writable partition on the
removable USB drive that also contains the small Alpine boot partition. It
does not need a volume label. It prefers a suitable sibling partition on the
same USB drive as the script; when Alpine is running from RAM, it looks for a
unique removable drive with a small FAT boot partition (up to 3 GB) and a
larger data partition (at least 10 GiB). The 13.5 GiB data partition on a
nominal 16 GB flash drive is large enough to meet this detection threshold.

The selected partition must already have a supported filesystem. The script
does not format, rename, or erase it. If it is not mounted, the script mounts
it read-write at a path such as `/mnt/windsmin-backup-sdb2`. If there is more
than one possible USB drive, or the intended partition cannot be identified
safely, the script stops and reports the problem instead of writing to the
Alpine system disk.

Backups are stored in a `windows_backup` directory on the selected partition:

```text
<backup partition>/windows_backup/
└── source_<disk-id>/
    ├── Users/                          # Profiles from one Windows installation
    ├── OtherVolumes/<device>/Users/     # Profiles from other volumes/installations
    └── <device>_<user>_tree.txt         # One directory tree per profile
```

When multiple Windows installations are found, their profiles are placed under
`OtherVolumes/<device>/Users/` to avoid mixing them. Make sure the data
partition has enough free space for the files being copied. A destination
partition mounted by the script stays mounted at its `/mnt/windsmin-backup-*`
path until it is unmounted or the computer shuts down.
FAT32 volumes cannot store a single file larger than 4 GB. The script warns
when the selected partition is reported as `vfat`; use a suitable filesystem
such as exFAT if your backup includes larger files. Formatting a partition
erases its contents, so the script never formats it automatically.

## Resuming an interrupted backup

The script keeps a small progress file beside each source-specific backup.
After each file is safely copied, its source disk identity, path, size, and
modification time are queued in the progress log. To reduce slow writes to USB,
the script flushes a durable checkpoint after every 64 files and when it exits.
File contents are first copied to a temporary file and synced, then renamed
into place; progress is saved only after the destination directories are synced.
This keeps resume reliable without forcing a progress-log and directory sync
after every single file.
Temporary files left by a sudden power loss are cleaned up when that same
source-specific backup is run again.

If you press `Ctrl+C`, the script stops and keeps the progress file. If the PC
loses power, completed-file progress already saved on the data partition remains
there. At most the last 63 files since the previous checkpoint may be copied
again after a sudden power loss; they are not skipped unless their progress was
durably recorded. On the next run, when the same source disk is detected, the
script asks whether to resume or start over. Resuming skips files already
recorded as complete when their source size and modification time still match
and the destination file exists with the expected size. Changed or incomplete
files are copied again.

The source-specific directory is based on the attached source volume IDs, so
backups from different PCs with different disk IDs are kept separately. If a
disk does not provide a filesystem UUID, the Linux device name is used as a
fallback; in that case, the script cannot reliably recognize the same disk
after its device name changes. Disconnect other computers' source disks while
resuming, and make sure the original source disk is attached. If you choose to
start over, existing copied files are overwritten as needed; unrelated files
are not deleted.

## Requirements

- Alpine Linux with permission to inspect and mount disks (normally run as
  `root`).
- `lsblk`, `findmnt`, `mount`, and `umount` from `util-linux`.
- Python 3 and the Python `tqdm` progress-bar package, installed on Alpine as
  `python3` and `py3-tqdm`.
- `git`, used by the launcher to update this repository.
- Filesystem support for the source partitions. The script considers NTFS,
  exFAT, FAT, ext2/3/4, Btrfs, and XFS partitions. NTFS mounting tries the
  `ntfs3` driver and then `ntfs-3g`.
- A removable USB drive with a small Alpine boot partition and a larger,
  writable data partition formatted with a supported filesystem.

## Running it on Alpine

From the project directory, run:

```sh
sh backup.sh
```

`backup.sh` installs `python3`, `py3-tqdm`, `util-linux`, `ntfs-3g`, and `git`
with Alpine's `apk` package manager, pulls the latest project changes with
`git pull`, then starts the Python script. The script's required Python
dependency (`tqdm`) is installed as Alpine's `py3-tqdm` package; it does not
need `pip install`. Package installation and `git pull` require network access.
You can run `python3 backup_users_to_usb.py` directly if the requirements are
already installed.

The current script does not require `/mnt/usb`; it selects the data partition
and mounts it under `/mnt/windsmin-backup-<device>` if needed. If you see the
message `USB not mounted: /mnt/usb`, the older script is still being run. Check
that the repository is on the branch containing automatic backup-partition
detection and that `git pull` actually updated it before running the backup.

The source partitions that the script mounts itself are mounted read-only.
Already-mounted source partitions keep their existing mount mode.

## Alpine diskless mode and `lbu commit`

The backup files are written to the selected data partition, not to Alpine's
RAM-based root filesystem; `lbu commit` is not needed to save them. The script
does not run `lbu commit`.

On a diskless Alpine system, packages installed with `apk add` may not remain
installed after reboot unless the system's local backup configuration saves
them. If you want the installed tools to persist across reboots, use Alpine's
normal `lbu` process after installing them. This is separate from the Windows
backup and does not save the copied profile files.

## Limitations and safety

- The script needs at least one detected volume whose root contains both
  `Windows` and `Users`; otherwise it stops instead of treating an arbitrary
  `Users` folder as the Windows installation.
- It only scans attached local partitions with the supported filesystem types.
  It does not scan network shares.
- Some domain accounts may not have a local profile on the computer. The script
  can only copy files that are present and readable on the attached disks.
- It does not distinguish local from domain profiles, verify copies with
  checksums, or erase old files already at the destination.
- Protect the backup drive: it may contain private user documents.

## Speed and progress display

The progress bar shows the percentage and number of files copied, refreshing
about every three seconds. The script counts files while creating the directory
tree reports, so it does not need an additional full scan just to calculate the
percentage. It then copies files using Python's optimized file-copy routines.
The progress bar is updated in the main copy loop; no extra thread is needed.
Copy speed still depends on the source disk, USB port, and flash drive.

## License

This repository is licensed under the
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).
It allows copying and use only for purposes permitted by that license; it is
not a general permission for commercial use or resale. Read [LICENSE](./LICENSE)
for the complete terms. The license is also available from the
[official PolyForm license source](https://github.com/polyformproject/polyform-licenses/blob/1.0.0/PolyForm-Noncommercial-1.0.0.md).

The repository previously contained an AGPL-3.0 license. Changing the license
does not automatically withdraw permissions already granted for earlier
versions. Make sure you have the rights to relicense all project files before
redistributing this repository under the new license. This README is
informational and is not legal advice.
