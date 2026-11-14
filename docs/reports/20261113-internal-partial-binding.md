# Internal RTX 4070 partial binding investigation

Read-only investigation on 2026-11-13. No driver binding, power policy, service restart or suspend was performed.

## Observed state

- Kernel: 7.0.0-43-generic; NVIDIA: 595.99.02.
- 0000:65:00.0: nvidia; 0000:65:00.1: vfio-pci.
- Both functions: runtime suspended, D3cold; driver_override: (null).
- Boot command line and modprobe configuration reserve both functions for VFIO.

## Timeline (local time)

- Boot: VFIO claims the internal GPU; NVIDIA reports it already bound to vfio-pci.
- 23:13:33: external GPU removal begins.
- 23:13:35: VFIO graphics function emits a VGA arbitration change while NVIDIA modules are removed.
- 23:14:28.960: NVIDIA probes internal graphics.
- 23:14:29.029: VFIO IDs are registered again, after NVIDIA has claimed graphics.
- 23:14:31: internal switch fails its holder check, naming systemd, systemd-logind and gnome-shell. The code performs module loading before a second holder check and before transactional binding.

## Mechanism

The external detach path runs `modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia`. Local configuration declares vfio-pci as a soft dependency of those NVIDIA modules. A read-only `modprobe -n -v -r ...` explicitly includes `rmmod vfio_pci`. Thus external cleanup can also remove the driver isolating the internal card. Subsequent module loading can probe internal graphics before VFIO returns, leaving audio isolated. This is strongly supported by the observed registration order; no live reproduction was attempted.

The configured NVreg_ExcludedGpus safeguard is ineffective here: NVIDIA logs that the GPU could not be excluded because PBI is not supported, followed by kernel warnings.

## Proposed repair, not applied during evidence preservation

Avoid broad NVIDIA module cleanup while any device is VFIO-bound. Keep the internal pair reserved throughout NVIDIA module loading, then verify bindings before any intentional switch. Treat module loading as a potentially mutating operation requiring recovery if it changes either binding. Review the obsolete GPU exclusion option separately. Do not force detach current desktop holders.

This finding explains partial binding; it does not establish the cause of the earlier failed suspend/resume.

## Follow-up implementation and validation

External detach now retains shared driver modules instead of running broad module removal. Internal switching reserves both starting driver bindings while loading modules, verifies that bindings are unchanged, and clears temporary overrides on failure. `make check poc-check` passes (139 Python tests, isolated D-Bus checks and 10 C PoC tests). These changes have not been installed or validated by a live GPU switch.

Suspend/resume remains unresolved. The 2026-11-13 lid test with temporary USB4 bridge wake disabled required a forced reboot; the persisted journal ends after suspend entry, without a recorded successful resume. Temporary wake and debug settings were verified restored after reboot. No claim of reliable lid recovery is made for the experimental Mutter build.
