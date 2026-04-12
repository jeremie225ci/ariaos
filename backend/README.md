## Local Backend

`backend/server.py` is the embedded websocket runtime used by Terminal Aria.

It runs fully locally and supports:

- session reset
- automatic chat title generation
- local OpenAI key usage from `~/.config/ariaos/local_agent_secrets.json`
- streamed text responses over the shell websocket protocol
- stop requests

This replaces the old split runtime path with a single local runtime inside the VM.
