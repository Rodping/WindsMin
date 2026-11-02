#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this launcher as root, for example: sudo sh backup.sh" >&2
    exit 1
fi

apt-get update
apt-get install -y --no-install-recommends \
    python3 \
    python3-tqdm \
    util-linux \
    ntfs-3g \
    git

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
git -C "$SCRIPT_DIR" pull
exec python3 "$SCRIPT_DIR/backup_users_to_usb.py"
