#!/usr/bin/env bash
set -euo pipefail

MARKER_PATH="${ARIA_REMOVE_PLACEHOLDER_MARKER:-/var/lib/ariaos/remove-placeholder-user}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "aria-remove-placeholder-user.sh must run as root." >&2
  exit 1
fi

if [[ ! -f "$MARKER_PATH" ]]; then
  exit 0
fi

USERNAME="$(tr -d '[:space:]' <"$MARKER_PATH" 2>/dev/null || true)"
if [[ -z "$USERNAME" ]]; then
  rm -f "$MARKER_PATH"
  exit 0
fi

loginctl terminate-user "$USERNAME" >/dev/null 2>&1 || true
pkill -KILL -u "$USERNAME" >/dev/null 2>&1 || true

if id -u "$USERNAME" >/dev/null 2>&1; then
  deluser --remove-home "$USERNAME" >/dev/null 2>&1 || userdel -r "$USERNAME" >/dev/null 2>&1 || true
fi

rm -f "/var/lib/AccountsService/users/$USERNAME" "/var/lib/AccountsService/icons/$USERNAME" || true
rm -f "$MARKER_PATH"
