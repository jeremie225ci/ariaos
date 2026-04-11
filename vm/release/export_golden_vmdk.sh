#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  export_golden_vmdk.sh [gold_vm_name] [version] [releases_dir]

Defaults:
  gold_vm_name  AriaOS-Golden
  version       YYYYMMDD_HHMMSS
  releases_dir  $HOME/AriaOS-Releases

Behavior:
  - Clones the powered-off Golden VDI to a VMDK artifact
  - Produces a versioned file plus a stable ariaos-latest.vmdk symlink
  - Writes sha256 files
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

GOLD_VM="${1:-AriaOS-Golden}"
VERSION="${2:-$(date +%Y%m%d_%H%M%S)}"
RELEASES_DIR="${3:-$HOME/AriaOS-Releases}"

retry_vbox() {
  local attempt
  for attempt in $(seq 1 8); do
    if VBoxManage "$@"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if ! retry_vbox showvminfo "$GOLD_VM" --machinereadable >/dev/null 2>&1; then
  echo "Golden VM not found: $GOLD_VM" >&2
  exit 1
fi

VM_STATE="$(retry_vbox showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}')"
if [[ "$VM_STATE" != "poweroff" ]]; then
  echo "Golden VM must be powered off before VMDK export. Current state: $VM_STATE" >&2
  exit 1
fi

CFG_FILE="$(retry_vbox showvminfo "$GOLD_VM" --machinereadable | awk -F= '/^CfgFile=/{gsub(/"/,"",$2); print $2}')"
VM_DIR="$(dirname "$CFG_FILE")"
SOURCE_VDI="$(find "$VM_DIR" -maxdepth 2 -type f -name '*.vdi' | head -n1)"
if [[ -z "$SOURCE_VDI" || ! -f "$SOURCE_VDI" ]]; then
  echo "No source VDI found under $VM_DIR" >&2
  exit 1
fi

mkdir -p "$RELEASES_DIR"

EST_BYTES="$(stat -c '%s' "$SOURCE_VDI")"
AVAIL_BYTES="$(df --output=avail -B1 "$RELEASES_DIR" | awk 'NR==2{print $1}')"
if [[ "$AVAIL_BYTES" -lt "$EST_BYTES" ]]; then
  echo "Not enough free space in $RELEASES_DIR" >&2
  echo "  Source VDI size  : $EST_BYTES bytes" >&2
  echo "  Available space  : $AVAIL_BYTES bytes" >&2
  exit 1
fi

VERSIONED_VMDK="$RELEASES_DIR/ariaos-${VERSION}.vmdk"
LATEST_VMDK="$RELEASES_DIR/ariaos-latest.vmdk"
TMP_VMDK="$RELEASES_DIR/.ariaos-${VERSION}.tmp.vmdk"

rm -f "$TMP_VMDK"

echo "Cloning $SOURCE_VDI -> $VERSIONED_VMDK"
retry_vbox clonemedium disk "$SOURCE_VDI" "$TMP_VMDK" --format VMDK --variant Standard
mv "$TMP_VMDK" "$VERSIONED_VMDK"

sha256sum "$VERSIONED_VMDK" >"$VERSIONED_VMDK.sha256"
ln -sfn "$(basename "$VERSIONED_VMDK")" "$LATEST_VMDK"
ln -sfn "$(basename "$VERSIONED_VMDK").sha256" "$LATEST_VMDK.sha256"

echo
echo "VMDK export complete:"
echo "  Versioned: $VERSIONED_VMDK"
echo "  Latest   : $LATEST_VMDK"
