# Aria Shell GTK

GTK4/libadwaita shell for the new local-first AriaOS line.

This shell is now the active UI direction for the public AriaOS repository. It provides the onboarding flow, the home screen, and the Terminal Aria experience used by the VM launcher in this repository.

## Current scope

- local-first onboarding
- local OpenAI key setup
- Aria Home
- Terminal Aria session history
- image attachment support
- local runtime connection handling
- long-term memory integration

## Local-first assumptions

- no mandatory hosted account flow
- no required Control Tower dependency in the main VM path
- OpenAI API key is provided by the user and stored locally on the VM
- terminal execution is driven by the local runtime

## Dependencies

Debian/Ubuntu packages typically needed:

- `python3-gi`
- `gir1.2-gtk-4.0`
- `gir1.2-adw-1`

Optional:

- `libgtk-4-media-gstreamer`

## Run

```bash
cd app/aria-shell-gtk
PYTHONPATH=src python3 -m aria_shell_gtk
```

## Notes

- the shell reads local AriaOS state from the VM user profile
- the launcher used by the VM guest files lives in `vm/guest/bin/run_aria_shell_gtk.sh`
- the public shell now targets a pure local VM architecture
