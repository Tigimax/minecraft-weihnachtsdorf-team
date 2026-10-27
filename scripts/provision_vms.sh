#!/usr/bin/env bash
# Provisioning script for Minecraft SMP Weihnachtsdorf VMs on Proxmox VE
# - VM 119 (pterodactyl-node1) on pnode01 (192.168.1.199)
# - VM 120 (mc-main) on pnode02 (192.168.1.200)

set -euo pipefail

PNODE01_IP="192.168.1.199"
PNODE02_IP="192.168.1.200"
IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
IMAGE_NAME="noble-server-cloudimg-amd64.img"

echo "=== 1. Preparing Cloud-Init Image ==="
for node in "${PNODE01_IP}" "${PNODE02_IP}"; do
  echo "Checking cloud image on ${node}..."
  ssh "root@${node}" "test -f /var/lib/vz/template/iso/${IMAGE_NAME} || wget -c '${IMAGE_URL}' -O /var/lib/vz/template/iso/${IMAGE_NAME}"
done

echo "=== 2. Creating VM 119 (pterodactyl-node1) on pnode01 ==="
ssh "root@${PNODE01_IP}" "
if ! qm status 119 >/dev/null 2>&1; then
  qm create 119 \
    --name pterodactyl-node1 \
    --ostype l26 \
    --memory 4096 \
    --cores 2 \
    --sockets 1 \
    --cpu cputype=host \
    --net0 virtio,bridge=vmbr0 \
    --scsihw virtio-scsi-single

  qm importdisk 119 /var/lib/vz/template/iso/${IMAGE_NAME} local-lvm

  qm set 119 \
    --scsi0 local-lvm:vm-119-disk-0,discard=on,ssd=1 \
    --ide2 local-lvm:cloudinit \
    --boot order=scsi0 \
    --serial0 socket \
    --vga serial0 \
    --ciuser tigimax \
    --sshkeys /tmp/deploy_keys.pub \
    --ipconfig0 ip=192.168.1.148/24,gw=192.168.1.1 \
    --nameserver 192.168.1.173 \
    --searchdomain tigimax.mooo.com \
    --agent enabled=1

  qm disk resize 119 scsi0 40G
  echo 'VM 119 created.'
else
  echo 'VM 119 already exists.'
fi
"

echo "=== 3. Creating VM 120 (mc-main) on pnode02 ==="
ssh "root@${PNODE02_IP}" "
if ! qm status 120 >/dev/null 2>&1; then
  qm create 120 \
    --name mc-main \
    --ostype l26 \
    --memory 14336 \
    --balloon 0 \
    --cores 6 \
    --sockets 1 \
    --cpu cputype=host \
    --net0 virtio,bridge=vmbr0 \
    --scsihw virtio-scsi-single

  qm importdisk 120 /var/lib/vz/template/iso/${IMAGE_NAME} local-lvm

  qm set 120 \
    --scsi0 local-lvm:vm-120-disk-0,discard=on,iothread=1,ssd=1 \
    --ide2 local-lvm:cloudinit \
    --boot order=scsi0 \
    --serial0 socket \
    --vga serial0 \
    --ciuser tigimax \
    --sshkeys /tmp/deploy_keys.pub \
    --ipconfig0 ip=192.168.1.149/24,gw=192.168.1.1 \
    --nameserver 192.168.1.173 \
    --searchdomain tigimax.mooo.com \
    --affinity 0-5 \
    --startup order=1,up=30 \
    --agent enabled=1

  qm disk resize 120 scsi0 80G
  echo 'VM 120 created.'
else
  echo 'VM 120 already exists.'
fi
"

echo "=== Provisioning complete ==="
