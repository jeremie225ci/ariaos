# AriaOS - AI Operating System for Agents

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.x-blue?style=for-the-badge&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/VM-Debian%2012%20%2F%20XFCE-red?style=for-the-badge&logo=debian" alt="Debian VM">
  <img src="https://img.shields.io/badge/AI-OpenAI%20GPT--5.4-green?style=for-the-badge&logo=openai" alt="OpenAI">
</p>

An agent-focused operating system that runs inside a dedicated virtual machine, with Terminal Aria, local memory, local files,voice mode ,futur task management and local OpenAI key setup.
The project was initially created to satisfy my curiosity. I’ve always wondered what would happen if I givee an AI agent a control of an operating system , gave it full privileges—including sudo—and let it interact with the computer’s user interface (clicking, scrolling, etc.). Well, to carry out this project, the only feasible and secure way was to place the agent in a virtual machine on a dedicated Linux system. So I went ahead and created linux distro based  an Debian fork dedicated to the agent, explicitly choosing GPT 5.4 because it’s relatively cost-effective and has the best computer usage scores in benchmark tests. The results are quite impressive and mind-blowing: the agent is capable of building entire complex apps in a matter of moments, can use my compromised apps, and operates on certain computers with highly sophisticated reasoning.

## 🚀 Features

- 🤖 **Agent-focused OS** - A dedicated environment for running and operating an agent inside a VM
- 💻 **Terminal Aria** - A work surface centered on agent-driven execution
- 🔐 **OpenAI BYOK** - Users provide their own OpenAI API key on first launch
- 🗂️ **Local Workspace** - Files, history, and memory stay inside the VM
- 🧠 **Visible Local Brain** - The repo now exposes the local VM brain, its prompts, and its agentic loop
- 🖥️ **Integrated Desktop** - Browser, files, and shell run inside the same machine

## ⚡ Quick Start

```bash
# Clone the repository
git clone git@github.com:jeremie225ci/ariaos.git
cd ariaos

# Run the GTK shell locally
cd app/aria-shell-gtk
PYTHONPATH=src python3 -m aria_shell_gtk
```

For the actual product experience, the intended path remains the preconfigured AriaOS VM. This public repository contains the shell codebase, VM guest files, and VM release tooling.

## 📖 Usage

### First Run

1. Boot the AriaOS VM
2. Open AriaOS / Aria Home
3. Complete onboarding
4. Enter an OpenAI API key
5. Launch Terminal Aria

### Main Flow

- **Aria Home** - Main entry point
- **Terminal Aria** - Task execution interface
- **OpenAI Key** - Local API key and model setup
- **Files** - Access to the local workspace
- **Browser** - Navigation inside the VM

### Current Direction

The public AriaOS flow is now **local-only**:

- no mandatory account creation
- no mandatory remote server dependency for first use
- OpenAI API key stored locally inside the VM
- embedded local runtime as the only supported execution path

## 🧠 Brain Runtime

If you want to understand what the public AriaOS brain can actually do, read:

- `docs/BRAIN_ACTION_SURFACE.md` - planner contract, `computer` loop, desktop helpers, file/programming actions, and runtime guard rails
- `docs/LOCAL_FIRST_ARCHITECTURE.md` - local runtime topology inside the VM
- `docs/VM_CODE_LAYOUT.md` - canonical VM code location, runtime state paths, caches, staging copies, and backups

## 🔧 Configuration

Main local files used by AriaOS:

- `~/.config/ariaos/user_secrets.json` - local OpenAI key and secrets
- `~/.config/ariaos/runtime_config.json` - runtime configuration
- `~/.local/state/ariaos/onboarding_state.json` - onboarding state
- `~/.ariaos/data/` - local history and data

## 🏗️ Project Structure

```text
ariaos/
├── README.md
├── backend/                      # Local backend runtime, visible brain, prompts, and voice server
├── aria/                         # Local input backend package used by the brain
├── app/
│   └── aria-shell-gtk/          # GTK shell, onboarding, home, terminal
├── vm/
│   ├── guest/                   # Launchers, desktop files, firstboot, greeter
│   └── release/                 # VM export and release tooling
├── docs/
│   ├── INSTALL_VM.md            # VM setup flow
│   ├── LOCAL_FIRST_ARCHITECTURE.md
│   ├── BRAIN_ACTION_SURFACE.md  # Planner actions, computer-use loop, and local helper/tool surface
│   └── VM_CODE_LAYOUT.md        # Canonical VM source location vs state/cache/staging directories
└── assets/                      # Shared repository assets
```

## 🛡️ Security

AriaOS isolates the agent experience inside a dedicated VM:

- 🧱 **Separate perimeter** - The agent works inside the VM, not directly on the host
- 🔐 **Local key storage** - The OpenAI key stays stored locally
- 🗃️ **Visible memory** - Preferences and useful summaries remain inspectable
- 🧭 **Simplified flow** - Fewer remote dependencies in the main product path

## 📋 Requirements

- Python 3
- Linux / Debian-based VM
- GTK4 + libadwaita for the GTK shell
- OpenAI API key

Common packages:

- `python3-gi`
- `gir1.2-gtk-4.0`
- `gir1.2-adw-1`

## 📄 License

Public license not finalized yet.
