# Aria Shell GTK

Experimental GTK4/libadwaita shell for AriaOS.

This project is intentionally isolated from the current Tkinter UI so the GTK redesign can progress without breaking the stable VM flow.

## Scope in this branch
- Intro screen
- Home screen
- Terminal placeholder with launch-back-to-legacy fallback
- Shared theme tokens
- Reading the same local Aria session and secret files used by the current product shell

## Not migrated yet
- Terminal websocket UI
- Session history UI
- Image attachment UI
- Control Tower auth/billing dialogs
- Root password/session privilege flow

## Dependencies
Debian/Ubuntu packages typically needed:
- `python3-gi`
- `gir1.2-gtk-4.0`
- `gir1.2-adw-1`

Optional:
- `libgtk-4-media-gstreamer`

## Run
```bash
cd services/aria-shell-gtk
PYTHONPATH=src python3 -m aria_shell_gtk
```

## Design goals
- Keep Linux underneath
- Replace the visible Aria layer only
- Preserve backend contracts while swapping UI technology
- Maintain a clean rollback path to the Tkinter shell
