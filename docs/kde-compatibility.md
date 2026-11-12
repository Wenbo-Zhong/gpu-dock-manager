# KDE Plasma compatibility — 2026-11-12

## Implemented scope

The ordinary-user client selects a KScreen adapter in a KDE session. GNOME retains
its Mutter adapter. Both internal-GPU and eGPU display controls use the selected
adapter, with separate KDE backups for the two GPUs. The root device-removal
checks are unchanged.

KScreen reads use `kscreen-doctor -j`. Connected output names must map uniquely
to DRM connectors, and complete EDIDs are hashed for identity checks. Disable
requires an enabled internal panel on another GPU. Restore requires matching
GPU identity, EDIDs, saved post-disable layout and original mode definitions.
Modes, positions, fractional scales, rotations and priorities are restored in
one CLI invocation. Unsupported mirrored and follow-preferred-mode layouts are
rejected. A failed disable attempts to restore the original layout only while
screen identities remain unchanged. KScreen may persist display changes.

KDE logout uses `org.kde.LogoutPrompt.promptLogout`, inspected on the running
session, so the desktop presents its own confirmation. No logout was triggered
while testing. The existing `mutter-device-ignore` rule has no effect on KWin;
the queued-removal dialog explains that immediately logging back into KDE can
reacquire the card. This path still needs real-world validation.

## Evidence and limitations

- Current session: Plasma 6.6 Wayland, AMD internal panel, RTX PRO 5000 eGPU.
- Live read-only KScreen status and tray hide/restore tests passed.
- Current KScreen output: internal panel only; no external monitor connected.
- Mocked tests cover disable/restore, scaling/rotation, stale/busy state,
  inactive internal panel, changed EDID/layout, invalid/mirrored configurations,
  rollback and session-adapter selection.
- Actual external-screen changes, KDE X11 and logout/removal combinations have
  not been validated. No physical GPU removal, compositor restart or logout was
  executed by the development checks.

## KWin GPU-release investigation

The live service reports systemd, logind, KWin and Xwayland holding the eGPU's
primary DRM node even without an external monitor. They are genuine holders;
the manager continues to reject safe removal.

Review of the Plasma/6.6 DRM backend shows that disabling outputs invokes
`releaseUnusedBuffers()` on GPU objects. Closing the session's DRM descriptor
happens in `DrmGpu` destruction. Releasing buffers therefore does not, by itself,
prove that KWin has released the GPU object or its session descriptor.

The backend also supports a `KWIN_DRM_DEVICES` allowlist. Excluding the eGPU at
session startup could avoid compositor access, but would also prevent KWin from
using that GPU's display connectors. No such session policy has been installed.

Next investigation: trace ownership of secondary DrmGpu objects, EGL resources,
DRM-lease announcements to Xwayland and logind device references. A release PoC
must retire lease announcements and graphics resources before destroying only
an idle secondary GPU, preserve the AMD desktop, and support rediscovery when
external outputs are needed. No KWin patch is implemented yet.

## Primary references

- [KScreen doctor source](https://github.com/KDE/libkscreen/blob/Plasma/6.6/src/doctor/doctor.cpp)
- [KWin DRM backend](https://github.com/KDE/kwin/blob/Plasma/6.6/src/backends/drm/drm_backend.cpp)
- [KWin DRM GPU lifetime](https://github.com/KDE/kwin/blob/Plasma/6.6/src/backends/drm/drm_gpu.cpp)

## Follow-up: startup-order hypothesis and guarded refresh probe

Upstream tag **v6.6.6** was inspected in addition to the moving Plasma/6.6 branch.
The installed Ubuntu package is 4:6.6.6-0ubuntu0.1. Its distro-specific patches
have not yet been compared. Downloaded upstream-file hashes are recorded in
reports/evidence/20261112-kwin-v6.6.6-source-fingerprints.json.

In v6.6.6, initialization calls `updateOutputs()` before sorting GPUs to prefer
one with an internal output. Cleanup inside `updateOutputs()` excludes the GPU
currently considered primary. **Hypothesis:** if an output-less eGPU is first in
the initial device enumeration, it can survive cleanup and only become secondary
after sorting. The AMD renderer reported by the current KWin session supports
the plausibility of an idle secondary, but does not prove this object ordering.
No internal KWin object trace has been collected yet.

A DRM change event calls `updateOutputs()` again on an active tracked GPU. This
could retire a now-secondary output-less GPU using the existing lifecycle.
The resulting `gpuRemoved` signal also reaches the DRM lease manager, whose
remove path withdraws connectors and removes the Wayland global. Whether the
running Xwayland closes its descriptor in this sequence requires measurement.

`experimental/kwin-poc/control.py inspect` performs only a status inspection.
Its explicit `refresh` experiment requires root, validates a unique non-boot
NVIDIA eGPU with no connected/enabled physical output, an enabled AMD internal
panel, a complete holder scan, and no holders beyond KWin/Xwayland/systemd/logind.
It rechecks identity and workloads, sends one DRM `change` event and samples
holders three times. It never reports permission to unplug. Root-service safe
removal remains a separate operation. The event experiment has now passed one live trial; see the result below.

References for the exact upstream base:
- https://github.com/KDE/kwin/blob/v6.6.6/src/backends/drm/drm_backend.cpp
- https://github.com/KDE/kwin/blob/v6.6.6/src/backends/drm/drm_gpu.cpp
- https://github.com/KDE/kwin/blob/v6.6.6/src/wayland/drmlease_v1.cpp

## Live refresh result

The user ran the guarded refresh experiment on 2026-11-12. Before the event,
systemd, logind, KWin and Xwayland held the target primary DRM node. All three
subsequent samples reported a complete scan with no holders and a passing
ordinary detach check. The GPU remained bound and present. This establishes
that the existing KWin release path can release those references in this
configuration without an explicit logout; the exact startup-order cause remains
unproven. A passing check does not mean PCI removal has occurred.

Sanitized evidence: reports/evidence/20261112-kwin-drm-refresh-live.json.
Actual safe removal, reconnect, repeated trials and external-monitor workflows
still need verification before this can be integrated into automatic removal.
