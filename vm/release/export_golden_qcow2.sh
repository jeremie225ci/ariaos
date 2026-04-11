#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  export_golden_qcow2.sh [gold_vm_name] [version] [releases_dir]

Defaults:
  gold_vm_name  AriaOS-Golden-Export
  version       YYYYMMDD_HHMMSS_utm
  releases_dir  $HOME/AriaOS-Releases

Behavior:
  - Converts the powered-off Golden VDI to a QCOW2 artifact for UTM/QEMU
  - Produces a versioned file plus a stable ariaos-latest.qcow2 symlink
  - Writes sha256 files
  - Optional env ARIA_RELEASE_BASENAME overrides the versioned filename base
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
require_cmd qemu-img
require_cmd sha256sum
require_cmd awk
require_cmd df
require_cmd stat
require_cmd find

GOLD_VM="${1:-AriaOS-Golden-Export}"
VERSION="${2:-$(date +%Y%m%d_%H%M%S)_utm}"
RELEASES_DIR="${3:-$HOME/AriaOS-Releases}"
RELEASE_BASENAME="${ARIA_RELEASE_BASENAME:-ariaos-${VERSION}}"

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

VM_INFO="$(retry_vbox showvminfo "$GOLD_VM" --machinereadable)"

pick_attached_disk() {
  local vm_info="$1"
  local override="${ARIA_QCOW_SOURCE_DISK:-}"
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi
  printf '%s\n' "$vm_info" | awk -F= '
    /^".+-[0-9]+-[0-9]+"=/ {
      key=$1
      gsub(/"/, "", key)
      if (key ~ /ImageUUID/ || key ~ /-discard-/ || key ~ /-nonrotational-/ || key ~ /-hotpluggable-/) {
        next
      }
      val=$2
      gsub(/"/, "", val)
      if (val == "none" || val ~ /\.iso$/) {
        next
      }
      print val
      exit
    }
  '
}

VM_STATE="$(printf '%s\n' "$VM_INFO" | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}')"
if [[ "$VM_STATE" != "poweroff" ]]; then
  echo "Golden VM must be powered off before QCOW2 export. Current state: $VM_STATE" >&2
  exit 1
fi

SOURCE_VDI="$(pick_attached_disk "$VM_INFO")"
if [[ -z "$SOURCE_VDI" || ! -f "$SOURCE_VDI" ]]; then
  echo "No attached source disk found for $GOLD_VM" >&2
  exit 1
fi

mkdir -p "$RELEASES_DIR"

SOURCE_BYTES="$(stat -c '%s' "$SOURCE_VDI")"
# QCOW2 is sparse; requiring 100% of the VDI size overestimates the needed headroom.
# Keep a conservative floor while allowing exports on hosts with modest free space.
EST_BYTES=$(( SOURCE_BYTES * 65 / 100 ))
MIN_BYTES=$(( 8 * 1024 * 1024 * 1024 ))
if [[ "$EST_BYTES" -lt "$MIN_BYTES" ]]; then
  EST_BYTES="$MIN_BYTES"
fi
AVAIL_BYTES="$(df --output=avail -B1 "$RELEASES_DIR" | awk 'NR==2{print $1}')"
if [[ "$AVAIL_BYTES" -lt "$EST_BYTES" ]]; then
  echo "Not enough free space in $RELEASES_DIR" >&2
  echo "  Source VDI size  : $SOURCE_BYTES bytes" >&2
  echo "  Required headroom: $EST_BYTES bytes" >&2
  echo "  Available space  : $AVAIL_BYTES bytes" >&2
  exit 1
fi

VERSIONED_QCOW2="$RELEASES_DIR/${RELEASE_BASENAME}.qcow2"
LATEST_QCOW2="$RELEASES_DIR/ariaos-latest.qcow2"
TMP_QCOW2="$RELEASES_DIR/.${RELEASE_BASENAME}.tmp.qcow2"

rm -f "$TMP_QCOW2"

echo "Converting $SOURCE_VDI -> $VERSIONED_QCOW2"
qemu-img convert -p -c -S 4k -O qcow2 "$SOURCE_VDI" "$TMP_QCOW2"
mv "$TMP_QCOW2" "$VERSIONED_QCOW2"
chmod 644 "$VERSIONED_QCOW2"

sha256sum "$VERSIONED_QCOW2" >"$VERSIONED_QCOW2.sha256"
ln -sfn "$(basename "$VERSIONED_QCOW2")" "$LATEST_QCOW2"
ln -sfn "$(basename "$VERSIONED_QCOW2").sha256" "$LATEST_QCOW2.sha256"

echo
echo "QCOW2 export complete:"
echo "  Versioned: $VERSIONED_QCOW2"
echo "  Latest   : $LATEST_QCOW2"
