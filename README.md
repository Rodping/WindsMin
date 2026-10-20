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

The exclusions apply wherever those directory names appear in the profile
tree. The tree file shows an excluded directory's name but does not list its
contents. The tree records names and directory structure only; it does not
contain file contents.

## Backup destination

The destination must already be mounted at `/mnt/usb`. The script does not
choose or mount the destination drive automatically. It writes to:

```text
/mnt/usb/windows_backup/
├── Users/                         # Users folder from one Windows installation
├── OtherVolumes/<device>/Users/    # Users folders from other volumes
└── windows_users_tree.txt
```

When more than one Windows installation is found, each installation's profiles
are also stored under `OtherVolumes/<device>/Users/` to avoid mixing them.
Make sure the destination has enough free space for the files being copied.
The 16 GB Alpine boot drive is not automatically used as the backup
destination.

## Requirements

- Alpine Linux with permission to inspect and mount disks (normally run as
  `root`).
- `lsblk`, `mount`, and `umount` from `util-linux`.
- Python 3 and the Python `tqdm` progress-bar package.
- Filesystem support for the source partitions. The script considers NTFS,
  exFAT, FAT, ext2/3/4, Btrfs, and XFS partitions. NTFS mounting tries the
  `ntfs3` driver and then `ntfs-3g`.
- A destination drive mounted at `/mnt/usb`.

## Running it on Alpine

From the project directory, run:

```sh
sh backup.sh
```

`backup.sh` installs `python3`, `util-linux`, `ntfs-3g`, and `py3-tqdm` with
`apk`, then starts the Python script. Package installation requires access to
the configured Alpine package repositories. You can run
`python3 backup_users_to_usb.py` directly if those dependencies are already
installed.

The source partitions that the script mounts itself are mounted read-only.
Already-mounted source partitions keep their existing mount mode.

## Alpine diskless mode and `lbu commit`

The backup files are written to `/mnt/usb/windows_backup`; `lbu commit` is not
needed to save them. The script does not run `lbu commit`.

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
