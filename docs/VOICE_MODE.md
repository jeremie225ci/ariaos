# AriaOS Voice Mode

This document describes the local voice path used by the public AriaOS build.

## Product Shape

Voice mode is local to the VM:

- the GTK shell captures microphone audio
- the local voice websocket handles transcription and TTS
- the shell keeps task execution in the same local runtime

There is no remote voice gateway in the public local-first build.

## Main Components

- `app/aria-shell-gtk/src/aria_shell_gtk/services/voice_client.py`
  GTK-side voice client.

- `app/aria-shell-gtk/src/aria_shell_gtk/views/voice_overlay.py`
  Voice overlay UI and state display.

- `backend/voice_server.py`
  Local voice websocket runtime on `127.0.0.1:8081`.

- `backend/brain.py`
  Shared OpenAI speech-to-text and text-to-speech helpers.

## Runtime Flow

1. The GTK shell ensures the local voice backend is reachable.
2. `VoiceClient` connects to `ws://127.0.0.1:8081/ws/voice`.
3. The client sends a `hello` frame with:
   - `voice_name`
   - `transcription_language`
   - `response_language`
4. The voice backend returns `ready` and switches to listening state.
5. The client streams PCM audio chunks.
6. The voice backend detects an utterance boundary using a simple RMS gate.
7. The utterance is transcribed locally through OpenAI.
8. The recognized command is translated into a shell-level command.
9. When a task result appears in the terminal snapshot, the voice backend can
   speak the result once through local TTS.

## Command Surface

The public local build keeps voice commands intentionally narrow.

Today the voice layer recognizes two command families:

- `start_task`
  Start a new Terminal Aria task when the shell is idle.

- `stop_task`
  Stop the current running task.

If Aria is already busy and the user asks for a new task, the voice layer does
not open a second task. It asks the user to stop the current one first.

That logic lives in:

- `backend/voice_server.py`
  `_command_from_transcript`

## Speech Detection

The public build uses a lightweight local utterance detector.

Implementation:

- `backend/voice_server.py`
  `_ingest_audio_chunk`

It uses:

- RMS thresholding
- a short pre-roll buffer
- silence counting to close the utterance
- max utterance length guards

This keeps the repository local and readable without bringing back the older
remote VAD stack.

## Transcription And TTS

Speech services are handled through the same local OpenAI runtime used by the
rest of AriaOS.

Implementation:

- `backend/brain.py`
  `transcribe_pcm`

- `backend/brain.py`
  `synthesize_speech`

Current defaults:

- transcription:
  - preferred: `gpt-4o-mini-transcribe`
  - fallback: `whisper-1`

- text-to-speech:
  - preferred: `gpt-4o-mini-tts`
  - fallback: `tts-1`

The voice backend asks for raw PCM TTS output so the GTK side can stream it
without another decode step.

## State And Configuration

Local voice configuration is stored in:

- `~/.config/ariaos/runtime_config.json`

Relevant fields:

- `voice_name`
- `voice_language`

The GTK shell exposes those values and the backend reads them when it restarts.

## Important Limitation

Voice mode is a control surface around Terminal Aria. It is not a separate
agent brain.

That means:

- speech starts or stops tasks
- the text/agentic runtime still performs the real work
- spoken result playback is derived from terminal task snapshots
