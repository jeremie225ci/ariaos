# AriaOS Kernel And Surface Map

This document explains what `kernel` means in the public AriaOS repository,
which old packaged names it replaced, and which visible UI surfaces now exist
in readable source form.

For the deeper audit of what kernel is actually used by the VM and whether the
older `Ariaos/kernel` tree is a real Aria-specific fork, read:

- `docs/KERNEL_AUDIT.md`

## What `kernel` Means In The Public Repo

There are two different historical meanings of `kernel` in AriaOS:

1. the real low-level Linux input layer
2. an old packaged/obfuscated alias used in private VM builds

In the public repository, the real kernel-level module is:

- `aria/kernel_input.py`

That file is responsible for:

- creating a virtual input device through `/dev/uinput`
- sending keyboard and mouse events at the Linux input layer
- falling back to `xdotool` when `/dev/uinput` is unavailable or ineffective

It is used by:

- `backend/agentic_runtime.py`
  `ActionExecutor`

So if someone asks "where is the kernel code now?", the answer is:

- real kernel-level input path: `aria/kernel_input.py`

## What The Old Packaged `kernel.pyc` Was

Older private packaged builds renamed readable modules before shipping the VM.
In that private layout, the name `kernel.pyc` did **not** refer to low-level
Linux kernel input. It was an obfuscated packaged alias for higher-level shell
service code.

The important distinction is:

- `aria/kernel_input.py` = real kernel-level input backend
- old `services/kernel.pyc` = packaged alias for shell/runtime service code

## Old Private Names vs Public Readable Names

The public repository keeps the readable names so contributors can understand
the system without reverse-engineering the VM package.

Important name mappings from the older packaged layout:

- public `app/aria-shell-gtk/src/aria_shell_gtk/services/core.py`
  old packaged alias: `services/kernel.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/services/loop.py`
  old packaged alias: `services/engine.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/views/console.py`
  old packaged alias: `views/screen.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/views/shell.py`
  old packaged alias: `views/hub.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/views/panels.py`
  old packaged alias: `views/cards.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/views/intro.py`
  old packaged alias: `views/splash.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/services/local_secrets.py`
  old packaged alias: `services/vault.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/services/channel.py`
  old packaged alias: `services/wire.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/services/bootstrap.py`
  old packaged alias: `services/seed.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/services/desktop_io.py`
  old packaged alias: `services/display.py`
- public `app/aria-shell-gtk/src/aria_shell_gtk/local_link.py`
  old packaged alias: `pulse.py`

The public goal is the inverse of the old package flow:

- private VM packaging hid the readable names
- public repo exposes the readable names again

## Kernel Runtime Flow Today

The current public local-first kernel/input path is:

1. `backend/agentic_runtime.py`
   `ActionExecutor` asks for an input backend
2. `aria/kernel_input.py`
   `create_input(...)` tries to build `KernelInput`
3. `KernelInput`
   uses `/dev/uinput` for low-level pointer and keyboard events
4. `ActionExecutor`
   probes whether pointer movement actually works in the VM
5. if the probe fails, Aria switches to `XdotoolInput`

That means the public repo now documents the real behavior explicitly:

- kernel path preferred when it truly works
- xdotool fallback used when the kernel path is present but ineffective

## UI Surfaces That Exist Today

There is no standalone module named `playground` in the current public repo.

The visible operator surfaces are:

- `app/aria-shell-gtk/src/aria_shell_gtk/views/intro.py`
  onboarding and local-first key entry
- `app/aria-shell-gtk/src/aria_shell_gtk/views/shell.py`
  Aria Home / main shell surface
- `app/aria-shell-gtk/src/aria_shell_gtk/views/console.py`
  Terminal Aria and streamed task execution UI
- `app/aria-shell-gtk/src/aria_shell_gtk/views/panels.py`
  shared UI panels and dialogs
- `app/aria-shell-gtk/src/aria_shell_gtk/views/voice_overlay.py`
  voice interaction overlay

So when people say `playground` informally, in the public repo they are
usually talking about one of these readable shell surfaces rather than a single
dedicated module.

## What Was Changed For The Public Local-First Build

At the kernel/surface level, the public repository now makes these changes
visible:

- the real low-level input backend is readable in `aria/kernel_input.py`
- runtime selection between kernel input and xdotool is readable in
  `backend/agentic_runtime.py`
- the old packaged names are no longer the source of truth
- the VM runtime code is expected to live in readable source form under
  `/opt/aria-client/share/AriaApp`
- public first-boot now moves privileged execution to sudoers/NOPASSWD instead
  of storing a VM admin password in AriaOS files

## Where To Modify Things

If you want to change kernel/input behavior:

- `aria/kernel_input.py`
- `backend/agentic_runtime.py`

If you want to change the visible shell surfaces:

- `app/aria-shell-gtk/src/aria_shell_gtk/views/intro.py`
- `app/aria-shell-gtk/src/aria_shell_gtk/views/shell.py`
- `app/aria-shell-gtk/src/aria_shell_gtk/views/console.py`
- `app/aria-shell-gtk/src/aria_shell_gtk/views/panels.py`
- `app/aria-shell-gtk/src/aria_shell_gtk/views/voice_overlay.py`

If you want to understand the installed VM code layout first:

- `docs/VM_CODE_LAYOUT.md`
- `docs/LOCAL_FIRST_ARCHITECTURE.md`
