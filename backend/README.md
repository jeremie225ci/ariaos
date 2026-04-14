## Local Backend

This directory contains the readable local backend used by AriaOS inside the VM.

If you want the exact planner/action contract, read `docs/BRAIN_ACTION_SURFACE.md`
from the repository root. That document explains:

- how the planner emits JSON actions
- how the `computer` subloop differs from specialized local helpers
- which file, desktop, system, and programming actions are really implemented
- which browser/Terminal guard rails are enforced by the runtime

If you want to know where this backend lives inside the VM and which paths are
real source code vs runtime state or deployment copies, read
`docs/VM_CODE_LAYOUT.md`.

- `backend/prompts.py`
  Visible prompt definitions for the local backend, including the direct-answer
  prompt and the full VM-style agentic system prompt.

- `backend/brain_config.py`
  Local-first runtime configuration used by the imported VM brain. It keeps the
  community build on a local execution path and drops the old remote parser
  assumptions.

- `backend/brain.py`
  Shared OpenAI runtime helpers. It loads the local key from `~/.config/ariaos/user_secrets.json`
  first and falls back to the legacy local secrets file when needed.

- `backend/agentic_runtime.py`
  Visible copy of the VM agentic brain. It contains the multi-step planner,
  computer-use loop, verification logic, scheduling actions, and local tool
  execution behavior.

- `backend/loopagentic.py`
  Thin bridge between the websocket server and the imported VM agentic runtime.
  It preserves the shell-facing protocol while delegating execution to the
  visible local brain.

- `backend/server.py`
  Embedded websocket runtime used by Terminal Aria on `127.0.0.1:8080`.
  It handles session reset, title generation, streamed responses, and stop
  requests for the local GTK shell.

- `backend/voice_server.py`
  Local voice websocket runtime on `127.0.0.1:8081`. It accepts microphone
  audio, performs transcription, emits start/stop commands back to the shell,
  and streams local TTS audio to the GTK client.

- `backend/profiles/`
  App-specific behavior profiles used by the agentic brain. The community build
  currently ships the Gmail profile used by the VM brain.

This replaces the old split client/server runtime with a readable local runtime
inside the VM and inside the public repository. The repository now exposes the
actual local brain path instead of only a minimal chat wrapper.
