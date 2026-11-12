# Distributing and rebuilding the Mutter eGPU patch

The project ships the unified patch, original-file SHA-256 fingerprints, generator and overlays under `experimental/mutter-poc/`. Installed packages put them in `/usr/share/usb4-gpu-manager/mutter-poc/`. The patch and Mutter-derived overlays are GPL-2.0-or-later. The verified seed is Ubuntu 26.04 amd64 Mutter `50.1-0ubuntu2.5` with the distro patches applied. A different distro build, even at the same upstream version, may require a port.

The included agent skill is `skills/mutter-egpu-port/` in the source tree and `/usr/share/usb4-gpu-manager/skills/mutter-egpu-port/` when installed. Point an agent to that `SKILL.md`, or copy the whole skill folder into the agent's skill directory. The skill uses this runbook and the distributed patch kit, so keep the project checkout or installed kit available.

## Acquire and verify the target source

Use the destination distro's authenticated repositories and matching build environment. Enable source repositories where needed. Use the distro source with its patches already applied, such as the tree extracted by `apt source` or the tree produced by an SRPM's `%prep`.

```bash
kit=/usr/share/usb4-gpu-manager/mutter-poc
python3 "$kit/source_patch.py" verify /absolute/path/to/prepared-mutter
```

If fingerprints differ, keep a clean copy of the target distro's prepared tree. Review the old patch and the new KMS, GBM/EGL, renderer, cursor and DRM lease paths. Port the changes in a separate tree. The generator `make_patch.py` and its overlays use exact anchors. Adapt them in a writable copy and regenerate `gpu-release-poc.patch` and its fingerprints against the new clean tree. Verify that the old unmodified path still works when the policy is disabled. Keep distro patches and build configuration in place.

The patch limits EGL/GBM initialization and DRM leases for one removable NVIDIA PCI function and retains KMS output via AMD composition and CPU copies. Hardware cursor availability and software overlay selection must stay per view. A GTK, WebKit, Xwayland or CUDA process can independently hold GPU resources. On the verified v4 build, a card reconnected with no external output may retain a KMS/logind reference until a display is enabled and disabled once.

## Build packages

### Debian and Ubuntu

In a clean matching builder, fetch `apt source mutter=<official-version>` and install build dependencies using the distro recipe. The helper expects a fresh `3.0 (quilt)` tree with distro patches applied. It verifies the patch, appends it to the series, and appends `+usb4gpu1` to the official version. Use `--revision N` for subsequent local builds of the same official version.

```bash
python3 "$kit/source_patch.py" prepare-deb /absolute/path/to/prepared-mutter
cd /absolute/path/to/prepared-mutter
dpkg-buildpackage -b -us -uc
```

The native recipe builds the distro's complete binary package set. Review dependencies of the installed Mutter libraries, typelib, common data and Shell. Prepare a single transaction containing the matching rebuilt packages that are installed or required. Use `apt install --simulate ./<package>.deb ...` to check it before executing it.

### Fedora and compatible DNF-based distributions

These steps apply to conventional DNF-managed systems. On an image-based system such as Fedora Atomic or bootc, use the distro's image build and rollback process for the host compositor. A DNF transaction inside a toolbox only updates the toolbox.

Fetch the target Mutter SRPM from the official source repository. Use the distro's `mock` configuration or an equivalent isolated matching builder. After `%prep`, verify or port the patch against the prepared tree. Add it as the last relevant patch in the native spec and preserve the existing `%autosetup` or explicit `%patch` behavior. Preserve the name, ABI, epoch and dist tag. Choose a local release that sorts above the stock build and below the next official EVR when available. Rebuild the SRPM and binary packages using the native recipe. Review a single DNF transaction for the matching rebuilt packages with `dnf install --assumeno ./<package>.rpm ...`.

The bundled exact fingerprints are for Ubuntu. Fedora and newer distro builds require independent source and runtime validation. The management application's RPM is separate from a rebuilt Mutter RPM.

## Validate, then make it the system default

On Ubuntu 26.04 amd64 the existing private build and one-shot launcher in the patch kit are available for staging a first test. On other distributions, use a disposable VM or separate test environment with the same Shell and Mutter packages. Check native and GI/typelib linkage, full Shell startup, an active AMD internal view, cursor and real external output. With the external output disabled, verify same-session NVIDIA fd cleanup, running Xwayland, remaining client processes, removal and reconnect. A headless build cannot verify display hardware or safe unplug.

Record exact versions for the installed Mutter package set and its companion packages. Download the complete official rollback set into a location accessible from a TTY before activation. Use the same transaction preview above and install the reviewed package set with APT or DNF. That replaces the distro's normal library paths through its package manager. Avoid global library-path overrides and mixing companions from different builds. Keep package updates enabled.

The packaged patch is opt-in. For the verified GNOME Wayland session, identify the active Shell user unit with `systemctl --user list-units 'org.gnome.Shell*.service'`. If the distro uses an instantiated service such as `org.gnome.Shell@ubuntu.service`, create a dedicated drop-in for that actual unit under `/etc/systemd/user/<actual-unit>.d/95-usb4-gpu-manager.conf`. For a single-user default, place the same drop-in under that user's `~/.config/systemd/user/`. Replace the example PCI address with the removable, non-boot NVIDIA device on the target system.

```ini
[Service]
Environment=MUTTER_GPU_RELEASE_POC=1
Environment=MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI=0000:03:00.0
```

Reload the user manager with `systemctl --user daemon-reload`. Save work and log out/in to load the system-default packages and policy. Verify the new Shell mappings and version after login. On a distro that launches Shell differently, use its documented per-session environment mechanism after checking the actual process environment.

For rollback from TTY, remove the specific policy drop-in and reload the user manager. Restore the saved official package set in one transaction using `apt install --allow-downgrades ./<official>.deb ...` or `dnf downgrade ./<official>.rpm ...`, then log in again. Check the preview first and include matching libraries and data. The application package does not install the policy drop-in or replace Mutter.

## Repository update reminders

The watcher is an opt-in systemd user timer. Configure it with the official version used for the current patch base, without the local suffix. The package `mutter` is normally suitable for tracking revisions across library ABI names. `--repo` restricts candidates to an APT repository URI or a DNF repository ID, and can be repeated.

```bash
usb4-gpu-mutter-watch configure apt mutter 50.1-0ubuntu2.5
systemctl --user daemon-reload
systemctl --user enable --now usb4-gpu-mutter-watch.timer
usb4-gpu-mutter-watch check
```

For Fedora, use `configure dnf mutter <official-epoch:version-release>` with `python3-rpm` installed. APT reads the configured binary indexes, refreshed by normal distro updates or `apt update`. DNF refreshes repository metadata as the user. Install `notify-send` for desktop notifications. Failed notifications are retried on a later check. Each notified version is recorded per configuration, and the timer does not install packages, invoke an agent or restart the desktop. After a successful port, configure the new official baseline.
