#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  clone_to_golden_vm.sh [source_vm] [gold_vm_name] [basefolder]

Defaults:
  source_vm     AriaOS
  gold_vm_name  AriaOS-Golden
  basefolder    $HOME/VMs

Notes:
  - The source VM must be powered off.
  - This creates a separate registered VirtualBox VM for release cleanup/export.
  - The dev VM remains untouched.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd VBoxManage
require_cmd awk

SOURCE_VM="${1:-AriaOS}"
GOLD_VM="${2:-AriaOS-Golden}"
BASEFOLDER="${3:-$HOME/VMs}"
NEUTRAL_OSTYPE="${ARIA_VBOX_NEUTRAL_OSTYPE:-Other_64}"

if ! VBoxManage showvminfo "$SOURCE_VM" --machinereadable >/dev/null 2>&1; then
  echo "Source VM not found: $SOURCE_VM" >&2
  exit 1
fi

if VBoxManage showvminfo "$GOLD_VM" --machinereadable >/dev/null 2>&1; then
  echo "Target VM already exists: $GOLD_VM" >&2
  exit 1
fi

VM_STATE="$(VBoxManage showvminfo "$SOURCE_VM" --machinereadable | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}')"
if [[ "$VM_STATE" != "poweroff" ]]; then
  echo "Source VM must be powered off before cloning. Current state: $VM_STATE" >&2
  exit 1
fi

mkdir -p "$BASEFOLDER"

echo "Cloning $SOURCE_VM -> $GOLD_VM into $BASEFOLDER"
VBoxManage clonevm "$SOURCE_VM" \
  --name "$GOLD_VM" \
  --basefolder "$BASEFOLDER" \
  --register \
  --mode machine

# Keep the guest type neutral in VirtualBox Manager branding.
VBoxManage modifyvm "$GOLD_VM" --ostype "$NEUTRAL_OSTYPE"

echo
echo "Golden VM created:"
echo "  Source : $SOURCE_VM"
echo "  Target : $GOLD_VM"
echo "  Folder : $BASEFOLDER/$GOLD_VM"
echo
echo "Next:"
echo "  1. Boot $GOLD_VM"
echo "  2. Inside the guest: sudo bash vm_patches/golden_vm_cleanup.sh --arm-firstboot"
echo "  3. Power off the guest"
echo "  4. Run host_tools/export_golden_ova.sh"
