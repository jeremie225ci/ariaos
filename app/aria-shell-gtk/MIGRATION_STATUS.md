# AriaOS Local Runtime Status

## Current state

- GTK4/libadwaita shell scaffold
- onboarding flow
- Aria Home
- Terminal Aria session UI
- SQLite-backed session history
- local OpenAI key dialog
- embedded websocket backend
- VM launcher path for the GTK shell
- onboarding gate based on the saved OpenAI key
- remote terminal orchestration removed from the public shell
- legacy remote service layer removed from the public shell

## Removed from the public build

- mandatory account creation
- mandatory hosted sign-in
- Control Tower dependency for first use
- remote account unlock gate before opening Terminal Aria
- legacy broker and node service modules

## Still being cleaned

- naming and messaging inherited from the old private product line
- packaging and release docs for the public VM distribution
- removal of old fallback paths that no longer match the product direction

## Immediate next targets

1. finish cleaning old naming inherited from the private line
2. document VM build, install, and first-run flow
3. simplify release tooling around the local-first AriaOS image
4. keep trimming fallback code that is no longer part of the shipped VM path
