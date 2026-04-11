# AriaOS Community

AriaOS Community is a local-first split of AriaOS intended for a public repository and a simpler VM distribution.

Current direction:

- remove the hosted account flow from onboarding
- keep the user inside the VM experience
- store the OpenAI API key locally on the VM
- simplify release tooling and guest files for a community build

Repository layout:

- `app/aria-shell-gtk/`: GTK shell client and community onboarding flow
- `vm/guest/`: guest launchers, desktop entries, first-boot assets, and VM-facing scripts
- `vm/release/`: release/export helpers carried over for the new distribution

This repository is currently being cleaned and split out from the private/mainline AriaOS tree.
