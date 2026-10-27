#!/bin/bash
# Sync Minecraft World from mc-main (Node 2) to pterodactyl-node1 (Node 1) for BlueMap
set -euo pipefail

rsync -av --delete \
  --rsync-path="sudo rsync" \
  -e "ssh -i /home/tigimax/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o BatchMode=yes" \
  tigimax@192.168.1.149:/var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/world/ \
  /opt/bluemap/world/
