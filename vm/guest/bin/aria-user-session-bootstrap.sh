#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ariaos"
BOOTSTRAP_STAMP="$STATE_DIR/session_bootstrap_v1"
CLIENT_DIR="${ARIA_CLIENT_DIR:-/opt/aria-client}"
APP_DIR="${CLIENT_DIR}/share/AriaApp"
XFCONF_DIR="$HOME/.config/xfce4/xfconf/xfce-perchannel-xml"
PANEL_DIR="$HOME/.config/xfce4/panel"
APPS_DIR="$HOME/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [[ -z "$DESKTOP_DIR" ]]; then
  DESKTOP_DIR="$HOME/Desktop"
fi
MEMORY_TEMPLATE="$APP_DIR/aria-memory-template.md"

mark_launcher_trusted() {
  local launcher="$1"
  local checksum
  checksum="$(sha256sum "$launcher" | awk '{print $1}')"
  chmod +x "$launcher" 2>/dev/null || true
  gio set "$launcher" metadata::trusted true 2>/dev/null || true
  gio set -t string "$launcher" metadata::xfce-exe-checksum "$checksum" 2>/dev/null || true
}

xfconf_set() {
  local channel="$1"
  local path="$2"
  local type="$3"
  local value="$4"
  xfconf-query -c "$channel" -p "$path" -n -t "$type" -s "$value" 2>/dev/null \
    || xfconf-query -c "$channel" -p "$path" -s "$value" 2>/dev/null \
    || true
}

wait_for_xfconf() {
  local attempt
  for attempt in $(seq 1 30); do
    if xfconf-query -c xfce4-desktop -lv >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

collect_backdrop_monitors() {
  {
    printf '%s\n' monitor0 monitor1 monitorVirtual1 Virtual1
    xrandr --listmonitors 2>/dev/null | awk 'NR>1 {print $4}' || true
    xfconf-query -c xfce4-desktop -lv 2>/dev/null | sed -n 's#^/backdrop/screen0/\\([^/]*\\)/.*#\\1#p' || true
  } | awk 'NF && !seen[$0]++'
}

ensure_xfdesktop() {
  local attempt
  if pgrep -x xfdesktop >/dev/null 2>&1; then
    return 0
  fi
  if command -v xfdesktop >/dev/null 2>&1; then
    nohup xfdesktop >/tmp/aria-user-session-bootstrap-xfdesktop-launch.log 2>&1 </dev/null &
  fi
  for attempt in $(seq 1 15); do
    if pgrep -x xfdesktop >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

apply_wallpaper_settings() {
  while IFS= read -r monitor; do
    [[ -n "$monitor" ]] || continue
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/image-path" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/last-image" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/last-single-image" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/image-path" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/last-image" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/last-single-image" string "/usr/share/backgrounds/aria-shell-wallpaper.png"
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/image-show" bool true
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/image-style" int 5
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/color-style" int 0
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/image-show" bool true
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/image-style" int 5
    xfconf_set xfce4-desktop "/backdrop/screen0/${monitor}/workspace0/color-style" int 0
  done < <(collect_backdrop_monitors)
}

reload_xfdesktop() {
  if pgrep -x xfdesktop >/dev/null 2>&1; then
    xfdesktop -R >/tmp/aria-user-session-bootstrap-xfdesktop.log 2>&1 || true
  fi
}

delayed_wallpaper_reapply() {
  (
    sleep 8
    wait_for_xfconf || true
    ensure_xfdesktop || true
    apply_wallpaper_settings
    reload_xfdesktop
  ) >/tmp/aria-user-session-bootstrap-delayed.log 2>&1 &
}

sync_panel_launcher() {
  local app_launcher="$1"
  local panel_launcher="$2"
  install -m 0644 "$app_launcher" "$panel_launcher"
  sed -i '/^X-XFCE-Source=/d' "$panel_launcher"
  printf 'X-XFCE-Source=file://%s\n' "$app_launcher" >> "$panel_launcher"
}

set_panel_items() {
  local plugin_id="$1"
  shift
  local args=()
  local item
  xfconf-query -c xfce4-panel -p "/plugins/plugin-${plugin_id}/items" -rR 2>/dev/null || true
  for item in "$@"; do
    args+=(-t string -s "$item")
  done
  xfconf-query -c xfce4-panel -p "/plugins/plugin-${plugin_id}/items" -n -a "${args[@]}" 2>/dev/null || true
}

mkdir -p "$STATE_DIR" "$XFCONF_DIR" "$PANEL_DIR" "$APPS_DIR" "$DESKTOP_DIR"
mkdir -p "$PANEL_DIR/launcher-2" "$PANEL_DIR/launcher-3" "$PANEL_DIR/launcher-4" "$PANEL_DIR/launcher-5"

[[ -f "$APP_DIR/xfce4-panel.aria.xml" ]] && install -m 0644 "$APP_DIR/xfce4-panel.aria.xml" "$XFCONF_DIR/xfce4-panel.xml"
[[ -f "$APP_DIR/xfce4-desktop.aria.xml" ]] && install -m 0644 "$APP_DIR/xfce4-desktop.aria.xml" "$XFCONF_DIR/xfce4-desktop.xml"
[[ -f "$APP_DIR/xsettings.aria.xml" ]] && install -m 0644 "$APP_DIR/xsettings.aria.xml" "$XFCONF_DIR/xsettings.xml"

for name in aria-home browser system-terminal aria-terminal files; do
  if [[ -f "$APPS_DIR/${name}.desktop" ]]; then
    install -m 0644 "$APPS_DIR/${name}.desktop" "$DESKTOP_DIR/${name}.desktop"
    mark_launcher_trusted "$DESKTOP_DIR/${name}.desktop"
  fi
done

if [[ -f "$APPS_DIR/aria-home.desktop" ]]; then
  sync_panel_launcher "$APPS_DIR/aria-home.desktop" "$PANEL_DIR/launcher-2/aria-home.desktop"
fi
if [[ -f "$APPS_DIR/browser.desktop" ]]; then
  sync_panel_launcher "$APPS_DIR/browser.desktop" "$PANEL_DIR/launcher-3/browser.desktop"
fi
if [[ -f "$APPS_DIR/system-terminal.desktop" ]]; then
  sync_panel_launcher "$APPS_DIR/system-terminal.desktop" "$PANEL_DIR/launcher-4/system-terminal.desktop"
fi
if [[ -f "$APPS_DIR/aria-terminal.desktop" ]]; then
  sync_panel_launcher "$APPS_DIR/aria-terminal.desktop" "$PANEL_DIR/launcher-5/aria-terminal.desktop"
fi

wait_for_xfconf || true
ensure_xfdesktop || true
apply_wallpaper_settings
sleep 1
reload_xfdesktop
sleep 2
wait_for_xfconf || true
ensure_xfdesktop || true
apply_wallpaper_settings
if pgrep -x xfdesktop >/dev/null 2>&1; then
  xfdesktop -R >/tmp/aria-user-session-bootstrap-xfdesktop-2.log 2>&1 || true
fi
delayed_wallpaper_reapply

xfconf_set xfce4-desktop /desktop-icons/style int 2
xfconf_set xfce4-desktop /desktop-icons/file-icons/show-home bool false
xfconf_set xfce4-desktop /desktop-icons/file-icons/show-filesystem bool false
xfconf_set xfce4-desktop /desktop-icons/file-icons/show-trash bool false
xfconf_set xfce4-desktop /desktop-icons/file-icons/show-removable bool false

xfconf-query -c xsettings -p /Net/ThemeName -s "Adwaita-dark" 2>/dev/null || true
xfconf-query -c xsettings -p /Net/IconThemeName -s "AriaMono" 2>/dev/null || true
xfconf-query -c xsettings -p /Gtk/CursorThemeName -s "Adwaita" 2>/dev/null || true
xfconf-query -c xsettings -p /Gtk/FontName -s "DejaVu Sans 10" 2>/dev/null || true
xfconf-query -c xfwm4 -p /general/theme -s "Default" 2>/dev/null || true
xfconf-query -c xfwm4 -p /general/title_font -s "DejaVu Sans Bold 9" 2>/dev/null || true

set_panel_items 2 aria-home.desktop
set_panel_items 3 browser.desktop
set_panel_items 4 system-terminal.desktop
set_panel_items 5 aria-terminal.desktop

touch "$BOOTSTRAP_STAMP"
