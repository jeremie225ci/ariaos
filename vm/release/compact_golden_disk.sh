#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  compact_golden_disk.sh [gold_vm_name]

Default:
  gold_vm_name  AriaOS-Golden

Notes:
  - Run this after zero-filling free space inside the guest.
  - The VM must be powered off.
  - Works on VDI disks only.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v VBoxManage >/dev/null 2>&1; then
  echo "Missing required command: VBoxManage" >&2
  exit 1
fi

GOLD_VM="${1:-AriaOS-Golden}"

if ! VBoxManage showvminfo "$GOLD_VM" --machinereadable >/dev/null 2>&1; then
  echo "Golden VM not found: $GOLD_VM" >&2
  exit 1
fi

VM_STATE="$(VBoxManage showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}')"
if [[ "$VM_STATE" != "poweroff" ]]; then
  echo "Golden VM must be powered off before compaction. Current state: $VM_STATE" >&2
  exit 1
fi

CFG_FILE="$(VBoxManage showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^CfgFile=/{gsub(/"/,"",$2); print $2}')"
VM_DIR="$(dirname "$CFG_FILE")"

FOUND=0
while IFS= read -r disk_file; do
  FOUND=1
  echo "Compacting $disk_file"
  VBoxManage modifymedium disk "$disk_file" --compact
done < <(find "$VM_DIR" -maxdepth 2 -type f -name '*.vdi' | sort)

if [[ "$FOUND" -eq 0 ]]; then
  echo "No VDI disks found under $VM_DIR" >&2
  exit 1
fi

echo "Compaction complete for $GOLD_VM"
