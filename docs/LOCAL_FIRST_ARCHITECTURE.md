# AriaOS Local-First Architecture

The current public AriaOS direction is local-first.

## Main principles

- the VM is the product
- onboarding happens inside the VM
- the user provides an OpenAI API key directly
- the key stays on the VM
- Terminal Aria talks to a local runtime by default

## Active path

1. GTK shell starts
2. onboarding checks whether a local OpenAI key exists
3. if missing, AriaOS asks for the key
4. the key is saved in the local secrets file
5. the local backend is restarted
6. Terminal Aria uses the local runtime

## Main state files

- onboarding state:
  `~/.local/state/ariaos/onboarding_state.json`
- local secrets:
  `~/.config/ariaos/local_agent_secrets.json`
- runtime config:
  `~/.config/ariaos/runtime_config.json`
- local history:
  `~/.ariaos/data/`

## Current cleanup direction

- keep only the pieces required by the VM product path
- reduce hosted-only modules
- keep public repository structure understandable
- document the VM release path clearly
