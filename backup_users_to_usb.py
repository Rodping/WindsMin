#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    print("Missing dependency: tqdm")
    print("Install with: apk add python3 py3-pip && pip install tqdm")
    sys.exit(1)

SRC = Path("/mnt/windows")
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

def get_total_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for f in files:
            total += (Path(root) / f).stat().st_size
    return total

def save_directory_tree(path: Path, out_file: Path):
    with open(out_file, "w", encoding="utf-8", errors="ignore") as f:
        def walk(current: Path, prefix: str = ""):
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
            for i, entry in enumerate(entries):
                connector = "└── " if i == len(entries) - 1 else "├── "
                line = prefix + connector + entry.name
                print(line)
                f.write(line + "\n")

                if entry.is_dir() and entry.name not in EXCLUDED_DIRS:
                    next_prefix = prefix + ("    " if i == len(entries) - 1 else "│   ")
                    walk(entry, next_prefix)

        if path.exists():
            walk(path)

def copy_users_to_usb(src: Path, dst: Path):
    users_src = src / "Users"
    users_dst = dst / "Users"

    if not users_src.exists():
        raise FileNotFoundError(f"No Users folder found: {users_src}")

    users_dst.mkdir(parents=True, exist_ok=True)

    total_size = get_total_size(users_src)
    print(f"Total bytes to copy: {total_size}")
    print(f"Destination: {users_dst}")

    with tqdm(total=total_size, unit="B", unit_scale=True, desc="Copying Users") as pbar:
        for root, dirs, files in os.walk(users_src):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            current_root = Path(root)

            for d in dirs:
                src_dir = current_root / d
                dst_dir = users_dst / src_dir.relative_to(users_src)
                dst_dir.mkdir(parents=True, exist_ok=True)

            for file_name in files:
                src_file = current_root / file_name
                dst_file = users_dst / src_file.relative_to(users_src)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                pbar.update(src_file.stat().st_size)

def main():
    if not SRC.exists():
        print(f"Source path not found: {SRC}")
        sys.exit(1)

    if not USB.exists():
        print(f"USB not mounted: {USB}")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    source_users = SRC / "Users"
    if source_users.exists():
        print("Saving directory tree to:", TREE_FILE)
        save_directory_tree(source_users, TREE_FILE)

        print("\nCopying Windows Users to USB...")
        copy_users_to_usb(SRC, OUT_DIR)

        print("\nDone.")
        print(f"Tree file saved to: {TREE_FILE}")
        print(f"Backup saved to: {OUT_DIR / 'Users'}")
    else:
        print(f"Missing Users folder at: {source_users}")
        sys.exit(1)

if __name__ == "__main__":
    main()