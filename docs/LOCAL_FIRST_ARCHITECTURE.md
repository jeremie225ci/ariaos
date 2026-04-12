# AriaOS Local-First Architecture

This document explains the active public architecture of AriaOS.

## Product shape

- The VM is the product.
- The GTK shell is the main operator interface.
- The user brings an OpenAI API key during onboarding.
- The key stays on the VM in `~/.config/ariaos/user_secrets.json`.
- Text execution and voice execution both run against local websocket services.

## Main local services

- `backend/server.py`
  Local text runtime for Terminal Aria on `127.0.0.1:8080`.

- `backend/voice_server.py`
  Local voice runtime on `127.0.0.1:8081`.

- `backend/loopagentic.py`
  Readable local orchestration loop for one task request.

- `backend/brain.py`
  Shared OpenAI helpers for chat, speech-to-text, text-to-speech, and runtime loading.

## Prompt locations

- `backend/prompts.py`
  Main system prompt for the local backend.

- `app/aria-shell-gtk/src/aria_shell_gtk/services/prompts.py`
  Prompt used by the shell to decide long-term memory updates.

## Runtime flow

1. The user opens AriaOS / Aria Home inside the VM.
2. Onboarding checks whether an OpenAI key already exists locally.
3. The key is stored in `~/.config/ariaos/user_secrets.json`.
4. The GTK shell starts Terminal Aria.
5. `RuntimeStore` ensures the local text backend is running.
6. If voice mode is enabled, `RuntimeStore` also ensures the local voice backend is running.
7. Terminal Aria sends prompts over the local websocket protocol.
8. The backend keeps short local session memory and calls OpenAI directly from the VM.

## Main state files

- `~/.config/ariaos/user_secrets.json`
  OpenAI key, base URL, and selected model.

- `~/.config/ariaos/runtime_config.json`
  Local runtime configuration such as voice name and language.

- `~/.local/state/ariaos/`
  Backend pid files, logs, onboarding state, and runtime state.

- `~/.ariaos/data/`
  Chat history and local memory databases.

## Important note about the legacy VM

Older private VM builds used obfuscated or less explicit names such as `kernel`
or `engine`. The public repository now uses readable names like `brain.py` and
`loopagentic.py`, but these are meant to reflect the same local responsibilities
in a clearer way.
