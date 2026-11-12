# Translating GPU Dock Manager

The application uses Python's standard gettext runtime. Translations are separate
from Python code and use standard UTF-8 PO files that can be edited with Lokalize,
Poedit or a text editor. Translation contributions use GPL-3.0-only.

## Catalogs

- `usb4_gpu_manager/locales/en/LC_MESSAGES/gpu-dock-manager.po`: English.
- `usb4_gpu_manager/locales/zh_CN/LC_MESSAGES/gpu-dock-manager.po`: Simplified Chinese.
- `usb4_gpu_manager/locales/zh_TW/LC_MESSAGES/gpu-dock-manager.po`: Traditional Chinese.
- `usb4_gpu_manager/locales/gpu-dock-manager.pot`: source message template.

English messages are the stable `msgid` keys. Translate only `msgstr`; do not
translate PCI addresses, device nodes, driver names, process names or commands.
Preserve every `{p0}`, `{p1}`, formatting suffix (such as `:.0f`), escape and
newline. Do not translate the template POT file directly. Read source references
and check the dialog context when translating short labels.

## Update an existing language

Install the gettext development tools (Ubuntu/Debian: `sudo apt install gettext`).
From the repository root:

```sh
python3 tools/translations.py extract
# Edit the relevant PO file. Review and clear fuzzy entries after checking them.
make i18n
make check
```

`extract` scans `tr("English source")` calls using Python AST and merges them with
existing PO files. `make i18n` checks format placeholders and compiles MO catalogs.
Commit the PO, compiled MO and updated POT files. End-user installations need only
Python's standard library and the bundled MO files, not gettext tools or OpenCC.

## Add a language

For example, to contribute German:

```sh
mkdir -p usb4_gpu_manager/locales/de/LC_MESSAGES
msginit --no-translator --locale=de \
  --input=usb4_gpu_manager/locales/gpu-dock-manager.pot \
  --output-file=usb4_gpu_manager/locales/de/LC_MESSAGES/gpu-dock-manager.po
# Translate msgstr entries, then:
make i18n
make check
```

The application discovers installed catalogs automatically; adding a language
normally requires no Python changes. Include the appropriate locale code, Language
header and gettext plural metadata. Translate desktop-entry and Polkit metadata in
`data/` too if you want localized launchers and authorization dialogs.

## Language selection and fallback

A user language override (`GPU_DOCK_LANGUAGE`) takes precedence, followed by the
first desktop `LANGUAGE` preference, then `LC_ALL`, `LC_MESSAGES` and `LANG`.
`zh_CN`, `zh_SG` and `zh-Hans` select Simplified Chinese. `zh_TW`, `zh_HK`, `zh_MO`
and `zh-Hant` select Traditional Chinese. English locales select English. Other
locales use a matching installed catalog (or a base-language catalog), otherwise
English. A missing or fuzzy translation also falls back to its English msgid.
Language is selected when the application starts; restart after changing it.

To preview translations in a separate development instance without changing the
system language, use the Python `Application(unique=False)` interface or quit the
existing tray instance before launching:

```sh
GPU_DOCK_LANGUAGE=zh_TW python3 -m usb4_gpu_manager
GPU_DOCK_LANGUAGE=en python3 -m usb4_gpu_manager
```

GTK/libadwaita labels follow the same selected language. The privileged daemon
emits English messages independent of the root account's language; the client
localizes recognized status, errors and events. Machine-readable keys, state
values, process names, driver names and paths stay unchanged. Unrecognized
third-party errors are preserved rather than guessed or translated destructively.
Previously stored Chinese service messages are recognized where catalog entries
are available; raw historical journals remain in their original language.

## Review checklist

Preview the overview, device list, deployment confirmation, removal confirmation,
tray menu and long errors. Check wrapping at a narrow window width. Automated
checks verify catalog coverage, language fallback, placeholders and preservation
of machine-readable data. GUI smoke checks cancel all dialogs and perform no GPU
removal, logout or system configuration changes.
