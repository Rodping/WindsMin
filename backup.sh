#!/bin/sh
set -eu

apk add --no-cache python3 py3-tqdm util-linux blkid ntfs-3g git

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
git -C "$SCRIPT_DIR" pull
exec python3 "$SCRIPT_DIR/backup_users_to_usb.py"
