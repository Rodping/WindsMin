#!/usr/bin/env python3
import hashlib
import json
import os
import re
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


BOOT_PARTITION_MAX_SIZE = 3 * 1024**3
DATA_PARTITION_MIN_SIZE = 4 * 1024**3
WRITABLE_FILESYSTEMS = {
    "exfat",
    "vfat",
    "fat",
    "fat12",
    "fat16",
    "fat32",
    "ext2",
    "ext3",
    "ext4",
    "btrfs",
    "xfs",
}


def list_partitions() -> list[dict]:
    result = subprocess.run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "NAME,TYPE,PKNAME,RM,TRAN,SIZE,FSTYPE,UUID,MOUNTPOINTS,RO",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not inspect disks with lsblk: {result.stderr.strip()}")

    partitions = []

    def walk(devices: list[dict], parent: dict | None = None) -> None:
        for device in devices:
            if device.get("type") == "part":
                partition = dict(device)
                partition["_parent_name"] = device.get("pkname") or (
                    parent.get("name") if parent else None
                )
                partition["_parent_rm"] = parent.get("rm") if parent else device.get("rm")
                partition["_parent_tran"] = (
                    parent.get("tran") if parent else device.get("tran")
                )
                partitions.append(partition)
            walk(device.get("children", []), device)

    walk(json.loads(result.stdout).get("blockdevices", []))
    return partitions


def is_path_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def find_script_partition(partitions: list[dict]) -> dict | None:
    script_directory = Path(__file__).resolve().parent
    matches = [
        (mountpoint, partition)
        for partition in partitions
        for mountpoint in get_mountpoints(partition)
        if is_path_inside(script_directory, mountpoint)
    ]
    if not matches:
        return None
    return max(matches, key=lambda match: len(str(match[0])))[1]


def is_removable_disk(partition: dict) -> bool:
    return str(partition.get("_parent_rm", "")).lower() in {"1", "true"} or (
        str(partition.get("_parent_tran", "")).lower() == "usb"
    )


def get_backup_partition(partitions: list[dict]) -> dict:
    script_partition = find_script_partition(partitions)

    if script_partition and is_removable_disk(script_partition):
        siblings = [
            partition
            for partition in partitions
            if partition.get("_parent_name") == script_partition.get("_parent_name")
            and partition.get("name") != script_partition.get("name")
            and partition.get("fstype") in WRITABLE_FILESYSTEMS
            and int(partition.get("size") or 0) > int(script_partition.get("size") or 0)
        ]
        if siblings:
            return max(siblings, key=lambda partition: int(partition.get("size") or 0))

    # If the script is running from RAM, use the unique removable disk with
    # a small Alpine-sized FAT boot partition and one larger data partition.
    disks: dict[str, list[dict]] = {}
    for partition in partitions:
        parent = partition.get("_parent_name")
        if parent and is_removable_disk(partition):
            disks.setdefault(parent, []).append(partition)

    candidates = []
    for disk_partitions in disks.values():
        has_small_boot_partition = any(
            partition.get("fstype") in {"vfat", "fat", "fat12", "fat16", "fat32"}
            and int(partition.get("size") or 0) <= BOOT_PARTITION_MAX_SIZE
            for partition in disk_partitions
        )
        data_partitions = [
            partition
            for partition in disk_partitions
            if partition.get("fstype") in WRITABLE_FILESYSTEMS
            and int(partition.get("size") or 0) >= DATA_PARTITION_MIN_SIZE
        ]
        if has_small_boot_partition and data_partitions:
            candidates.append(
                max(data_partitions, key=lambda partition: int(partition.get("size") or 0))
            )

    if len(candidates) == 1:
        return candidates[0]

    if not candidates:
        raise RuntimeError(
            "Could not identify the backup partition. Expected a larger writable "
            "partition on the removable USB drive that contains the small Alpine "
            "boot partition. No disk was changed."
        )
    raise RuntimeError(
        "More than one removable USB drive could be the backup target. "
        "Disconnect other removable drives and try again. No disk was changed."
    )


def is_mount_read_write(path: Path) -> bool:
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "OPTIONS", "--target", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "rw" in result.stdout.strip().split(",")


def resolve_backup_root() -> Path:
    partitions = list_partitions()
    partition = get_backup_partition(partitions)
    device = partition["name"]
    if partition.get("fstype") == "vfat":
        print(
            "Warning: the backup partition uses FAT. If it is FAT32, individual "
            "files larger than 4 GB cannot be stored.",
            file=sys.stderr,
        )

    mounted_paths = get_mountpoints(partition)
    for mountpoint in mounted_paths:
        if is_mount_read_write(mountpoint) and os.access(mountpoint, os.W_OK):
            return mountpoint

    if mounted_paths:
        mountpoint = mounted_paths[0]
        raise RuntimeError(
            f"Backup partition {device} is mounted read-only at {mountpoint}. "
            "Remount it read-write and try again."
        )

    mountpoint = Path("/mnt") / f"windsmin-backup-{Path(device).name}"
    mountpoint.mkdir(parents=True, exist_ok=True)
    existing_mount = subprocess.run(
        [
            "findmnt",
            "--noheadings",
            "--output",
            "SOURCE",
            "--mountpoint",
            str(mountpoint),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing_mount.returncode == 0:
        raise RuntimeError(
            f"Mount path {mountpoint} is already in use by "
            f"{existing_mount.stdout.strip()}; refusing to write there."
        )

    filesystem = partition["fstype"]
    if filesystem in {"fat", "fat12", "fat16", "fat32"}:
        filesystem = "vfat"
    result = subprocess.run(
        ["mount", "-t", filesystem, "-o", "rw", device, str(mountpoint)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not mount backup partition {device} read-write: "
            f"{result.stderr.strip()}"
        )
    if not is_mount_read_write(mountpoint):
        raise RuntimeError(
            f"Backup partition {device} did not mount read-write at {mountpoint}."
        )
    return mountpoint


EXCLUDED_DIRS = {
    "AppData",
    "Windows",
    "Program Files",
    "ProgramData",
    "$Recycle.Bin",
    "All Users",
    "Default User",
    "Public",
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
    identity: str
    root: Path
    is_windows_installation: bool


def is_windows_root(path: Path) -> bool:
    return (path / "Windows").is_dir() and (path / "Users").is_dir()


def get_mountpoints(partition: dict) -> list[Path]:
    mountpoints = partition.get("mountpoints") or []
    if isinstance(mountpoints, str):
        mountpoints = [mountpoints]
    return [Path(path) for path in mountpoints if path]


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
                        identity=partition.get("uuid") or device,
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

        volume_ids = [volume.identity for volume in user_volumes]
        if len(volume_ids) != len(set(volume_ids)):
            raise RuntimeError(
                "Two source partitions have the same filesystem UUID. "
                "Cannot safely separate backup progress for these disks."
            )

        yield user_volumes
    finally:
        for mountpoint in reversed(temporary_mounts):
            unmount(mountpoint)
            os.rmdir(mountpoint)


def sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return sanitized.strip("._") or "user"


def iter_real_user_dirs(users_root: Path) -> list[Path]:
    if not users_root.is_dir():
        return []

    user_dirs = []
    for entry in sorted(users_root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_symlink():
            continue
        if entry.name in EXCLUDED_DIRS:
            continue
        if entry.is_dir():
            user_dirs.append(entry)
    return user_dirs


def write_directory_tree(current: Path, handle, prefix: str = "") -> None:
    entries = [
        entry
        for entry in sorted(current.iterdir(), key=lambda p: p.name.lower())
        if not entry.is_symlink() and entry.name not in EXCLUDED_DIRS
    ]
    for index, entry in enumerate(entries):
        connector = "└── " if index == len(entries) - 1 else "├── "
        handle.write(f"{prefix}{connector}{entry.name}\n")
        if entry.is_dir():
            next_prefix = prefix + ("    " if index == len(entries) - 1 else "│   ")
            write_directory_tree(entry, handle, next_prefix)


def save_directory_trees(volumes: list[UserVolume], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    for volume in volumes:
        users_root = volume.root / "Users"
        for user_dir in iter_real_user_dirs(users_root):
            device_name = Path(volume.device).name or "unknown"
            file_name = f"{sanitize_filename(device_name)}_{user_dir.name}_tree.txt"
            tree_path = out_dir / file_name

            with open(tree_path, "w", encoding="utf-8", errors="ignore") as handle:
                handle.write(f"{user_dir.name}\n")
                write_directory_tree(user_dir, handle)

            print(f"Saved directory tree: {tree_path}")


def get_backup_path(volume: UserVolume, windows_volume_count: int, dst: Path) -> Path:
    if volume.is_windows_installation and windows_volume_count == 1:
        return dst / "Users"
    device_name = Path(volume.device).name or "unknown"
    return dst / "OtherVolumes" / device_name / "Users"


def get_source_backup_dir(volumes: list[UserVolume], out_dir: Path) -> Path:
    source_ids = sorted(volume.identity for volume in volumes)
    fingerprint = hashlib.sha256("\n".join(source_ids).encode("utf-8")).hexdigest()[:12]
    return out_dir / f"source_{fingerprint}"


def get_file_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_completed_files(progress_file: Path, source_ids: list[str]) -> dict[str, dict]:
    lines = progress_file.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise RuntimeError(f"Resume file is empty and cannot be read: {progress_file}")

    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Resume file has an invalid header: {progress_file}") from error

    if header.get("type") != "header" or header.get("source_ids") != source_ids:
        raise RuntimeError(
            "The saved progress belongs to a different source disk set. "
            "Refusing to resume it."
        )

    completed = {}
    for index, line in enumerate(lines[1:], start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                print("Ignoring an incomplete final resume record.", file=sys.stderr)
                continue
            raise RuntimeError(
                f"Resume file contains invalid data on line {index + 1}: {progress_file}"
            ) from error
        if record.get("type") == "file":
            try:
                completed[record["path"]] = {
                    "size": record["size"],
                    "mtime_ns": record["mtime_ns"],
                }
            except KeyError as error:
                raise RuntimeError(
                    f"Resume file contains an incomplete record on line {index + 1}."
                ) from error
    return completed


def start_progress_file(progress_file: Path, volumes: list[UserVolume]) -> None:
    header = {
        "type": "header",
        "version": 1,
        "source_ids": sorted(volume.identity for volume in volumes),
        "sources": [
            {"device": volume.device, "identity": volume.identity}
            for volume in volumes
        ],
    }
    with open(progress_file, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class ProgressJournal:
    """Write progress in durable batches after copied files are safely published."""

    CHECKPOINT_EVERY_FILES = 64

    def __init__(self, handle):
        self.handle = handle
        self.pending_directories: set[Path] = set()
        self.pending_records = 0

    def record(
        self,
        relative_path: str,
        signature: dict[str, int],
        destination_directory: Path,
    ) -> None:
        record = {"type": "file", "path": relative_path, **signature}
        self.handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.pending_directories.add(destination_directory)
        self.pending_records += 1
        if self.pending_records >= self.CHECKPOINT_EVERY_FILES:
            self.sync()

    def sync(self) -> None:
        for directory in self.pending_directories:
            directory_fd = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.pending_directories.clear()
        self.pending_records = 0


def ask_to_resume(progress_file: Path, volumes: list[UserVolume]) -> bool:
    print(f"Found an unfinished backup: {progress_file.parent}")
    print(
        "Current source disks: "
        + ", ".join(f"{volume.device} (ID {volume.identity})" for volume in volumes)
    )
    try:
        answer = input("Resume that backup? [Y/n] ").strip().lower()
    except EOFError as error:
        raise RuntimeError(
            "An unfinished backup exists, but no interactive terminal is available. "
            "Run the script interactively to choose resume or restart."
        ) from error
    return answer not in {"n", "no"}


def iter_backup_directories(users_root: Path) -> Iterator[tuple[Path, list[str], list[str]]]:
    for root, dirs, files in os.walk(users_root):
        current_root = Path(root)
        dirs[:] = sorted(
            (
                directory
                for directory in dirs
                if directory not in EXCLUDED_DIRS
                and not (current_root / directory).is_symlink()
            ),
            key=str.lower,
        )
        visible_files = sorted(
            (
                file_name
                for file_name in files
                if not (current_root / file_name).is_symlink()
            ),
            key=str.lower,
        )
        yield current_root, dirs, visible_files


def copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".windsmin-copy-",
        suffix=".partial",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(source, temporary_path)
        with open(temporary_path, "rb") as copied_file:
            os.fsync(copied_file.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def remove_abandoned_temporary_files(out_dir: Path) -> None:
    for root, _dirs, files in os.walk(out_dir):
        for file_name in files:
            if file_name.startswith(".windsmin-copy-") and file_name.endswith(".partial"):
                (Path(root) / file_name).unlink(missing_ok=True)


def copy_users_to_usb(
    volumes: list[UserVolume],
    dst: Path,
    progress_file: Path,
    completed_files: dict[str, dict],
) -> None:
    windows_volume_count = sum(volume.is_windows_installation for volume in volumes)
    print("Starting copy. Progress is shown as bytes transferred; total size is not pre-scanned.")

    with tqdm(
        total=None,
        unit="B",
        unit_scale=True,
        unit_dynamic=True,
        desc="Copying Users",
    ) as pbar, open(progress_file, "a", encoding="utf-8") as progress_handle:
        journal = ProgressJournal(progress_handle)
        try:
            for volume in volumes:
                users_src = volume.root / "Users"
                users_dst = get_backup_path(volume, windows_volume_count, dst)
                print(f"Copying {volume.device} Users folder to: {users_dst}")

                for current_root, dirs, files in iter_backup_directories(users_src):
                    target_dir = users_dst / current_root.relative_to(users_src)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for directory in dirs:
                        (target_dir / directory).mkdir(exist_ok=True)

                    for file_name in files:
                        source_file = current_root / file_name
                        relative_to_users = source_file.relative_to(users_src)
                        destination = users_dst / relative_to_users
                        progress_key = f"{volume.identity}/{relative_to_users.as_posix()}"
                        signature = get_file_signature(source_file)
                        saved_signature = completed_files.get(progress_key)

                        if (
                            saved_signature == signature
                            and destination.is_file()
                            and destination.stat().st_size == signature["size"]
                        ):
                            continue

                        copy_file_atomically(source_file, destination)
                        journal.record(progress_key, signature, destination.parent)
                        completed_files[progress_key] = signature
                        pbar.update(signature["size"])
        finally:
            journal.sync()


def main():
    global USB

    try:
        USB = resolve_backup_root()
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    print(f"Using backup target: {USB}")

    try:
        with find_user_volumes() as volumes:
            print(f"Found {len(volumes)} volume(s) with a Users folder.")
            out_dir = get_source_backup_dir(volumes, USB / "windows_backup")
            out_dir.mkdir(parents=True, exist_ok=True)

            progress_file = out_dir / ".resume.jsonl"
            if progress_file.exists():
                resume = ask_to_resume(progress_file, volumes)
            else:
                resume = False

            if resume:
                completed_files = load_completed_files(
                    progress_file,
                    sorted(volume.identity for volume in volumes),
                )
            else:
                start_progress_file(progress_file, volumes)
                completed_files = {}

            remove_abandoned_temporary_files(out_dir)
            save_directory_trees(volumes, out_dir)

            print("\nCopying user folders to USB...")
            copy_users_to_usb(volumes, out_dir, progress_file, completed_files)

            progress_file.unlink(missing_ok=True)
            print("\nBackup completed.")
            print(f"Directory trees and backup saved to: {out_dir}")
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            "\nBackup stopped. Completed files and progress were saved. "
            "Run the script again with the same source disk to choose whether "
            "to resume or start over.",
            file=sys.stderr,
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
