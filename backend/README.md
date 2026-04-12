## Local Backend

This directory contains the readable local backend used by AriaOS inside the VM.

- `backend/brain.py`
  Shared OpenAI runtime helpers. It loads the local key from `~/.config/ariaos/user_secrets.json`
  first and falls back to the legacy local secrets file when needed.

- `backend/loopagentic.py`
  Readable local orchestration loop for Terminal Aria. It rebuilds the short
  session context, calls OpenAI, and returns a structured local task result.

- `backend/server.py`
  Embedded websocket runtime used by Terminal Aria on `127.0.0.1:8080`.
  It handles session reset, title generation, streamed responses, and stop
  requests for the local GTK shell.

- `backend/voice_server.py`
  Local voice websocket runtime on `127.0.0.1:8081`. It accepts microphone
  audio, performs transcription, emits start/stop commands back to the shell,
  and streams local TTS audio to the GTK client.

This replaces the old split client/server runtime with a readable local runtime
inside the VM and inside the public repository.
