Name:           usb4-gpu-manager
Version:        %{app_version}
Release:        1%{?dist}
Summary:        Desktop and system service for USB4 external GPUs
License:        GPL-3.0-only AND GPL-2.0-or-later
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4 >= 4.12
Requires:       libadwaita >= 1.4
Requires:       systemd
Requires:       dbus
Requires:       polkit
Requires:       kmod
Recommends:     libnotify
Recommends:     python3-rpm

%description
Manages external NVIDIA GPUs and their display outputs.
Includes an optional Mutter patch, porting skill and update watcher.

%prep
%setup -q

%build

%install
sh packaging/stage.sh "%{buildroot}"

%post
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || :
    dbus-send --system --type=method_call --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ReloadConfig || :
fi

%preun
if [ "$1" -eq 0 ] && [ -d /run/systemd/system ]; then
    systemctl disable --now usb4-gpu-manager.service >/dev/null 2>&1 || :
fi

%postun
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || :
    if [ "$1" -ne 0 ]; then
        systemctl try-restart usb4-gpu-manager.service || :
    else
        dbus-send --system --type=method_call --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ReloadConfig || :
    fi
fi

%files
%license LICENSE
%{_bindir}/usb4-gpu-manager
%{_bindir}/usb4-gpu-managerctl
%{_bindir}/usb4-gpu-mutter-watch
/usr/lib/usb4-gpu-manager
/usr/lib/systemd/system/usb4-gpu-manager.service
/usr/lib/systemd/user/usb4-gpu-mutter-watch.service
/usr/lib/systemd/user/usb4-gpu-mutter-watch.timer
%{_datadir}/usb4-gpu-manager
%{_datadir}/dbus-1/system.d/io.github.usb4gpumanager.Manager.conf
%{_datadir}/dbus-1/system-services/io.github.usb4gpumanager.Manager.service
%{_datadir}/polkit-1/actions/io.github.usb4gpumanager.policy
%{_datadir}/applications/io.github.usb4gpumanager.App.desktop
%doc %{_datadir}/doc/usb4-gpu-manager/copyright

%doc %{_datadir}/doc/usb4-gpu-manager/NOTICE.md
