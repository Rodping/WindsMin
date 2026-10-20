#!/bin/sh
set -eu

apk add --no-cache python3 util-linux ntfs-3g py3-tqdm

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
git -C "$SCRIPT_DIR" pull
exec python3 "$SCRIPT_DIR/backup_users_to_usb.py"
