#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  prepare_release_blank_vm.sh [source_vm] [release_vm] [basefolder]

Defaults:
  source_vm   AriaOS
  release_vm  AriaOS-Release-Blank
  basefolder  $HOME/VMs

Environment:
  VM_USER               Guest Linux user to clean up. Default: jeremie
  VM_PASS               Guest SSH/sudo password. Required.
  VM_HOST               Guest SSH host. Default: 127.0.0.1
  VM_PORT               Guest SSH port for the cloned VM. Default: 2223
  VM_START_TYPE         VirtualBox start type. Default: headless
  ARIA_EXPORT_FORMAT    none|ova|qcow2|vmdk|all. Default: none
  ARIA_RELEASE_VERSION  Export version passed to export scripts. Default: YYYYMMDD_HHMMSS
  ARIA_RELEASES_DIR     Export destination. Default: $HOME/AriaOS-Releases
  ARIA_VBOX_NEUTRAL_OSTYPE  Default: Linux26_64

Behavior:
  - requires the source VM to be powered off
  - clones the dev VM to a new registered release VM
  - rebinds guest SSH forwarding to VM_PORT on the clone
  - boots the clone, waits for SSH, and cleans it for export
  - arms first boot OS user creation and removes the placeholder user now
  - powers the clone off
  - optionally exports the powered-off release VM
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
require_cmd sshpass
require_cmd ssh
require_cmd scp
require_cmd mktemp

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_VM="${1:-AriaOS}"
RELEASE_VM="${2:-AriaOS-Release-Blank}"
BASEFOLDER="${3:-$HOME/VMs}"
VM_USER="${VM_USER:-jeremie}"
VM_PASS="${VM_PASS:-}"
VM_HOST="${VM_HOST:-127.0.0.1}"
VM_PORT="${VM_PORT:-2223}"
VM_START_TYPE="${VM_START_TYPE:-headless}"
EXPORT_FORMAT="${ARIA_EXPORT_FORMAT:-none}"
RELEASE_VERSION="${ARIA_RELEASE_VERSION:-$(date +%Y%m%d_%H%M%S)}"
RELEASES_DIR="${ARIA_RELEASES_DIR:-$HOME/AriaOS-Releases}"
NEUTRAL_OSTYPE="${ARIA_VBOX_NEUTRAL_OSTYPE:-Linux26_64}"
LOCAL_CLEANUP_SCRIPT="$ROOT_DIR/vm_patches/golden_vm_cleanup.sh"
REMOTE_CLEANUP_SCRIPT="/tmp/aria-golden-vm-cleanup.sh"
REMOTE_FINALIZE_SCRIPT="/tmp/aria-finalize-release-blank.sh"
START_TIMEOUT_SECONDS=240
POWEROFF_TIMEOUT_SECONDS=180

if [[ -z "$VM_PASS" ]]; then
  echo "VM_PASS is required." >&2
  exit 1
fi

if [[ ! -f "$LOCAL_CLEANUP_SCRIPT" ]]; then
  echo "Missing local cleanup script: $LOCAL_CLEANUP_SCRIPT" >&2
  exit 1
fi

vm_exists() {
  VBoxManage showvminfo "$1" --machinereadable >/dev/null 2>&1
}

vm_state() {
  VBoxManage showvminfo "$1" --machinereadable | awk -F= '/^VMState=/{gsub(/"/,"",$2); print $2}'
}

wait_for_ssh() {
  local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if sshpass -p "$VM_PASS" ssh -T \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=5 \
      -p "$VM_PORT" \
      "$VM_USER@$VM_HOST" "echo ssh-ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

wait_for_poweroff() {
  local deadline=$((SECONDS + POWEROFF_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if [[ "$(vm_state "$RELEASE_VM")" == "poweroff" ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

SSH=(sshpass -p "$VM_PASS" ssh -T -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -p "$VM_PORT" "$VM_USER@$VM_HOST")
SCP=(sshpass -p "$VM_PASS" scp -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P "$VM_PORT")

if ! vm_exists "$SOURCE_VM"; then
  echo "Source VM not found: $SOURCE_VM" >&2
  exit 1
fi

if vm_exists "$RELEASE_VM"; then
  echo "Target VM already exists: $RELEASE_VM" >&2
  exit 1
fi

if [[ "$(vm_state "$SOURCE_VM")" != "poweroff" ]]; then
  echo "Source VM must be powered off before cloning: $SOURCE_VM" >&2
  exit 1
fi

mkdir -p "$BASEFOLDER"

echo "Cloning $SOURCE_VM -> $RELEASE_VM"
VBoxManage clonevm "$SOURCE_VM" \
  --name "$RELEASE_VM" \
  --basefolder "$BASEFOLDER" \
  --register \
  --mode machine

VBoxManage modifyvm "$RELEASE_VM" --ostype "$NEUTRAL_OSTYPE"
VBoxManage modifyvm "$RELEASE_VM" --natpf1 delete ssh >/dev/null 2>&1 || true
VBoxManage modifyvm "$RELEASE_VM" --natpf1 "ssh,tcp,,$VM_PORT,,22"

echo "Starting cloned VM: $RELEASE_VM"
VBoxManage startvm "$RELEASE_VM" --type "$VM_START_TYPE" >/dev/null

echo "Waiting for SSH on $VM_HOST:$VM_PORT"
if ! wait_for_ssh; then
  echo "SSH did not become ready on the cloned VM." >&2
  exit 1
fi

echo "Uploading cleanup tooling to the cloned VM"
"${SCP[@]}" "$LOCAL_CLEANUP_SCRIPT" "$VM_USER@$VM_HOST:$REMOTE_CLEANUP_SCRIPT"

FINALIZE_LOCAL="$(mktemp)"
cleanup_local() {
  rm -f "$FINALIZE_LOCAL"
}
trap cleanup_local EXIT

cat >"$FINALIZE_LOCAL" <<EOF
#!/usr/bin/env bash
set -euo pipefail
bash "$REMOTE_CLEANUP_SCRIPT" --target-user="$VM_USER" --arm-firstboot --remove-user-now
sync
/sbin/poweroff
EOF

"${SCP[@]}" "$FINALIZE_LOCAL" "$VM_USER@$VM_HOST:$REMOTE_FINALIZE_SCRIPT"

echo "Preparing blank release image inside the cloned VM"
"${SSH[@]}" "printf '%s\n' '$VM_PASS' | sudo -S -p '' install -m 0755 '$REMOTE_CLEANUP_SCRIPT' '$REMOTE_CLEANUP_SCRIPT'"
"${SSH[@]}" "printf '%s\n' '$VM_PASS' | sudo -S -p '' install -m 0755 '$REMOTE_FINALIZE_SCRIPT' '$REMOTE_FINALIZE_SCRIPT'"
"${SSH[@]}" "printf '%s\n' '$VM_PASS' | sudo -S -p '' nohup '$REMOTE_FINALIZE_SCRIPT' >/tmp/aria-finalize-release-blank.log 2>&1 < /dev/null &"

echo "Waiting for the cloned VM to power off after cleanup"
if ! wait_for_poweroff; then
  echo "Cloned VM did not power off after cleanup. Check it before exporting." >&2
  exit 1
fi

echo "Release clone is ready: $RELEASE_VM"

case "$EXPORT_FORMAT" in
  none)
    ;;
  ova)
    "$ROOT_DIR/host_tools/export_golden_ova.sh" "$RELEASE_VM" "$RELEASE_VERSION" "$RELEASES_DIR"
    ;;
  qcow2)
    "$ROOT_DIR/host_tools/export_golden_qcow2.sh" "$RELEASE_VM" "${RELEASE_VERSION}_utm" "$RELEASES_DIR"
    ;;
  vmdk)
    "$ROOT_DIR/host_tools/export_golden_vmdk.sh" "$RELEASE_VM" "$RELEASE_VERSION" "$RELEASES_DIR"
    ;;
  all)
    "$ROOT_DIR/host_tools/export_golden_ova.sh" "$RELEASE_VM" "$RELEASE_VERSION" "$RELEASES_DIR"
    "$ROOT_DIR/host_tools/export_golden_qcow2.sh" "$RELEASE_VM" "${RELEASE_VERSION}_utm" "$RELEASES_DIR"
    "$ROOT_DIR/host_tools/export_golden_vmdk.sh" "$RELEASE_VM" "$RELEASE_VERSION" "$RELEASES_DIR"
    ;;
  *)
    echo "Unknown ARIA_EXPORT_FORMAT: $EXPORT_FORMAT" >&2
    exit 1
    ;;
esac

echo "Done."
