---
name: mutter-egpu-port
description: Port the USB4 GPU Manager Mutter eGPU patch to a Linux distribution's Mutter release, build and validate matching desktop packages, set a tested build as the system default with rollback, or handle the project's newer-Mutter notification.
---

# Port the Mutter eGPU patch

Find the project root or the installed kit in `/usr/share/usb4-gpu-manager`. Read [the porting runbook](../../docs/mutter-porting.md) in the source tree, or `/usr/share/usb4-gpu-manager/mutter-porting.md` from an installed package. Use the shipped unified patch and its source fingerprints as the reference. The verified seed is Ubuntu 26.04 amd64 Mutter `50.1-0ubuntu2.5`.

1. Identify the target distro, its host update mechanism, Mutter source version, GNOME Shell package, ABI and test GPU. For an image-managed host, follow its image workflow for activation and rollback. Get the target distro's authenticated source package or SRPM and start from its prepared source after the distro patches. Record the official version separately from the local version.
2. Run `source_patch.py verify`. On a mismatch, review the new source and port the behavior; avoid offset/fuzz or merely replacing the fingerprints. Check that the selected removable NVIDIA PCI device stays KMS-only with CPU-copy, AMD remains the renderer, the cursor overlay is per view, the target DRM lease is hidden, and only idle secondary resources are released. Re-generate the unified patch and fingerprints against the new prepared baseline. Read the runbook for source/package format details.
3. Compile in an isolated, matching distro environment and preserve its package names, ABI, dependencies, and files. Validate source application, native/typelib library identity, a complete isolated Shell startup and real outputs. Check Xwayland descriptors and same-session output disable, removal and reconnect on a test machine before claiming hardware support.
4. Treat build, test, and system activation as separate steps. Prepare the official rollback packages and TTY commands before changing the system default. Use the distro's package manager and enable the policy only for the verified external PCI device as shown in the runbook. Report exactly which stages ran and which hardware behavior remains untested.
5. Point the update watcher at the **official base version**, even when the installed package has a local suffix. A newer repository candidate prompts a fresh port and validation; preserve normal distro security updates and keep the old patch/build available for review.
