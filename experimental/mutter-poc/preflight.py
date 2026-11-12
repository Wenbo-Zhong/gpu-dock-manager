#!/usr/bin/python3
"""Check native/GI library identity in a child without initializing any GPU."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SYSTEM_COMPANIONS = Path("/usr/lib/x86_64-linux-gnu/mutter-18")
LIBRARIES = {
    "Meta": ("libmutter-18.so.0", "meta_context_get_type"),
    "Clutter": ("libmutter-clutter-18.so.0", "clutter_actor_get_type"),
    "Cogl": ("libmutter-cogl-18.so.0", "cogl_context_get_type"),
    "Mtk": ("libmutter-mtk-18.so.0", "mtk_region_get_type"),
}


def use_system_companions(prefix):
    # Only the Mutter core contains our changes. GNOME Shell's GI loader searches
    # its compiled-in system directory, so native dependencies must use those
    # same Clutter/Cogl/Mtk objects. LD_LIBRARY_PATH alone cannot ensure this.
    directory = prefix / "lib/x86_64-linux-gnu/mutter-18"
    result = {}
    for namespace, (name, _) in LIBRARIES.items():
        if namespace == "Meta":
            continue
        target = SYSTEM_COMPANIONS / name
        if not target.is_file():
            raise RuntimeError("Missing system companion: " + str(target))
        link = directory / name
        if not link.is_symlink():
            raise RuntimeError("Expected Meson-installed SONAME symlink: " + str(link))
        link.unlink()
        link.symlink_to(target)
        result[name] = str(target.resolve())
    return result


def runtime_environment(manifest):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(manifest["library_paths"])
    return env


def probe(manifest):
    # Emulate the executable's native dependencies and GI's separate dlopen
    # search. Comparing addresses avoids calling get_type twice (which can
    # deadlock or abort with duplicate GType registrations).
    native = ctypes.CDLL("libmutter-18.so.0", mode=ctypes.RTLD_GLOBAL)
    repository = ctypes.CDLL("libgirepository-2.0.so.0")
    repository.gi_repository_new.restype = ctypes.c_void_p
    repository.gi_repository_prepend_search_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    repository.gi_repository_prepend_library_path.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    repository.gi_repository_require.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    repository.gi_repository_require.restype = ctypes.c_void_p
    repository.gi_typelib_symbol.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    repository.gi_typelib_symbol.restype = ctypes.c_int
    repo = repository.gi_repository_new()
    repository.gi_repository_prepend_search_path(repo, os.fsencode(SYSTEM_COMPANIONS))
    repository.gi_repository_prepend_library_path(repo, os.fsencode(SYSTEM_COMPANIONS))
    result = {}
    for namespace, (name, symbol) in LIBRARIES.items():
        library = native if namespace == "Meta" else ctypes.CDLL(name)
        address = ctypes.cast(getattr(library, symbol), ctypes.c_void_p).value
        typelib = repository.gi_repository_require(repo, namespace.encode(), b"18", 0, None)
        if not typelib:
            raise RuntimeError("Cannot load typelib: " + namespace)
        gi_address = ctypes.c_void_p()
        found = repository.gi_typelib_symbol(typelib, symbol.encode(), ctypes.byref(gi_address))
        result[namespace] = {"symbol_matches": bool(found and address == gi_address.value)}
    maps = Path("/proc/self/maps").read_text().splitlines()
    for namespace, (name, _) in LIBRARIES.items():
        paths = sorted({line.split()[-1] for line in maps if "/" + name in line})
        expected = (Path(manifest["prefix"]) / "lib/x86_64-linux-gnu" / name
                    if namespace == "Meta" else SYSTEM_COMPANIONS / name).resolve()
        result[namespace]["loaded_paths"] = paths
        result[namespace]["single_expected_library"] = bool(paths) and all(Path(p).resolve() == expected for p in paths)
    return {"passed": all(v["symbol_matches"] and v["single_expected_library"] for v in result.values()),
            "libraries": result, "gpu_initialized": False}


def check(manifest):
    completed = subprocess.run(["/usr/bin/python3", "-I", str(HERE / "preflight.py"), "--child"],
        input=json.dumps(manifest), env=runtime_environment(manifest),
        capture_output=True, text=True, timeout=10)
    if completed.returncode:
        raise RuntimeError("Mutter GI preflight failed: " + completed.stderr[-2000:])
    result = json.loads(completed.stdout)
    if not result["passed"]:
        raise RuntimeError("拒绝加载混合的 Mutter/GI 库：" + json.dumps(result, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".cache/usb4-gpu-manager/mutter-poc/manifest.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        print(json.dumps(probe(json.load(sys.stdin))))
    else:
        print(json.dumps(check(json.loads(args.manifest.read_text())), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
