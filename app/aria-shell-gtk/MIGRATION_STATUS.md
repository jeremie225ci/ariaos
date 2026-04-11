# GTK4 Migration Status

## Done in `gtk4-redesign`
- Dedicated branch created
- Isolated GTK4/libadwaita shell scaffold created
- Shared theme CSS created
- Intro screen scaffold created
- Home screen scaffold created
- Real GTK terminal browser created
- Live websocket prompt bridge added to the GTK terminal
- Existing local Aria session/key files are read by the GTK shell
- SQLite session history is mirrored into the GTK terminal
- Optional launcher with fallback to Tkinter created

## Still using the stable Tk stack
- Image attachment flow
- Control Tower auth modal flow
- Billing modal flow
- API key entry flow
- VM default launcher

## Safe testing strategy
1. Keep Tkinter as the default shell
2. Install GTK runtime dependencies in the VM
3. Launch `run_aria_shell_gtk.sh` manually
4. Validate Intro, Home, and Terminal prompt flow
5. Port image upload and remaining access modals after regression pass

## Runtime dependencies to install in the VM
- python3-gi
- gir1.2-gtk-4.0
- gir1.2-adw-1

## Next migration targets
1. Port image attachment into the GTK composer
2. Port access/billing/account states one-to-one
3. Replace the legacy terminal launch fallback
4. Add title generation polish and runtime notifications
5. Switch launcher only after full regression pass
