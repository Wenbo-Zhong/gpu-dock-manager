PYTHON := /usr/bin/python3
VERSION := 0.1.2

.PHONY: run test check gui-check poc-check dist deb rpm i18n i18n-check
run:
	$(PYTHON) -m usb4_gpu_manager
test:
	$(PYTHON) -m unittest discover -v
	/usr/bin/dbus-run-session -- $(PYTHON) -m tests.dbus_smoke
check: i18n-check test
	$(PYTHON) -m compileall -q usb4_gpu_manager data tests tools experimental
	bash -n install.sh uninstall.sh
	sh -n packaging/stage.sh
	sh -n packaging/build-deb.sh
	sh -n packaging/build-rpm.sh
	systemd-analyze verify data/usb4-gpu-manager.service
	desktop-file-validate data/io.github.usb4gpumanager.App.desktop
i18n:
	$(PYTHON) tools/translations.py compile
i18n-check:
	$(PYTHON) tools/translations.py check
gui-check:
	$(PYTHON) -m tests.gui_smoke
poc-check:
	$(PYTHON) experimental/mutter-poc/test.py
dist:
	mkdir -p dist
	tar --exclude=__pycache__ --transform='s,^,usb4-gpu-manager-$(VERSION)/,' -czf dist/usb4-gpu-manager-$(VERSION).tar.gz usb4_gpu_manager data tests tools experimental docs packaging skills install.sh uninstall.sh Makefile pyproject.toml MANIFEST.in README.md ARCHITECTURE.md SECURITY.md CONTRIBUTING.md LICENSE NOTICE.md
deb:
	sh packaging/build-deb.sh $(VERSION)
rpm: dist
	sh packaging/build-rpm.sh $(VERSION)
