#!/usr/bin/env bash
# Provisioning script for Minecraft SMP Weihnachtsdorf VMs on Proxmox VE
# - VM 119 (pterodactyl-node1) on pnode01 (192.168.1.199)
# - VM 120 (mc-main) on pnode02 (192.168.1.200)

set -euo pipefail

PNODE01_IP="192.168.1.199"
PNODE02_IP="192.168.1.200"
IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
IMAGE_NAME="noble-server-cloudimg-amd64.img"
SSH_PUB_KEY="${HOME}/.ssh/id_ed25519.pub"

if [[ ! -f "${SSH_PUB_KEY}" ]]; then
  echo "Error: SSH public key ${SSH_PUB_KEY} not found!" >&2
  exit 1
fi

echo "=== 1. Preparing Cloud-Init Image & SSH Keys ==="
for node in "${PNODE01_IP}" "${PNODE02_IP}"; do
  echo "Checking cloud image on ${node}..."
  ssh "root@${node}" "test -f /var/lib/vz/template/iso/${IMAGE_NAME} || wget -c '${IMAGE_URL}' -O /var/lib/vz/template/iso/${IMAGE_NAME}"
  echo "Syncing SSH public key to ${node}:/tmp/deploy_keys.pub..."
  scp "${SSH_PUB_KEY}" "root@${node}:/tmp/deploy_keys.pub"
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
  qm start 119
  echo 'VM 119 created and started.'
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
  qm start 120
  echo 'VM 120 created and started.'
else
  echo 'VM 120 already exists.'
fi
"

echo "=== 4. Ensuring QEMU Guest Agent on guests ==="
for guest_ip in "192.168.1.148" "192.168.1.149"; do
  echo "Checking guest agent on ${guest_ip}..."
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "tigimax@${guest_ip}" "
    if ! systemctl is-active --quiet qemu-guest-agent; then
      sudo apt-get update && sudo apt-get install -y qemu-guest-agent && sudo systemctl start qemu-guest-agent
    fi
  " || echo "Warning: Could not connect to ${guest_ip} yet (still booting?)."
done

echo "=== Provisioning complete ==="
