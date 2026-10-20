#!/bin/sh

apk add pv
apk add python3 py3-pip 
pip install tqdm
python3 backup_users_to_usb.py