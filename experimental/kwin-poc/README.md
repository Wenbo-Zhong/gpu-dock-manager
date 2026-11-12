# KWin GPU reference probe

GPL-3.0-only. This experiment does not replace KWin or install a session override.
The current hypothesis and limitations are in ../../docs/kde-compatibility.md.

Read-only, ordinary user (uses the installed manager service):

```sh
/usr/bin/python3 -I experimental/kwin-poc/control.py inspect
```

Explicit experiment, after saving work and disconnecting all eGPU monitors:

```sh
sudo /usr/bin/python3 -I experimental/kwin-poc/control.py refresh
```

Refresh validates the target twice, sends a single udev DRM change event, and
prints before/after holder snapshots. It does not remove a PCI function, unbind
a driver, kill clients, replace KWin or log out. If holders disappear, use the
manager's ordinary safe-removal workflow and wait for its confirmation before
unplugging. If they persist, retain the output for investigation. No retry loop
or automatic startup policy is installed. One live refresh trial released systemd/logind/KWin/Xwayland holders in all
three samples. Actual safe removal, reconnect and monitor workflows remain
unverified; see the compatibility report.
