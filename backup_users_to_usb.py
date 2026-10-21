#!/usr/bin/env python3
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import tty
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Iterator

try:
    from tqdm import tqdm
except ImportError:
    print("Missing dependency: tqdm")
    print("Install with: apk add python3 py3-tqdm")
    sys.exit(1)


BOOT_PARTITION_MAX_SIZE = 3 * 1024**3
DATA_PARTITION_MIN_SIZE = 10 * 1024**3
SYS_BLOCK_PATH = Path("/sys/class/block")
SYS_TPM_PATH = Path("/sys/class/tpm")
DEV_PATH = Path("/dev")
BITLOCKER_FILESYSTEMS = {"bitlocker"}
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


@dataclass
class BackupSummary:
    file_count: int = 0
    total_bytes: int = 0

    def add_file(self, size: int) -> None:
        self.file_count += 1
        self.total_bytes += size


class SkipCurrentUser(Exception):
    pass


class CtrlSpaceSkipListener:
    def __init__(self) -> None:
        self.file_descriptor: int | None = None
        self.original_terminal_settings = None
        self.available = False
        self._stop_event = threading.Event()
        self._skip_event = threading.Event()
        self._state_lock = threading.Lock()
        self._active_user: tuple[str, str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_error: OSError | ValueError | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return

        self.file_descriptor = sys.stdin.fileno()
        self.original_terminal_settings = termios.tcgetattr(self.file_descriptor)
        tty.setcbreak(self.file_descriptor)
        self.available = True
        self._reader_thread = threading.Thread(
            target=self._read_keys,
            name="ctrl-space-listener",
            daemon=True,
        )
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join()
            self._reader_thread = None
        if self.original_terminal_settings is not None:
            termios.tcsetattr(
                self.file_descriptor,
                termios.TCSADRAIN,
                self.original_terminal_settings,
            )
            self.original_terminal_settings = None

    def begin_user(self, identity: str, user_name: str) -> None:
        with self._state_lock:
            self._skip_event.clear()
            self._active_user = (identity, user_name)

    def end_user(self) -> None:
        with self._state_lock:
            self._active_user = None

    def skip_requested(self) -> bool:
        with self._state_lock:
            if self._reader_error is not None:
                raise RuntimeError(
                    "Could not monitor keyboard input for Ctrl+Space."
                ) from self._reader_error
        return self._skip_event.is_set()

    def _read_keys(self) -> None:
        if self.file_descriptor is None:
            return
        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select(
                    [self.file_descriptor],
                    [],
                    [],
                    0.1,
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as error:
                with self._state_lock:
                    self._reader_error = error
                return
            if not readable:
                continue
            try:
                key = os.read(self.file_descriptor, 1)
            except InterruptedError:
                continue
            except OSError as error:
                with self._state_lock:
                    self._reader_error = error
                return
            if not key:
                return
            if key == b"\x00":
                with self._state_lock:
                    if self._active_user is not None:
                        self._skip_event.set()


@contextmanager
def capture_ctrl_space() -> Generator[CtrlSpaceSkipListener, None, None]:
    listener = CtrlSpaceSkipListener()
    listener.start()
    try:
        yield listener
    finally:
        listener.stop()


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


def detect_bitlocker_partitions(partitions: list[dict]) -> list[dict]:
    detected = []
    for partition in partitions:
        filesystem = str(partition.get("fstype") or "").strip().casefold()
        if filesystem in BITLOCKER_FILESYSTEMS:
            detected.append(partition)
            continue
        if filesystem:
            continue

        try:
            result = subprocess.run(
                ["blkid", "-p", "-s", "TYPE", "-o", "value", partition["name"]],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"BitLocker probe timed out for {partition['name']}."
            ) from error
        except FileNotFoundError as error:
            raise RuntimeError(
                "The blkid utility is required for BitLocker detection. "
                "Install Alpine's blkid package and try again."
            ) from error

        if (
            result.returncode == 0
            and result.stdout.strip().casefold() in BITLOCKER_FILESYSTEMS
        ):
            partition["fstype"] = "BitLocker"
            detected.append(partition)
    return detected


def detect_tpm_devices(
    sys_tpm_path: Path = SYS_TPM_PATH,
    dev_path: Path = DEV_PATH,
) -> list[str]:
    devices = []
    for tpm_path in sorted(sys_tpm_path.glob("tpm[0-9]*")):
        if not re.fullmatch(r"tpm\d+", tpm_path.name):
            continue
        enabled_path = tpm_path / "device" / "enabled"
        if enabled_path.is_file():
            state = "enabled" if enabled_path.read_text(encoding="ascii").strip() == "1" else "disabled"
        else:
            state = "status unavailable"
        devices.append(f"{tpm_path.name} ({state})")

    if devices:
        return devices

    for device_path in sorted(dev_path.glob("tpm[0-9]*")):
        if re.fullmatch(r"tpm\d+", device_path.name):
            devices.append(f"{device_path}")
    return devices


def print_hardware_security_status(bitlocker_partitions: list[dict]) -> None:
    tpm_devices = detect_tpm_devices()
    if tpm_devices:
        print("TPM: Alpine detected " + ", ".join(tpm_devices) + ".")
        print("     This does not confirm that BitLocker is using this TPM.")
    else:
        print(
            "TPM: No TPM interface detected by Alpine. It may be absent, disabled "
            "in firmware, or unsupported by the running kernel."
        )

    if not bitlocker_partitions:
        print("BitLocker: No BitLocker volumes detected.")
        return

    warning = (
        "\n"
        + "!" * 76
        + "\n!!! BITLOCKER ENCRYPTION DETECTED - BACKUP CANNOT READ LOCKED FILES !!!\n"
        + "!" * 76
    )
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        warning = f"\033[1;31m{warning}\033[0m"
    print(warning)
    for partition in bitlocker_partitions:
        print(f"  Encrypted volume: {partition['name']}")
    print(
        "Do not format or repair these volumes. Unlock them in Windows or with an "
        "authorized recovery method before attempting to back up their files."
    )
    print("The script will continue checking other readable volumes.\n")


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
    if str(partition.get("_parent_rm", "")).lower() in {"1", "true"}:
        return True
    if str(partition.get("_parent_tran", "")).lower() == "usb":
        return True

    parent_name = partition.get("_parent_name")
    if not parent_name:
        return False

    sysfs_device = SYS_BLOCK_PATH / Path(parent_name).name / "device"
    try:
        device_path = sysfs_device.resolve(strict=True)
    except OSError:
        return False

    return any(part.lower().startswith("usb") for part in device_path.parts)


def get_backup_partition(partitions: list[dict]) -> dict:
    script_partition = find_script_partition(partitions)

    if script_partition and is_removable_disk(script_partition):
        siblings = [
            partition
            for partition in partitions
            if partition.get("_parent_name") == script_partition.get("_parent_name")
            and partition.get("name") != script_partition.get("name")
            and partition.get("fstype") in WRITABLE_FILESYSTEMS
            and int(partition.get("size") or 0) >= DATA_PARTITION_MIN_SIZE
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


def resolve_backup_root(partitions: list[dict] | None = None) -> Path:
    if partitions is None:
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
def find_user_volumes(
    partitions: list[dict] | None = None,
) -> Generator[list[UserVolume], None, None]:
    if partitions is None:
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
            bitlocker_partitions = [
                partition
                for partition in partitions
                if str(partition.get("fstype") or "").casefold() in BITLOCKER_FILESYSTEMS
            ]
            if bitlocker_partitions:
                details += (
                    " BitLocker volumes were detected and cannot be read while locked: "
                    + ", ".join(partition["name"] for partition in bitlocker_partitions)
                    + "."
                )
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
    with os.scandir(users_root) as entries:
        for entry in entries:
            if entry.name in EXCLUDED_DIRS:
                continue
            if entry.is_dir(follow_symlinks=False):
                user_dirs.append(Path(entry.path))
    return sorted(user_dirs, key=lambda path: path.name.lower())


def write_directory_tree(
    current: Path,
    handle,
    summary: BackupSummary,
    prefix: str = "",
    skip_check: Callable[[], bool] | None = None,
) -> None:
    if skip_check is not None and skip_check():
        raise SkipCurrentUser

    with os.scandir(current) as scan:
        entries = sorted(
            (
                entry
                for entry in scan
                if entry.name not in EXCLUDED_DIRS and not entry.is_symlink()
            ),
            key=lambda entry: entry.name.lower(),
        )

    for index, entry in enumerate(entries):
        if skip_check is not None and skip_check():
            raise SkipCurrentUser
        connector = "└── " if index == len(entries) - 1 else "├── "
        handle.write(f"{prefix}{connector}{entry.name}\n")
        if entry.is_dir(follow_symlinks=False):
            next_prefix = prefix + ("    " if index == len(entries) - 1 else "│   ")
            write_directory_tree(entry, handle, summary, next_prefix, skip_check)
        elif entry.is_file(follow_symlinks=False):
            summary.add_file(entry.stat(follow_symlinks=False).st_size)

    if skip_check is not None and skip_check():
        raise SkipCurrentUser


def save_directory_trees(
    volumes: list[UserVolume],
    out_dir: Path,
    skip_listener: CtrlSpaceSkipListener,
    skipped_users: set[tuple[str, str]],
) -> BackupSummary:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = BackupSummary()

    if skip_listener.available:
        print(
            "While a user tree is being scanned, press Ctrl+Space to skip that "
            "user's tree and file copy."
        )
    else:
        print(
            "Ctrl+Space skipping is unavailable because stdin is not an "
            "interactive terminal."
        )

    for volume in volumes:
        users_root = volume.root / "Users"
        for user_dir in iter_real_user_dirs(users_root):
            device_name = Path(volume.device).name or "unknown"
            file_name = f"{sanitize_filename(device_name)}_{user_dir.name}_tree.txt"
            tree_path = out_dir / file_name
            user_summary = BackupSummary()
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=".windsmin-tree-",
                suffix=".partial",
                dir=out_dir,
            )
            temp_path = Path(temp_name)

            skip_listener.begin_user(volume.identity, user_dir.name)
            print(
                f"Now scanning user '{user_dir.name}'. "
                "Ctrl+Space skips this user's tree and file copy.",
                flush=True,
            )
            try:
                with os.fdopen(
                    temp_fd,
                    "w",
                    encoding="utf-8",
                    errors="ignore",
                ) as handle:
                    handle.write(f"{user_dir.name}\n")
                    write_directory_tree(
                        user_dir,
                        handle,
                        user_summary,
                        skip_check=skip_listener.skip_requested,
                    )
                os.replace(temp_path, tree_path)
            except SkipCurrentUser:
                skipped_users.add((volume.identity, user_dir.name))
                print(
                    f"Skipped user '{user_dir.name}': no tree or files will be "
                    "copied for this user in this run."
                )
                continue
            finally:
                skip_listener.end_user()
                temp_path.unlink(missing_ok=True)

            summary.file_count += user_summary.file_count
            summary.total_bytes += user_summary.total_bytes
            print(f"Saved directory tree: {tree_path}")
    return summary


def get_backup_path(volume: UserVolume, windows_volume_count: int, dst: Path) -> Path:
    if volume.is_windows_installation and windows_volume_count == 1:
        return dst / "Users"
    device_name = Path(volume.device).name or "unknown"
    return dst / "OtherVolumes" / device_name / "Users"


def get_source_backup_dir(volumes: list[UserVolume], out_dir: Path) -> Path:
    source_ids = sorted(volume.identity for volume in volumes)
    fingerprint = hashlib.sha256("\n".join(source_ids).encode("utf-8")).hexdigest()[:12]
    return out_dir / f"source_{fingerprint}"


def load_completed_files(progress_file: Path, source_ids: list[str]) -> dict[str, dict]:
    completed = {}
    with open(progress_file, encoding="utf-8") as handle:
        first_line = handle.readline()
        if not first_line:
            raise RuntimeError(f"Resume file is empty and cannot be read: {progress_file}")

        try:
            header = json.loads(first_line)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Resume file has an invalid header: {progress_file}"
            ) from error

        if header.get("type") != "header" or header.get("source_ids") != source_ids:
            raise RuntimeError(
                "The saved progress belongs to a different source disk set. "
                "Refusing to resume it."
            )

        for line_number, line in enumerate(handle, start=2):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                if not line.endswith("\n"):
                    print("Ignoring an incomplete final resume record.", file=sys.stderr)
                    continue
                raise RuntimeError(
                    f"Resume file contains invalid data on line {line_number}: "
                    f"{progress_file}"
                ) from error

            if record.get("type") == "file":
                try:
                    completed[record["path"]] = {
                        "size": record["size"],
                        "mtime_ns": record["mtime_ns"],
                    }
                except KeyError as error:
                    raise RuntimeError(
                        f"Resume file contains an incomplete record on line "
                        f"{line_number}."
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


def iter_backup_directories(
    users_root: Path,
    skipped_user_dirs: set[str] | None = None,
) -> Iterator[tuple[Path, list[os.DirEntry[str]], list[os.DirEntry[str]]]]:
    pending = [users_root]
    while pending:
        current_root = pending.pop()
        with os.scandir(current_root) as scan:
            entries = list(scan)

        directories = []
        files = []
        for entry in entries:
            if entry.name in EXCLUDED_DIRS or entry.is_symlink():
                continue
            if (
                current_root == users_root
                and skipped_user_dirs is not None
                and entry.name in skipped_user_dirs
            ):
                continue
            if entry.is_dir(follow_symlinks=False):
                directories.append(entry)
            elif entry.is_file(follow_symlinks=False):
                files.append(entry)

        yield current_root, directories, files
        pending.extend(Path(entry.path) for entry in reversed(directories))


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
    for root, dirs, files in os.walk(out_dir):
        dirs.sort()
        for file_name in files:
            if (
                file_name.startswith((".windsmin-copy-", ".windsmin-tree-"))
                and file_name.endswith(".partial")
            ):
                (Path(root) / file_name).unlink(missing_ok=True)


def copy_users_to_usb(
    volumes: list[UserVolume],
    dst: Path,
    progress_file: Path,
    completed_files: dict[str, dict],
    total_bytes: int,
    skipped_users: set[tuple[str, str]],
) -> None:
    windows_volume_count = sum(volume.is_windows_installation for volume in volumes)
    print("Copying files. Progress is shown as a percentage and refreshes every 3 seconds.")

    with tqdm(
        total=total_bytes,
        desc="Copying Users",
        bar_format="{desc}: {percentage:3.0f}%",
        mininterval=3,
        dynamic_ncols=True,
    ) as pbar, open(progress_file, "a", encoding="utf-8") as progress_handle:
        journal = ProgressJournal(progress_handle)
        try:
            for volume in volumes:
                users_src = volume.root / "Users"
                users_dst = get_backup_path(volume, windows_volume_count, dst)
                print(f"Copying {volume.device} Users folder to: {users_dst}")
                skipped_user_dirs = {
                    user_name
                    for identity, user_name in skipped_users
                    if identity == volume.identity
                }

                for current_root, dirs, files in iter_backup_directories(
                    users_src,
                    skipped_user_dirs,
                ):
                    target_dir = users_dst / current_root.relative_to(users_src)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    for directory in dirs:
                        if current_root == users_src:
                            print(f"Copying files for user '{directory.name}'...")
                        (target_dir / directory.name).mkdir(exist_ok=True)

                    for file_entry in files:
                        source_file = Path(file_entry.path)
                        source_stat = file_entry.stat(follow_symlinks=False)
                        relative_to_users = source_file.relative_to(users_src)
                        destination = users_dst / relative_to_users
                        progress_key = f"{volume.identity}/{relative_to_users.as_posix()}"
                        signature = {
                            "size": source_stat.st_size,
                            "mtime_ns": source_stat.st_mtime_ns,
                        }
                        saved_signature = completed_files.get(progress_key)

                        if (
                            saved_signature == signature
                            and destination.is_file()
                            and destination.stat().st_size == signature["size"]
                        ):
                            pbar.update(signature["size"])
                            continue

                        copy_file_atomically(source_file, destination)
                        journal.record(progress_key, signature, destination.parent)
                        completed_files[progress_key] = signature
                        pbar.update(signature["size"])
        finally:
            journal.sync()
        pbar.refresh()


def main():
    global USB

    try:
        partitions = list_partitions()
        bitlocker_partitions = detect_bitlocker_partitions(partitions)
        print_hardware_security_status(bitlocker_partitions)
        USB = resolve_backup_root(partitions)
    except (FileNotFoundError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    print(f"Using backup target: {USB}")

    try:
        with find_user_volumes(partitions) as volumes:
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
            skipped_users: set[tuple[str, str]] = set()
            with capture_ctrl_space() as skip_listener:
                summary = save_directory_trees(
                    volumes,
                    out_dir,
                    skip_listener,
                    skipped_users,
                )
            required_gib = summary.total_bytes / (1024**3)
            print(
                f"Backup size: {required_gib:.2f} GiB "
                f"({summary.file_count:,} files)."
            )
            if skipped_users:
                skipped_names = ", ".join(
                    sorted(user_name for _, user_name in skipped_users)
                )
                print(f"Skipped users for this run: {skipped_names}")

            print("\nCopying user folders to USB...")
            copy_users_to_usb(
                volumes,
                out_dir,
                progress_file,
                completed_files,
                summary.total_bytes,
                skipped_users,
            )

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
