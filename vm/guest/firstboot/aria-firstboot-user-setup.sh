#!/usr/bin/env bash
set -euo pipefail

MARKER_PATH="${ARIA_FIRSTBOOT_MARKER:-/var/lib/ariaos/firstboot-user-setup}"
COMPLETE_PATH="${ARIA_FIRSTBOOT_COMPLETE:-/var/lib/ariaos/firstboot-user-setup-complete}"
ALLOW_EXISTING_USERS="${ARIA_FIRSTBOOT_ALLOW_EXISTING_USERS:-0}"
TTY_DEVICE="${ARIA_FIRSTBOOT_TTY:-/dev/tty1}"
SKIP_REBOOT="${ARIA_FIRSTBOOT_SKIP_REBOOT:-0}"
RESERVED_USERNAMES="/usr/lib/user-setup/reserved-usernames"

real_users() {
  awk -F: '$3 >= 1000 && $1 != "nobody" { print $1 }' /etc/passwd
}

fail_dialog() {
  whiptail --title "AriaOS setup" --msgbox "$1" 10 72 || true
}

input_box() {
  local title="$1"
  local prompt="$2"
  local default="${3:-}"
  whiptail --title "$title" --inputbox "$prompt" 11 72 "$default" 3>&1 1>&2 2>&3
}

password_box() {
  local title="$1"
  local prompt="$2"
  whiptail --title "$title" --passwordbox "$prompt" 11 72 3>&1 1>&2 2>&3
}

focus_setup_tty() {
  local tty_number="$1"
  local attempt

  for attempt in 1 2 3 4 5; do
    command -v plymouth >/dev/null 2>&1 && plymouth quit >/dev/null 2>&1 || true
    chvt "$tty_number" >/dev/null 2>&1 || true
    sleep 1
  done
}

is_valid_username() {
  local candidate="$1"
  [[ "$candidate" =~ ^[a-z][-a-z0-9_]*$ ]] || return 1
  [[ ${#candidate} -le 32 ]] || return 1
  if [[ -f "$RESERVED_USERNAMES" ]] && grep -q "^${candidate}$" "$RESERVED_USERNAMES"; then
    return 1
  fi
  ! id -u "$candidate" >/dev/null 2>&1
}

create_user() {
  local username="$1"
  local fullname="$2"
  local password="$3"

  adduser --disabled-password --gecos "$fullname" "$username"
  printf '%s:%s\n' "$username" "$password" | chpasswd
  adduser "$username" sudo >/dev/null 2>&1 || true
}

seed_local_admin_secret() {
  local username="$1"
  local password="$2"
  local user_home
  local config_dir
  local secret_path
  local stamp_path
  user_home="$(getent passwd "$username" | cut -d: -f6)"
  [[ -n "$user_home" && -d "$user_home" ]] || return 0

  config_dir="$user_home/.config/ariaos"
  secret_path="$config_dir/local_agent_secrets.json"
  stamp_path="$config_dir/local_secret_stamp"
  if [[ -f "$secret_path" ]]; then
    return 0
  fi

  install -d -o "$username" -g "$username" -m 0700 "$config_dir"
  ARIA_SECRET_PATH="$secret_path" ARIA_SECRET_STAMP="$stamp_path" ARIA_VM_ADMIN_PASSWORD="$password" python3 - <<'PY'
import json
import os
import time
from pathlib import Path

secret_path = Path(os.environ["ARIA_SECRET_PATH"])
stamp_path = Path(os.environ["ARIA_SECRET_STAMP"])
password = os.environ["ARIA_VM_ADMIN_PASSWORD"]
secret_path.write_text(json.dumps({"vm_admin_password": password}, indent=2), encoding="utf-8")
stamp_path.write_text(str(time.time()), encoding="utf-8")
PY
  chown "$username:$username" "$secret_path" "$stamp_path"
  chmod 0600 "$secret_path" "$stamp_path"
}

seed_user_branding() {
  local username="$1"
  local user_home
  user_home="$(getent passwd "$username" | cut -d: -f6)"
  [[ -n "$user_home" && -d "$user_home" ]] || return 0

  install -d -o "$username" -g "$username" -m 0755 \
    "$user_home/.config/xfce4/xfconf/xfce-perchannel-xml" \
    "$user_home/.config/autostart" \
    "$user_home/.local/bin" \
    "$user_home/Desktop"

  for name in xfce4-desktop.xml xfce4-panel.xml xsettings.xml; do
    if [[ -f "/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/${name}" ]]; then
      install -o "$username" -g "$username" -m 0644 \
        "/etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/${name}" \
        "$user_home/.config/xfce4/xfconf/xfce-perchannel-xml/${name}"
    fi
  done

  for name in aria-user-session-bootstrap.desktop aria-home.desktop aria-session-env.desktop aria-polkit.desktop aria-keep-display-awake.desktop light-locker.desktop; do
    if [[ -f "/etc/skel/.config/autostart/${name}" ]]; then
      install -o "$username" -g "$username" -m 0644 \
        "/etc/skel/.config/autostart/${name}" \
        "$user_home/.config/autostart/${name}"
    fi
  done

  if [[ -f /etc/skel/.local/bin/aria-user-session-bootstrap ]]; then
    install -o "$username" -g "$username" -m 0755 \
      /etc/skel/.local/bin/aria-user-session-bootstrap \
      "$user_home/.local/bin/aria-user-session-bootstrap"
  fi

  if [[ -f "/etc/skel/Desktop/Aria Memory.md" && ! -f "$user_home/Desktop/Aria Memory.md" ]]; then
    install -o "$username" -g "$username" -m 0644 \
      "/etc/skel/Desktop/Aria Memory.md" \
      "$user_home/Desktop/Aria Memory.md"
  fi

  install -d -m 0755 /var/lib/AccountsService/users
  cat >"/var/lib/AccountsService/users/${username}" <<EOF
[User]
SystemAccount=false
BackgroundFile='/usr/share/backgrounds/aria-shell-wallpaper.png'
Icon='/usr/share/pixmaps/aria-logo.png'
EOF
  chmod 0644 "/var/lib/AccountsService/users/${username}"
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "aria-firstboot-user-setup.sh must run as root." >&2
  exit 1
fi

if [[ ! -f "$MARKER_PATH" ]]; then
  exit 0
fi

rm -f "$COMPLETE_PATH"

if ! command -v whiptail >/dev/null 2>&1; then
  echo "whiptail is required for AriaOS first-boot setup." >&2
  exit 1
fi

if [[ "$TTY_DEVICE" =~ ^/dev/tty([0-9]+)$ ]] && command -v chvt >/dev/null 2>&1; then
  focus_setup_tty "${BASH_REMATCH[1]}" &
fi

exec <"$TTY_DEVICE" >"$TTY_DEVICE" 2>&1
export TERM="${TERM:-linux}"
clear

existing_users="$(real_users || true)"
if [[ -n "$existing_users" && "$ALLOW_EXISTING_USERS" != "1" ]]; then
  fail_dialog "AriaOS first-boot setup is armed, but the machine still has an existing Linux user: ${existing_users}. Clean the image before enabling first boot setup."
  exit 1
fi

whiptail \
  --title "Welcome to AriaOS" \
  --msgbox "Create the Linux account that will own this machine. After this step, AriaOS will reboot to the normal login screen. Aria onboarding starts after you log in." \
  12 78 || true

while true; do
  full_name="$(input_box "Welcome to AriaOS" "Full name" "")" || continue
  full_name="$(printf '%s' "$full_name" | sed -e 's/^ *//' -e 's/ *$//')"
  if [[ -z "$full_name" ]]; then
    fail_dialog "Full name cannot be empty."
    continue
  fi

  username="$(input_box "Welcome to AriaOS" "Choose a Linux username" "$(printf '%s' "$full_name" | awk '{print tolower($1)}')")" || continue
  username="$(printf '%s' "$username" | tr '[:upper:]' '[:lower:]' | sed -e 's/^ *//' -e 's/ *$//')"
  if ! is_valid_username "$username"; then
    fail_dialog "Choose a different username. Use lowercase letters, numbers, dashes, or underscores, and avoid reserved or existing names."
    continue
  fi

  password_one="$(password_box "Welcome to AriaOS" "Choose a password for ${username}")" || continue
  password_two="$(password_box "Welcome to AriaOS" "Repeat the password for ${username}")" || continue
  if [[ -z "$password_one" ]]; then
    fail_dialog "Password cannot be empty."
    continue
  fi
  if [[ "$password_one" != "$password_two" ]]; then
    fail_dialog "Passwords do not match."
    continue
  fi

  if ! whiptail --title "Confirm account" --yesno "Create Linux user '${username}' now?" 10 64; then
    continue
  fi

  create_user "$username" "$full_name" "$password_one"
  seed_local_admin_secret "$username" "$password_one"
  seed_user_branding "$username"
  install -d -m 0755 /var/lib/ariaos
  rm -f "$MARKER_PATH"
  touch "$COMPLETE_PATH"
  systemctl set-default graphical.target >/dev/null 2>&1 || true
  passwd -l root >/dev/null 2>&1 || true

  if [[ "$SKIP_REBOOT" == "1" ]]; then
    whiptail --title "AriaOS setup complete" --msgbox "Linux account '${username}' has been created. Reboot is skipped because ARIA_FIRSTBOOT_SKIP_REBOOT=1." 10 72
    exit 0
  fi

  whiptail --title "AriaOS setup complete" --msgbox "Linux account '${username}' has been created. The system will reboot to the normal login screen." 10 72
  systemctl reboot
  exit 0
done
