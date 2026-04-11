# AriaOS Local-First Migration Status

## Already migrated

- GTK4/libadwaita shell scaffold
- onboarding flow
- Aria Home
- Terminal Aria session UI
- SQLite-backed session history
- local OpenAI key dialog
- VM launcher path for the GTK shell
- local-first onboarding gate based on the saved OpenAI key

## Intentionally removed from the main flow

- mandatory account creation
- mandatory hosted sign-in
- Control Tower dependency for first use
- remote account unlock gate before opening Terminal Aria

## Still being cleaned

- legacy hosted modules left in the tree but no longer used by the main flow
- naming and messaging inherited from the old private product line
- packaging and release docs for the public VM distribution
- removal of old fallback paths that no longer match the product direction

## Immediate next targets

1. continue deleting unused hosted/control-plane code
2. document VM build, install, and first-run flow
3. simplify release tooling around the local-first AriaOS image
4. reduce duplicated legacy modules and dialogs
