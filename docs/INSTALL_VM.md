# AriaOS VM Setup

This document describes the intended first-run flow for the new local-first AriaOS repository.

## Product assumptions

- AriaOS is distributed as a virtual machine image
- the main experience runs inside the VM
- the user brings an OpenAI API key
- the key is stored locally on the VM

## First run

1. boot the AriaOS VM
2. let the desktop session start normally
3. open AriaOS / Aria Home
4. finish onboarding
5. enter an OpenAI API key
6. let the local runtime restart
7. open Terminal Aria

## Current launcher behavior

The VM guest launcher checks:

- onboarding state file
- local secrets file containing the OpenAI API key

If onboarding is not complete and no key is present, the shell starts in `intro` mode.

Relevant file:

- `vm/guest/bin/run_aria_shell_gtk.sh`

## Runtime prerequisites inside the VM

- Python 3
- GTK4 bindings for Python
- libadwaita bindings
- a working local Aria runtime/backend

## Current status

The repository is still being cleaned, so packaging and public install instructions are not final yet. This file tracks the intended local-first flow rather than a fully finalized release process.
