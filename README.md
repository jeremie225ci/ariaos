# AriaOS

AriaOS is an AI operating system for agents.

The project is built around a simple idea: instead of running an agent as a tab inside a generic app, give it its own operating environment. AriaOS runs inside a dedicated virtual machine, keeps the host outside the working perimeter, and exposes a workspace designed for agent-driven execution, memory, tools, and direct operator control.

## Direction

This repository is the new local-first AriaOS line.

The goal is to make AriaOS:

- open source
- easier to understand
- easier to install and package
- independent from the old hosted account stack
- usable with a user-provided OpenAI API key stored locally on the VM

The current migration direction is:

- no mandatory account creation in onboarding
- no required control plane for the main VM experience
- bring-your-own OpenAI key at first launch
- local runtime and local workspace as the default product path

## What AriaOS Is

AriaOS is not a generic Linux distribution. It is a focused agent workspace packaged as a VM.

Core ideas:

- dedicated VM workspace for the agent
- desktop shell and terminal entrypoint designed around the agent workflow
- local long-term memory file and task history
- browser, files, and desktop tools available inside the same VM
- export and release tooling for shipping the VM image

## Current Product Shape

In the new local-first flow, the user:

1. boots the VM
2. goes through onboarding
3. enters an OpenAI API key once
4. stores that key locally on the VM
5. launches Terminal Aria and works from inside the machine

This removes the old account-first experience and makes the product easier to adopt, inspect, and redistribute.

## Repository Layout

- `app/aria-shell-gtk/`
  GTK shell client, onboarding flow, home screen, and terminal experience.
- `vm/guest/`
  Guest launchers, desktop entries, first-boot assets, and VM-facing scripts.
- `vm/release/`
  Release and export helpers for producing VM artifacts.
- `docs/`
  Project documentation for the new public AriaOS line.
- `assets/`
  Shared visuals and repository assets.

## Migration Status

This repository is still under active cleanup and migration from the older private AriaOS codebase.

Already in progress:

- split into a dedicated public-facing repository
- local-only onboarding path centered on the OpenAI key
- VM launcher gating based on local key setup instead of remote account state

Still being cleaned:

- old hosted/control-plane remnants in unused modules
- naming and docs across the tree
- packaging and release documentation
- final public-ready structure for the VM distribution

## Vision

The long-term goal is to make AriaOS a practical operating environment for autonomous and semi-autonomous agents:

- inspectable
- reproducible
- shareable
- usable by builders without a private backend dependency

This repository is the base for that version of AriaOS.
