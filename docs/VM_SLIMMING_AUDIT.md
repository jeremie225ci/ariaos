# AriaOS VM Slimming Audit

This document records which parts of the current AriaOS VM are actually needed
by the public product, which parts are optional legacy leftovers, and which
parts can be removed without breaking the core AriaOS flow.

Audit date:

- April 14, 2026

## Short Verdict

The VM is **not** large because of the Linux kernel.

Observed on the running VM:

- kernel image:
  `/boot/vmlinuz-6.12.63+deb13-amd64` = about `12 MB`
- initramfs:
  `/boot/initrd.img-6.12.63+deb13-amd64` = about `66 MB`
- full `/boot`:
  about `90 MB`

The real size drivers are user-space components:

- `/usr/local/lib/ollama` = `4.3 GB`
- `/var/lib/flatpak` = `1.7 GB`
- `/var/lib/snapd` = `1.1 GB`
- `/opt/google` = `387 MB`
- `/home/tu/Downloads` = `288 MB`
- `/home/tu/.cache/google-chrome` = `166 MB`
- `/home/tu/.config/google-chrome` = `254 MB`

So the honest answer is:

- the kernel is present because the VM is Linux
- the kernel is **not** why the VM export is heavy
- the main weight comes from optional runtime stacks and leftover user data

## What AriaOS Actually Needs

The public AriaOS product currently depends on these categories:

- a Debian/Linux VM
- the local Aria runtime in `/opt/aria-client`
- GTK shell and desktop launchers
- `google-chrome` or another supported Chromium-family browser
- `xdotool` as fallback input path
- `/dev/uinput` kernel interface when available
- audio stack for voice mode
- `ffmpeg` for video/media actions exposed by the runtime

Public code references confirmed during this audit:

- browser launch paths prefer:
  `google-chrome`, then `chromium`, then `chromium-browser`
- browser launcher:
  `vm/guest/desktop/browser.desktop`
- browser wrapper:
  `aria-browser`
- xdotool fallback:
  `aria/kernel_input.py`
  and `backend/agentic_runtime.py`
- voice backend:
  `backend/voice_server.py`
- ffmpeg actions:
  `backend/agentic_runtime.py`

## Safe To Remove For The Public Release

These items are not part of the core public AriaOS product path and can be
removed from the release VM without breaking the main AriaOS flow.

## 1. Ollama

Observed state:

- binary:
  `/usr/local/bin/ollama`
- libraries:
  `/usr/local/lib/ollama`
- service:
  `ollama.service`
- service state during audit:
  `active`
- package manager ownership:
  no Debian package ownership was found for the binary

Why it is safe to remove:

- no public AriaOS code path references `ollama`
- the current product direction uses the user's OpenAI API key
- no local-model flow is part of the documented public release path

Space impact:

- `/usr/local/lib/ollama` = `4.3 GB`
- `/usr/local/bin/ollama` = `39 MB`

Release guidance:

- remove the systemd service
- remove the manual binary
- remove the Ollama libraries
- remove the `ollama` user/group only if they are not used elsewhere

Verdict:

- safe to remove

## 2. Flatpak And Flatpak Apps

Observed state:

- `flatpak` package installed
- `/var/lib/flatpak` = `1.7 GB`
- installed Flatpak app:
  `org.nickvision.money`
- installed Flatpak runtimes:
  GNOME platform, Mesa GL runtime, `openh264`

Why it is safe to remove:

- no public AriaOS code path depends on Flatpak
- the installed Flatpak app is not part of the public AriaOS experience
- the release VM does not need a secondary app distribution system for AriaOS

Verdict:

- safe to remove for the AriaOS release VM

## 3. Snapd And Snap Apps

Observed state:

- `snapd` package installed
- `/var/lib/snapd` = `1.1 GB`
- installed snap app:
  `discord`
- installed base snaps:
  `bare`, `core22`, `gnome-42-2204`, `gtk-common-themes`
- services enabled and active:
  `snapd`

Why it is safe to remove for the core product:

- no public AriaOS startup path depends on Snap
- there is no public VM launcher for Discord
- the public product does not require a snap runtime

Important nuance:

- the codebase does contain optional Discord automation helpers
- removing Discord does not break core AriaOS
- removing Discord does remove that optional app target from the VM image

Verdict:

- safe to remove for the public AriaOS release image

## 4. User Downloads And Temporary User Data

Observed state:

- `/home/tu/Downloads` = `288 MB`
- visible leftover files include `Pomatez` artifacts

Why it is safe to remove:

- downloaded user files are not part of the AriaOS product
- shipping stray downloads inside the public VM only wastes space and looks
  unprofessional

Verdict:

- safe to remove

## 5. Browser Caches And Profile Cache Bloat

Observed state:

- `/home/tu/.cache/google-chrome` = `166 MB`
- `/home/tu/.config/google-chrome` = `254 MB`

Why it is safe to clean:

- cache data is not required for browser functionality
- a clean release image should not ship stale browser cache artifacts

Important nuance:

- keep the Chrome package itself if AriaOS still expects Chrome as the main
  browser in the VM
- clean caches, not the browser dependency

Verdict:

- safe to clean

## 6. APT Caches

Observed state:

- `/var/cache/apt` = `92 MB`

Why it is safe to remove:

- package caches are not required to run AriaOS
- they can always be rebuilt later by apt if needed

Verdict:

- safe to clean

## Keep For Now

These items should stay in the public release VM unless you intentionally
change the product contract.

## 1. Google Chrome

Why keep it:

- public code explicitly tries `google-chrome` first
- browser helpers and launchers are already aligned with Chrome
- removing it would change the user-facing browser story and would need either:
  - a Chromium package replacement
  - launcher changes
  - runtime verification

Observed size:

- `/opt/google` = `387 MB`

Verdict:

- keep for now

## 2. xdotool

Why keep it:

- it is the fallback input backend when `/dev/uinput` is missing or ineffective
- several helpers still call it directly

Verdict:

- keep

## 3. ffmpeg

Why keep it:

- the runtime exposes `ffmpeg_run`
- local media extraction and video helpers depend on it

Verdict:

- keep

## 4. Audio Stack

Why keep it:

- voice mode depends on local audio capture/playback
- PulseAudio/PipeWire libraries are part of that path

Verdict:

- keep

## 5. VirtualBox Guest Additions

Why keep it:

- they improve VM usability and desktop integration

Verdict:

- keep

## Estimated Safe Savings

Without changing the public AriaOS feature set, the biggest safe wins are:

- remove Ollama:
  about `4.3 GB` plus service/binary overhead
- remove Flatpak stack:
  about `1.7 GB`
- remove Snap stack and Discord snap:
  about `1.1 GB`
- remove stray downloads:
  about `288 MB`
- clean Chrome caches and profile cache bloat:
  about `400 MB`
- clean APT caches:
  about `92 MB`

Rough total reclaimable space inside the guest:

- around `7.8 GB` before filesystem compaction/export effects

The exported `.ova` shrink will depend on:

- whether zero-fill/compaction is run before export
- the disk format
- compression efficiency during OVA creation

But these removals are large enough that the release artifact should shrink
materially after a proper compact-and-export pass.

## Recommended Release Policy

For the public AriaOS release VM:

- keep only what is required for AriaOS itself
- do not ship alternate app ecosystems unless AriaOS actually depends on them
- do not ship local-model stacks unless local models are part of the product
- do not ship leftover user downloads
- clean browser caches before export

## Practical Release Target

The clean release VM should aim to contain:

- Debian base system
- AriaOS runtime
- browser needed by AriaOS
- audio stack needed by voice mode
- input tools needed by AriaOS fallback paths
- nothing else that is not part of the actual product story
