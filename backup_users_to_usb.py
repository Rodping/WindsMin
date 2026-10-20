#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from tqdm import tqdm
except ImportError:
    print("Missing dependency: tqdm")
    print("Install with: apk add python3 py3-tqdm")
    sys.exit(1)

USB = Path("/mnt/usb")
OUT_DIR = USB / "windows_backup"
TREE_FILE = OUT_DIR / "windows_users_tree.txt"

EXCLUDED_DIRS = {
    "AppData",
    "Windows",
    "Program Files",
    "ProgramData",
    "$Recycle.Bin",
}
NTFS_FILESYSTEMS = {"ntfs", "ntfs3"}
MOUNTABLE_FILESYSTEMS = NTFS_FILESYSTEMS | {
    "exfat",
    "vfat",
    "fat",
    "ext2",
    "ext3",
    "ext4",
    "btrfs",
    "xfs",
}


@dataclass(frozen=True)
class UserVolume:
    device: str
    root: Path
    is_windows_installation: bool


def is_windows_root(path: Path) -> bool:
    return (path / "Windows").is_dir() and (path / "Users").is_dir()


def get_partitions(devices: list[dict]) -> Iterator[dict]:
    for device in devices:
        if device.get("type") == "part":
            yield device
        yield from get_partitions(device.get("children", []))


def get_mountpoints(partition: dict) -> list[Path]:
    mountpoints = partition.get("mountpoints") or []
    if isinstance(mountpoints, str):
        mountpoints = [mountpoints]
    return [Path(path) for path in mountpoints if path]


def list_partitions() -> list[dict]:
    result = subprocess.run(
        ["lsblk", "--json", "--paths", "--output", "NAME,TYPE,FSTYPE,MOUNTPOINTS"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not inspect disks with lsblk: {result.stderr.strip()}")

    devices = json.loads(result.stdout).get("blockdevices", [])
    return list(get_partitions(devices))


def mount_partition_read_only(device: str, filesystem: str, mountpoint: str) -> None:
    mount_errors = []
    drivers = ("ntfs3", "ntfs-3g") if filesystem in NTFS_FILESYSTEMS else (filesystem,)
    for driver in drivers:
        result = subprocess.run(
            ["mount", "-t", driver, "-o", "ro", device, mountpoint],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        mount_errors.append(f"{driver}: {result.stderr.strip()}")

    raise RuntimeError(
        f"Could not mount {device} read-only ({'; '.join(mount_errors)}). "
        "Check that the filesystem driver is installed and run with permission to mount."
    )


def unmount(mountpoint: str) -> None:
    result = subprocess.run(
        ["umount", mountpoint],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not unmount temporary Windows volume {mountpoint}: "
            f"{result.stderr.strip()}"
        )


def is_backup_volume(mountpoint: Path) -> bool:
    backup_path = USB.resolve()
    source_path = mountpoint.resolve()
    try:
        backup_path.relative_to(source_path)
        return True
    except ValueError:
        try:
            source_path.relative_to(backup_path)
            return True
        except ValueError:
            return False


@contextmanager
def find_user_volumes() -> Iterator[list[UserVolume]]:
    partitions = list_partitions()
    temporary_mounts = []
    user_volumes = []
    seen_roots = set()
    mount_errors = []

    try:
        for partition in partitions:
            filesystem = (partition.get("fstype") or "").lower()
            if filesystem not in MOUNTABLE_FILESYSTEMS:
                continue

            device = partition.get("name", "unknown")
            mountpoints = get_mountpoints(partition)

            if not mountpoints:
                mountpoint = tempfile.mkdtemp(prefix="windows-source-")
                try:
                    mount_partition_read_only(device, filesystem, mountpoint)
                except RuntimeError as error:
                    os.rmdir(mountpoint)
                    mount_errors.append(str(error))
                    continue
                temporary_mounts.append(mountpoint)
                mountpoints = [Path(mountpoint)]

            for mountpoint in mountpoints:
                if is_backup_volume(mountpoint):
                    continue
                users_path = mountpoint / "Users"
                if not users_path.is_dir():
                    continue

                resolved_root = mountpoint.resolve()
                if resolved_root in seen_roots:
                    continue
                seen_roots.add(resolved_root)
                user_volumes.append(
                    UserVolume(
                        device=device,
                        root=mountpoint,
                        is_windows_installation=is_windows_root(mountpoint),
                    )
                )

        if not any(volume.is_windows_installation for volume in user_volumes):
            details = f" Mount errors: {'; '.join(mount_errors)}" if mount_errors else ""
            raise FileNotFoundError(
                "Could not find a Windows installation and Users folder on an "
                f"available disk partition.{details}"
            )

        yield user_volumes
    finally:
        for mountpoint in reversed(temporary_mounts):
            unmount(mountpoint)
            os.rmdir(mountpoint)


def get_total_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [directory for directory in dirs if directory not in EXCLUDED_DIRS]
        for file_name in files:
            total += (Path(root) / file_name).stat().st_size
    return total


def save_directory_trees(volumes: list[UserVolume], out_file: Path) -> None:
    with open(out_file, "w", encoding="utf-8", errors="ignore") as f:
        def walk(current: Path, prefix: str = "") -> None:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
            for i, entry in enumerate(entries):
                connector = "└── " if i == len(entries) - 1 else "├── "
                line = prefix + connector + entry.name
                print(line)
                f.write(line + "\n")

                if (
                    entry.is_dir()
                    and not entry.is_symlink()
                    and entry.name not in EXCLUDED_DIRS
                ):
                    next_prefix = prefix + ("    " if i == len(entries) - 1 else "│   ")
                    walk(entry, next_prefix)

        for volume in volumes:
            kind = "Windows" if volume.is_windows_installation else "additional disk"
            heading = f"{volume.device} ({kind})"
            print(f"\n{heading}")
            f.write(f"\n{heading}\n")
            walk(volume.root / "Users")


def get_backup_path(volume: UserVolume, windows_volume_count: int, dst: Path) -> Path:
    if volume.is_windows_installation and windows_volume_count == 1:
        return dst / "Users"
    device_name = Path(volume.device).name or "unknown"
    return dst / "OtherVolumes" / device_name / "Users"


def copy_users_to_usb(volumes: list[UserVolume], dst: Path) -> None:
    windows_volume_count = sum(volume.is_windows_installation for volume in volumes)
    total_size = sum(get_total_size(volume.root / "Users") for volume in volumes)
    print(f"Total bytes to copy: {total_size}")

    with tqdm(total=total_size, unit="B", unit_scale=True, desc="Copying Users") as pbar:
        for volume in volumes:
            users_src = volume.root / "Users"
            users_dst = get_backup_path(volume, windows_volume_count, dst)
            print(f"Copying {volume.device} Users folder to: {users_dst}")
            users_dst.mkdir(parents=True, exist_ok=True)

            for root, dirs, files in os.walk(users_src):
                dirs[:] = [directory for directory in dirs if directory not in EXCLUDED_DIRS]
                current_root = Path(root)

                for directory in dirs:
                    src_dir = current_root / directory
                    dst_dir = users_dst / src_dir.relative_to(users_src)
                    dst_dir.mkdir(parents=True, exist_ok=True)

                for file_name in files:
                    src_file = current_root / file_name
                    dst_file = users_dst / src_file.relative_to(users_src)
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    pbar.update(src_file.stat().st_size)


def main():
    if not USB.exists():
        print(f"USB not mounted: {USB}")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with find_user_volumes() as volumes:
            print(f"Found {len(volumes)} volume(s) with a Users folder.")
            print("Saving directory tree to:", TREE_FILE)
            save_directory_trees(volumes, TREE_FILE)

            print("\nCopying user folders to USB...")
            copy_users_to_usb(volumes, OUT_DIR)

            print("\nDone.")
            print(f"Tree file saved to: {TREE_FILE}")
            print(f"Backup saved to: {OUT_DIR}")
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()