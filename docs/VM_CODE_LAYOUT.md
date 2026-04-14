# AriaOS VM Code Layout

This document explains where the real code lives inside the VM, which paths are only runtime state, and which directories are staging or backup copies rather than the canonical source tree.

## Canonical Code Location Inside The VM

The installed AriaOS code that the VM actually runs lives here:

- `/opt/aria-client/share/AriaApp`

This directory is the deployed copy of the public repository and should be treated as the canonical runtime source tree inside the VM.

Main code areas inside that tree:

- `/opt/aria-client/share/AriaApp/backend`
  The local brain, prompts, websocket backend, and voice backend.
- `/opt/aria-client/share/AriaApp/app/aria-shell-gtk`
  The GTK shell, onboarding flow, Terminal Aria UI, and local client bridge.
- `/opt/aria-client/share/AriaApp/aria`
  Local low-level helper code such as the input backend package.
- `/opt/aria-client/share/AriaApp/docs`
  Public documentation shipped with the installed VM code.
- `/opt/aria-client/share/AriaApp/vm`
  VM guest integration and release tooling files.

## Canonical Source Of Truth During Development

When developing on the host machine, the main editable repository is:

- your local AriaOS clone on the host
  Example: `/path/to/ariaos`

The deployment model is:

1. edit the host repository
2. commit and push from the host repository
3. sync that repository into `/opt/aria-client/share/AriaApp` inside the VM

The goal is for the host repository and the installed VM tree to match exactly.

## Paths That Are Not Source Code

These paths are important, but they are runtime state, not the app source tree:

- `~/.config/ariaos`
  Local secrets and runtime configuration.
- `~/.ariaos/data`
  History databases, memory, and user/task data.
- `~/.local/state/ariaos`
  Runtime logs, PID files, and transient state such as onboarding markers.

Do not treat those directories as the main place to edit AriaOS logic.

## Generated And Non-Canonical Copies

These paths may exist in the VM, but they are not the canonical installed source tree:

- `/home/tu/ariaos-community-deploy`
  Deployment staging copy used while syncing the host repository into the VM.
- `/home/tu/ariaos-community-test`
  Scratch/test copy created during migration and verification work.
- `/opt/aria-client.backup-*`
  Backup of an older installed client layout kept for rollback/reference.
- `__pycache__/`
  Generated Python bytecode cache directories. These are not hand-written source files.

If you are trying to understand or edit the real VM code, start with `/opt/aria-client/share/AriaApp`, not these paths.

## What Is Still Generated Automatically

The normal public launch paths now export `PYTHONDONTWRITEBYTECODE=1`, so the
VM should not keep regenerating `__pycache__` directories during normal use.

If `__pycache__` appears again, it usually means Python was run manually outside
the standard Aria launchers. Those files are still:

- generated locally
- safe to delete
- not the human-readable source of truth

## Legacy Naming Audit

The installed public tree should expose readable names such as:

- `backend/agentic_runtime.py`
- `backend/brain.py`
- `backend/loopagentic.py`
- `backend/server.py`
- `backend/voice_server.py`

Old packaged names like `kernel.pyc`, `engine.pyc`, or `screen.pyc` should not be treated as the public runtime layout anymore.

One remaining readable module name that still contains the word `kernel` is:

- `aria/kernel_input.py`

That file is not obfuscation. It is the local input backend module used by the runtime.

## If You Want To Modify AriaOS

Change these places first:

- planner rules and visible action contract:
  `/opt/aria-client/share/AriaApp/backend/brain_config.py`
- planner/runtime behavior:
  `/opt/aria-client/share/AriaApp/backend/agentic_runtime.py`
- lightweight backend helpers:
  `/opt/aria-client/share/AriaApp/backend/brain.py`
- GTK shell and Terminal Aria UI:
  `/opt/aria-client/share/AriaApp/app/aria-shell-gtk`

Avoid editing:

- `__pycache__`
- PID files
- logs
- generated state under `~/.local/state/ariaos`

## Practical Rule

If a user asks "where is the AriaOS VM code?", the short answer should be:

- runtime source in the VM: `/opt/aria-client/share/AriaApp`
- editable development repository on the host: your local AriaOS clone path

Everything else should be treated as state, cache, staging, or backup unless explicitly noted otherwise.
