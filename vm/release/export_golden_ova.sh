#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  export_golden_ova.sh [gold_vm_name] [version] [releases_dir]

Defaults:
  gold_vm_name  AriaOS-Golden-Export-OVA
  version       YYYYMMDD_HHMMSS
  releases_dir  $HOME/AriaOS-Releases

Behavior:
  - Exports a powered-off Golden VM to OVA
  - Produces a versioned file plus a stable ariaos-latest.ova symlink target
  - Writes sha256 files
  - Optional env ARIA_RELEASE_BASENAME overrides the versioned filename base
  - Optional env ARIA_OVF_VM_NAME overrides the import-facing VM name inside the OVF
  - Prints the ARIA_OVA_PATH value to use in the website
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
require_cmd sha256sum
require_cmd awk
require_cmd df
require_cmd stat
require_cmd find
require_cmd python3

GOLD_VM="${1:-AriaOS-Golden-Export-OVA}"
VERSION="${2:-$(date +%Y%m%d_%H%M%S)}"
RELEASES_DIR="${3:-$HOME/AriaOS-Releases}"
RELEASE_BASENAME="${ARIA_RELEASE_BASENAME:-ariaos-${VERSION}}"
NEUTRAL_OSTYPE="${ARIA_VBOX_NEUTRAL_OSTYPE:-Linux26_64}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVA_PATCHER="$ROOT_DIR/host_tools/patch_ova_ovf_in_place.py"

if ! VBoxManage showvminfo "$GOLD_VM" --machinereadable >/dev/null 2>&1; then
  echo "Golden VM not found: $GOLD_VM" >&2
  exit 1
fi

VM_STATE="$(VBoxManage showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}')"
if [[ "$VM_STATE" != "poweroff" ]]; then
  echo "Golden VM must be powered off before export. Current state: $VM_STATE" >&2
  exit 1
fi

# Ensure exported appliances don't expose a distro-specific VirtualBox guest label.
VBoxManage modifyvm "$GOLD_VM" --ostype "$NEUTRAL_OSTYPE"

CFG_FILE="$(VBoxManage showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^CfgFile=/{gsub(/"/,"",$2); print $2}')"
VM_DIR="$(dirname "$CFG_FILE")"
EST_BYTES=0
while IFS= read -r disk_file; do
  if [[ -f "$disk_file" ]]; then
    size="$(stat -c '%s' "$disk_file" 2>/dev/null || echo 0)"
    EST_BYTES=$((EST_BYTES + size))
  fi
done < <(find "$VM_DIR" -maxdepth 2 \( -name '*.vdi' -o -name '*.vmdk' -o -name '*.qcow2' \) -type f)

mkdir -p "$RELEASES_DIR"
AVAIL_BYTES="$(df --output=avail -B1 "$RELEASES_DIR" | awk 'NR==2{print $1}')"

if [[ "$EST_BYTES" -gt 0 && "$AVAIL_BYTES" -lt "$EST_BYTES" ]]; then
  echo "Not enough free space in $RELEASES_DIR" >&2
  echo "  Estimated disk payload: $EST_BYTES bytes" >&2
  echo "  Available space       : $AVAIL_BYTES bytes" >&2
  exit 1
fi

VERSIONED_OVA="$RELEASES_DIR/${RELEASE_BASENAME}.ova"
LATEST_OVA="$RELEASES_DIR/ariaos-latest.ova"
TMP_OVA="$RELEASES_DIR/${RELEASE_BASENAME}.exporting.ova"

cleanup() {
  rm -f "$TMP_OVA"
}
trap cleanup EXIT

echo "Exporting $GOLD_VM -> $VERSIONED_OVA"
VBoxManage export "$GOLD_VM" --output "$TMP_OVA"
python3 "$OVA_PATCHER" "$TMP_OVA"
VBoxManage import "$TMP_OVA" --dry-run >/dev/null
mv "$TMP_OVA" "$VERSIONED_OVA"
chmod 644 "$VERSIONED_OVA"

sha256sum "$VERSIONED_OVA" >"$VERSIONED_OVA.sha256"
ln -sfn "$(basename "$VERSIONED_OVA")" "$LATEST_OVA"
ln -sfn "$(basename "$VERSIONED_OVA").sha256" "$LATEST_OVA.sha256"

echo
echo "Export complete:"
echo "  Versioned: $VERSIONED_OVA"
echo "  Latest   : $LATEST_OVA"
echo
echo "Set this in your release environment:"
echo "  ARIA_OVA_PATH=$LATEST_OVA"
