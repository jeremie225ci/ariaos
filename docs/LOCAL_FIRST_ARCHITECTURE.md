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

- `backend/agentic_runtime.py`
  Visible VM-style local brain with the multi-step planner, computer-use loop,
  scheduling actions, verification rules, and local tool execution.

- `backend/loopagentic.py`
  Thin compatibility bridge that feeds one shell request into the visible local
  agentic brain and returns the task result through the websocket protocol.

- `backend/brain.py`
  Shared OpenAI helpers for chat, speech-to-text, text-to-speech, and runtime loading.

- `backend/brain_config.py`
  Local-first config layer for the imported VM brain.

- `aria/kernel_input.py`
  Local input backend package used by the VM brain when probing kernel input or
  falling back to xdotool.

- `docs/KERNEL_AND_SURFACE_MAP.md`
  Reference document for the real kernel-level module, the old packaged name
  mapping, and the readable UI surfaces exposed by the public repo.

## Prompt locations

- `backend/prompts.py`
  Main system prompts for the local backend, including the direct-answer prompt
  and the full visible VM agentic prompt.

- `app/aria-shell-gtk/src/aria_shell_gtk/services/prompts.py`
  Prompt used by the shell to decide long-term memory updates.

## Local intelligence layers

- Long-term memory:
  a readable Markdown memory file plus a local SQLite embedding index for
  semantic summary recall.
  See `docs/LONG_TERM_MEMORY.md`.

- Voice mode:
  a local websocket service that handles microphone audio, transcription, TTS,
  and narrow task-control commands.
  See `docs/VOICE_MODE.md`.

- Future tasks:
  the planner contract already understands scheduling and task mutations, but
  the current public shell does not yet persist and execute those payloads as a
  full local scheduler.
  See `docs/FUTURE_TASKS.md`.

## Runtime flow

1. The user opens AriaOS / Aria Home inside the VM.
2. Onboarding checks whether an OpenAI key already exists locally.
3. The key is stored in `~/.config/ariaos/user_secrets.json`.
4. The GTK shell starts Terminal Aria.
5. `RuntimeStore` ensures the local text backend is running.
6. If voice mode is enabled, `RuntimeStore` also ensures the local voice backend is running.
7. Terminal Aria sends prompts over the local websocket protocol.
8. `backend/loopagentic.py` creates a local `AgenticLoop` task runtime.
9. `backend/agentic_runtime.py` runs the VM-style planner, computer-use logic,
   verification rules, and local tools directly inside the VM.
10. The backend calls OpenAI directly from the VM using the user-provided key.

## Main state files

- `~/.config/ariaos/user_secrets.json`
  OpenAI key, base URL, and selected model.

- `~/.config/ariaos/runtime_config.json`
  Local runtime configuration such as voice name and language.

- `~/.local/state/ariaos/`
  Backend pid files, logs, onboarding state, and runtime state.

- `~/.ariaos/data/`
  Chat history and local memory databases, including the SQLite memory index.

## Important note about the legacy VM

Older private VM builds used obfuscated or less explicit names such as `kernel`
or `engine`. The public repository now uses readable names like `brain.py` and
`loopagentic.py`, but these are meant to reflect the same local responsibilities
in a clearer way. The public repository now also carries a visible copy of the
VM brain in `backend/agentic_runtime.py` instead of hiding that logic behind the
packaged VM only.
