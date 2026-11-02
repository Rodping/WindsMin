#!/usr/bin/env python3
import os
import re
import shutil
import sys
from pathlib import Path

import backup_users_to_usb as backup


BACKUP_DIR_NAME = "windows_backup"
SOURCE_BACKUP_PATTERN = re.compile(r"source_[0-9a-f]{12}\Z")


def list_source_backups(backup_root: Path) -> list[Path]:
    backup_dir = backup_root / BACKUP_DIR_NAME
    if backup_dir.is_symlink():
        raise RuntimeError(f"Refusing to use a symbolic link: {backup_dir}")
    if not backup_dir.exists():
        return []
    if not backup_dir.is_dir():
        raise RuntimeError(f"Backup path is not a directory: {backup_dir}")

    return sorted(
        (
            entry
            for entry in backup_dir.iterdir()
            if SOURCE_BACKUP_PATTERN.fullmatch(entry.name)
            and entry.is_dir()
            and not entry.is_symlink()
        ),
        key=lambda path: path.name,
    )


def confirm(prompt: str, expected: str) -> bool:
    try:
        response = input(prompt).strip()
    except EOFError:
        return False
    return response == expected


def delete_selected_backup(backup_dir: Path, selected: Path) -> None:
    if selected.parent != backup_dir or not SOURCE_BACKUP_PATTERN.fullmatch(
        selected.name
    ):
        raise RuntimeError(f"Refusing to delete an unexpected path: {selected}")
    if selected.is_symlink() or not selected.is_dir():
        raise RuntimeError(f"Selected backup is not a real directory: {selected}")
    shutil.rmtree(selected)


def main() -> int:
    if not sys.stdin.isatty():
        print("Run this script from an interactive terminal.", file=sys.stderr)
        return 1
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print(
            "Run this script as root, for example: sudo python3 delete_backup.py",
            file=sys.stderr,
        )
        return 1

    try:
        partitions = backup.list_partitions()
        backup_root = backup.resolve_backup_root(partitions)
        backups = list_source_backups(backup_root)
    except (FileNotFoundError, RuntimeError, OSError) as error:
        print(f"Could not inspect backups: {error}", file=sys.stderr)
        return 1

    backup_dir = backup_root / BACKUP_DIR_NAME
    print(f"Detected backup partition: {backup_root}")
    print(f"Backup directory: {backup_dir}")
    if not backups:
        print("No WindsMin backup folders were found. Nothing was deleted.")
        return 0

    print("\nFound backup sets:")
    for index, path in enumerate(backups, start=1):
        print(f"  {index}. {path.name}")
    print("\nOnly these source_<12-hex-digits> directories can be deleted.")
    print(
        "Other files and folders, the Debian system, and the USB partition "
        "are left untouched."
    )

    try:
        mode = input(
            "\nChoose [1] one backup, [A] all backups, or [Q] quit: "
        ).strip().casefold()
        if mode in {"q", "quit"}:
            print("Cancelled. Nothing was deleted.")
            return 0
        if mode in {"1", "one"}:
            choice = input("Enter the number of the backup to delete: ").strip()
            if not choice.isdigit() or not 1 <= int(choice) <= len(backups):
                print("Invalid selection. Nothing was deleted.", file=sys.stderr)
                return 1
            selected = backups[int(choice) - 1]
            print(f"\nThis will permanently delete: {selected}")
            if not confirm(
                f"Type {selected.name} to confirm: ",
                selected.name,
            ):
                print("Confirmation did not match. Nothing was deleted.")
                return 0
            delete_selected_backup(backup_dir, selected)
            print(f"Deleted backup: {selected.name}")
            return 0
        if mode not in {"a", "all"}:
            print("Invalid choice. Nothing was deleted.")
            return 0

        print(
            f"\nThis will permanently delete all {len(backups)} listed backup sets."
        )
        if not confirm("Type DELETE ALL to confirm: ", "DELETE ALL"):
            print("Confirmation did not match. Nothing was deleted.")
            return 0
        for selected in backups:
            delete_selected_backup(backup_dir, selected)
            print(f"Deleted backup: {selected.name}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No further backups were deleted.")
        return 130
    except OSError as error:
        print(f"Could not delete backup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
