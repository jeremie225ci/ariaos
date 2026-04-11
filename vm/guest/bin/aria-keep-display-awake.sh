#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

for _ in $(seq 1 20); do
  if xset q >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

xset s off -dpms s noblank >/dev/null 2>&1 || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/dpms-enabled -n -t bool -s false >/dev/null 2>&1 || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/blank-on-ac -n -t int -s 0 >/dev/null 2>&1 || true
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/blank-on-battery -n -t int -s 0 >/dev/null 2>&1 || true
