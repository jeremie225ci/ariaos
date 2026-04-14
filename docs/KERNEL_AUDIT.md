# AriaOS Kernel Audit

This document records what the AriaOS project actually uses as its kernel layer,
what the public repository exposes today, and what the older legacy workspace
still contains.

Audit date:

- April 14, 2026

## Short Verdict

AriaOS **does run on a real kernel**, but the current public project does **not**
ship a custom Aria kernel implementation.

What exists today:

- the VM boots a standard Debian Linux kernel
- AriaOS runs in user space on top of that kernel
- Aria injects mouse and keyboard events through `/dev/uinput`
- boot branding is applied through Plymouth, GRUB, and LightDM

What does **not** exist in the public project today:

- a custom Aria kernel in C
- a custom Aria kernel in Rust
- a maintained Aria-specific kernel patchset that is clearly wired into the VM

## Evidence Snapshot

The audit found these concrete facts:

- running VM kernel:
  `6.12.63+deb13-amd64`
- visible boot image in the VM:
  `/boot/vmlinuz-6.12.63+deb13-amd64`
- VM GRUB branding:
  `GRUB_DISTRIBUTOR="AriaOS"`
- VM default boot flags:
  `GRUB_CMDLINE_LINUX_DEFAULT="quiet splash noresume loglevel=3 rd.systemd.show_status=false vt.global_cursor_default=0"`
- kernel support for uinput:
  `CONFIG_INPUT_UINPUT=m`
- older kernel checkout remote:
  `git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
- older kernel checkout divergence from origin:
  `0 0`

That last point matters: the older visible kernel checkout is not even ahead of
its upstream remote during this audit.

## Public Repository Reality

In the public repository:

- `aria/kernel_input.py`
  is the real kernel-facing module used by AriaOS today

That module does **not** change kernel source code. It talks to the running
Linux kernel through the standard `/dev/uinput` interface.

So the public project currently exposes:

- user-space code that talks to the kernel

It does **not** expose:

- a custom kernel source tree used as part of the product build

## Audit Of The Older Legacy `kernel/` Tree

The older legacy workspace does contain a visible kernel repository:

- `Ariaos/kernel`

But the audit shows that this tree is an upstream Linux repository, not a clear
Aria-specific fork.

Observed facts:

- it is a nested Git repository
- its remote is:
  `git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
- its branch tracks:
  `master...origin/master`
- its working tree is clean
- `git describe --tags --always` returns:
  `v6.16-rc5-53-g8c2e52ebbe88`
- `origin/master...master` reports:
  `0 0`
- no Aria-specific strings were found in the kernel Git history or tracked
  kernel source paths during this audit

This means:

- the tree exists
- but it currently looks like a plain upstream Linux checkout
- not like a clearly maintained Aria kernel fork

## Audit Of The Kernel Actually Running In The VM

The running VM clone was checked directly over SSH on April 14, 2026.

Observed kernel:

- `uname -r`:
  `6.12.63+deb13-amd64`
- boot image present in the VM:
  `/boot/vmlinuz-6.12.63+deb13-amd64`

This matters because it does **not** match the source tree described above:

- running VM kernel:
  Debian `6.12.63+deb13-amd64`
- nested source tree:
  upstream Linux `v6.16-rc5-53-g8c2e52ebbe88`

So the currently running AriaOS VM is **not** booting directly from that
visible nested kernel checkout.

## Where The Aria Boot Look Actually Comes From

The Aria branding visible at boot does not prove a custom kernel.

The audit found that the branding is applied through user-space boot and login
components:

- legacy workspace:
  `vm_patches/AriaApp/apply_boot_branding.sh`
- legacy workspace:
  `vm_patches/AriaApp/ariaos.plymouth`
- legacy workspace:
  `vm_patches/AriaApp/ariaos.script`
- public repo:
  `vm/guest/greeter/lightdm-gtk-greeter.conf`

That boot-branding path:

- installs an Aria Plymouth theme
- sets the default Plymouth theme
- updates initramfs
- changes GRUB branding and boot flags
- changes the LightDM greeter background and logo

So the boot logo and boot splash are currently explained by:

- Plymouth
- GRUB configuration
- LightDM branding

not by a demonstrated kernel source patch.

## Public Repo Coverage vs Legacy Workspace

The public repository does expose part of the VM presentation layer:

- `vm/guest/greeter/lightdm-gtk-greeter.conf`
- `vm/guest/greeter/aria_greeter.png`
- `vm/guest/greeter/aria_shell_wallpaper.png`
- `vm/guest/greeter/logo5.png`

But during this audit, the full Plymouth and GRUB branding installer path was
only visible in the older legacy workspace, not as a clearly mirrored public
VM build path.

That means the current public repo is honest about the runtime brain and the
kernel-facing input layer, but it is still incomplete if the goal is to expose
the entire boot-branding pipeline end to end.

There is another reason to avoid treating the older legacy workspace as the
public source of truth: some legacy scripts there still contain outdated secret
handling and environment-specific paths that should not be carried into the
clean public release flow.

## How Aria Talks To The Running Kernel

AriaOS talks to the running kernel through:

- `/dev/uinput`

The public module responsible for that is:

- `aria/kernel_input.py`

The flow is:

1. open `/dev/uinput`
2. register supported event types with `ioctl`
3. create a virtual input device
4. write Linux input events
5. let the running kernel inject those events into the VM desktop session

This is kernel-facing behavior, but it is still **user-space code**.

It means AriaOS:

- uses the running kernel
- asks the kernel to create a virtual input device
- injects events through a standard Linux interface

It does **not** imply:

- a patched kernel
- a rebuilt kernel
- a custom Aria scheduler or graphics stack inside kernel space

The VM observation also showed:

- support is compiled as `CONFIG_INPUT_UINPUT=m`
- `uinput` was not loaded in `lsmod` at the exact audit instant

That is consistent with a standard Linux VM where the capability exists in the
kernel configuration and can be activated when the runtime needs it.

## What `kernel_input.py` Changes In Practice

`aria/kernel_input.py` changes runtime behavior, not kernel source.

It can:

- create a temporary virtual input device while Aria is running
- send mouse moves, clicks, drags, scrolls, and keyboard events
- fall back to `xdotool` if `/dev/uinput` is unavailable or ineffective

It does not:

- edit kernel C source
- compile kernel modules
- replace the Debian kernel image
- inject persistent kernel patches into the VM disk

## What This Means For The Project

Today, the honest architecture statement is:

- AriaOS is an agent operating environment built on top of a standard Linux VM
- the kernel is the Debian/Linux kernel already installed in that VM
- the public repo documents the agent/runtime layer and the kernel-facing input layer
- the public repo does not currently document a custom Aria kernel fork because
  there is no clear evidence of one being used by the running VM
- the public repo only partially exposes the boot-branding layer observed in
  the older legacy workspace

## If You Want The Kernel Story To Be Public And Clean

There are two sane options:

1. document the real kernel dependency clearly

- exact VM kernel version
- boot-branding path
- `/dev/uinput` path
- permissions/modules needed for input injection
- which boot assets are already public and which still need to be mirrored

2. publish a real kernel customization layer only if it actually exists

- a patch series
- a forked kernel repo
- a defconfig
- a build script that produces the kernel installed in the VM

What is **not** useful:

- shipping a huge upstream Linux checkout inside the public AriaOS repo without
  proving it is the kernel actually used by the VM

## Related Docs

- `docs/KERNEL_AND_SURFACE_MAP.md`
- `docs/LOCAL_FIRST_ARCHITECTURE.md`
- `docs/VM_CODE_LAYOUT.md`
