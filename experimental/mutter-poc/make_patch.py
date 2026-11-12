#!/usr/bin/python3
"""Generate the opt-in research patch from the audited Ubuntu 50.1 source.

Generated patch and overlays: GPL-2.0-or-later (Mutter-compatible).
Does not modify the supplied source tree or the installed compositor.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = "src/backends/native/"


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Source anchor is not unique: " + old[:100])
    return text.replace(old, new, 1)


def patched_files(source):
    files = {}
    names = [NATIVE + n for n in ("meta-renderer-native.c", "meta-device-pool.c", "meta-render-device.c", "meta-render-device-gbm.c", "meta-kms-impl-device.c")]
    for name in names:
        files[name] = (source / name).read_text()
        files[name] = once(files[name], '#include "config.h"', '#include "config.h"\n#include "backends/native/meta-gpu-release-poc.h"')

    name = "src/backends/meta-egl.c"
    text = (source / name).read_text()
    text = once(text, '#include "config.h"', '#include "config.h"\n#include "backends/native/meta-gpu-release-poc.h"')
    for operation, call in (
        ("initialize", "eglInitialize (display, NULL, NULL)"),
        ("terminate", "eglTerminate (display)"),
        ("query-devices", "egl->eglQueryDevicesEXT (max_devices,\n                                devices,\n                                num_devices)"),
    ):
        pointer = "egl" if operation == "query-devices" else "display"
        replacement = f'''  EGLBoolean poc_result;

  gpu_release_poc_fds ("{operation}-before", {pointer});
  poc_result = {call};
  GPU_POC_LOG ("event=egl-call operation={operation} object=%p result=%u", {pointer}, poc_result);
  gpu_release_poc_fds ("{operation}-after", {pointer});
  if (!poc_result)'''
        # Declare at the function's beginning for Mutter's declaration style.
        # query-devices has an extension availability check before this call.
        if operation == "query-devices":
            replacement = replacement.replace("  EGLBoolean poc_result;\n\n", "")
            anchor = "  if (!is_egl_proc_valid (egl->eglQueryDevicesEXT, error))"
            text = once(text, anchor, "  EGLBoolean poc_result;\n\n" + anchor)
        text = once(text, "  if (!" + call + ")", replacement)
    text = once(text, "  display = egl->eglGetPlatformDisplayEXT (platform,", '''  gpu_release_poc_fds ("get-platform-display-before", native_display);
  display = egl->eglGetPlatformDisplayEXT (platform,''')
    text = once(text, "  if (display == EGL_NO_DISPLAY)\n    {\n      set_egl_error", '''  GPU_POC_LOG ("event=egl-display platform=0x%x native=%p display=%p", platform, native_display, display);
  gpu_release_poc_fds ("get-platform-display-after", display);
  if (display == EGL_NO_DISPLAY)
    {
      set_egl_error''')
    text = once(text, "  *out_string = device_string;", '''  if (name == 0x3233 || name == 0x3377)
    GPU_POC_LOG ("event=egl-device-node device=%p attribute=0x%x node=%s",
                 device, name, device_string ? device_string : "none");
  *out_string = device_string;''')
    files[name] = text

    name = NATIVE + "meta-renderer-native.c"
    text = files[name]
    text = once(text, '#include "backends/meta-color-device.h"', '#include "backends/meta-color-device.h"\n#include "backends/meta-backend-private.h"\n#include "backends/meta-launcher.h"\n#include "backends/native/meta-kms-connector.h"')
    text = once(text, "  GHashTable *gpu_datas;", """  GHashTable *gpu_datas;

  GDBusConnection *poc_connection;
  GDBusMethodInvocation *poc_request;
  char *poc_device;
  guint poc_name_id;
  guint poc_registration_id;
  guint poc_idle_id;""")
    text = once(text, "  MetaGpuKms *gpu_kms = renderer_gpu_data->gpu_kms;", """  MetaGpuKms *gpu_kms = renderer_gpu_data->gpu_kms;

  GPU_POC_LOG ("event=gpu-data-free path=%s", gpu_kms ?
    meta_gpu_kms_get_file_path (gpu_kms) : "surfaceless");""")
    text = once(text, "  return !g_hash_table_contains (used_gpus, key);", """  GPU_POC_LOG ("event=normal-cache-decision path=%s retained=%d",
    key ? meta_gpu_kms_get_file_path (key) : "surfaceless",
    g_hash_table_contains (used_gpus, key));
  return !g_hash_table_contains (used_gpus, key);""")
    text = once(text, "static void\nrelease_unused_gpus_idle", '#include "backends/native/meta-renderer-native-poc.inc"\n\nstatic void\nrelease_unused_gpus_idle')
    text = once(text, "  g_hash_table_foreach_remove (renderer_native->gpu_datas,", """  GPU_POC_LOG ("event=normal-cache-pass views=%u lingering=%u detached=%u pending=%d",
    g_list_length (meta_renderer_get_views (renderer)),
    g_list_length (renderer_native->lingering_onscreens),
    g_list_length (renderer_native->detached_onscreens), renderer_native->pending_mode_set);
  g_hash_table_foreach_remove (renderer_native->gpu_datas,""")
    text = once(text, "  g_hash_table_insert (renderer_native->gpu_datas,\n                       gpu_kms,", """  GPU_POC_LOG ("event=gpu-data-created path=%s", gpu_kms ?
    meta_gpu_kms_get_file_path (gpu_kms) : "surfaceless");
  g_hash_table_insert (renderer_native->gpu_datas,
                       gpu_kms,""")
    text = once(text, "  return TRUE;\n}\n\nstatic void\ninitable_iface_init (GInitableIface *initable_iface)", "  gpu_poc_start (renderer_native);\n  return TRUE;\n}\n\nstatic void\ninitable_iface_init (GInitableIface *initable_iface)")
    text = once(text, "  clear_detached_onscreens (renderer_native);\n\n  G_OBJECT_CLASS", "  gpu_poc_stop (renderer_native);\n  clear_detached_onscreens (renderer_native);\n\n  G_OBJECT_CLASS")
    files[name] = text

    name = NATIVE + "meta-kms-impl-device.c"
    text = files[name]
    text = once(text, "  priv->fd_hold_count++;", """  priv->fd_hold_count++;
  if (gpu_release_poc_trace_device (priv->path))
    GPU_POC_LOG ("event=kms-hold path=%s holds=%d", priv->path, priv->fd_hold_count);""")
    text = once(text, "  priv->fd_hold_count--;", """  priv->fd_hold_count--;
  if (gpu_release_poc_trace_device (priv->path))
    GPU_POC_LOG ("event=kms-unhold path=%s holds=%d", priv->path, priv->fd_hold_count);""")
    text = once(text, "  priv->device_file = device_file;", """  priv->device_file = device_file;
  if (gpu_release_poc_trace_device (priv->path))
    GPU_POC_LOG ("event=kms-reopen path=%s", priv->path);""")
    files[name] = text

    name = NATIVE + "meta-device-pool.c"
    text = files[name]
    text = once(text, "  grefcount ref_count;", "  grefcount ref_count;\n  guint poc_refs;")
    text = once(text, "  g_ref_count_init (&file->ref_count);", "  g_ref_count_init (&file->ref_count);\n  file->poc_refs = 1;")
    text = once(text, "  file->flags = flags;\n\n  return file;", """  file->flags = flags;
  if (gpu_release_poc_trace_device (path))
    GPU_POC_LOG ("event=device-open path=%s fd=%d refs=%u", path, fd, file->poc_refs);

  return file;""")
    text = once(text, "  g_ref_count_inc (&file->ref_count);", """  g_ref_count_inc (&file->ref_count);
  file->poc_refs++;
  if (gpu_release_poc_trace_device (file->path))
    GPU_POC_LOG ("event=device-acquire path=%s fd=%d refs=%u",
                 file->path, file->fd, file->poc_refs);""")
    text = once(text, "  if (!g_ref_count_dec (&file->ref_count))", """  file->poc_refs--;
  if (gpu_release_poc_trace_device (file->path))
    GPU_POC_LOG ("event=device-release path=%s fd=%d refs=%u",
                 file->path, file->fd, file->poc_refs);
  if (!g_ref_count_dec (&file->ref_count))""")
    text = once(text, "  close (file->fd);", """  close (file->fd);
  if (gpu_release_poc_trace_device (file->path))
    GPU_POC_LOG ("event=device-close-returned path=%s fd=%d", file->path, file->fd);""")
    files[name] = text

    name = NATIVE + "meta-render-device.c"
    text = files[name]
    text = once(text, "  priv->egl_display = egl_display;", '''  GPU_POC_LOG ("event=render-device-display object=%p type=%s path=%s display=%p",
    render_device, G_OBJECT_TYPE_NAME (render_device),
    priv->device_file ? meta_device_file_get_path (priv->device_file) : "none", egl_display);
  priv->egl_display = egl_display;''')
    text = once(text, "  if (priv->egl_display != EGL_NO_DISPLAY)\n    {\n      meta_egl_terminate", """  GPU_POC_LOG ("event=render-device-dispose path=%s",
    priv->device_file ? meta_device_file_get_path (priv->device_file) : "none");
  if (priv->egl_display != EGL_NO_DISPLAY)
    {
      GPU_POC_LOG ("event=egl-terminate-begin display=%p", priv->egl_display);
      meta_egl_terminate""")
    text = once(text, "      priv->egl_display = EGL_NO_DISPLAY;", "      GPU_POC_LOG (\"event=egl-terminate-returned display=%p\", priv->egl_display);\n      priv->egl_display = EGL_NO_DISPLAY;")
    text = once(text, "  g_clear_pointer (&priv->device_file, meta_device_file_release);", """  GPU_POC_LOG ("event=render-device-finalize path=%s",
    priv->device_file ? meta_device_file_get_path (priv->device_file) : "none");
  g_clear_pointer (&priv->device_file, meta_device_file_release);""")
    files[name] = text

    name = NATIVE + "meta-render-device-gbm.c"
    files[name] = once(files[name], "  gbm_device = gbm_create_device (meta_device_file_get_fd (device_file));", '''  gpu_release_poc_fds ("gbm-create-before", render_device);
  gbm_device = gbm_create_device (meta_device_file_get_fd (device_file));
  GPU_POC_LOG ("event=gbm-create object=%p path=%s fd=%d gbm=%p",
    render_device, meta_device_file_get_path (device_file), meta_device_file_get_fd (device_file), gbm_device);
  gpu_release_poc_fds ("gbm-create-after", gbm_device);''')
    files[name] = once(files[name], "  g_clear_pointer (&render_device_gbm->gbm_device, gbm_device_destroy);", """  GPU_POC_LOG ("event=gbm-destroy-begin device=%p", render_device_gbm->gbm_device);
  gpu_release_poc_fds ("gbm-destroy-before", render_device_gbm->gbm_device);
  g_clear_pointer (&render_device_gbm->gbm_device, gbm_device_destroy);
  gpu_release_poc_fds ("gbm-destroy-after", render_device_gbm);
  GPU_POC_LOG ("event=gbm-destroy-returned");""")
    for name in ("meta-gpu-release-poc.h", "meta-renderer-native-poc.inc"):
        files[NATIVE + name] = (HERE / "overlay" / name).read_text()
    from drm_only_patch import apply
    return apply(files, source, once)


def generate(source, output):
    files = patched_files(source)
    patch = []
    fingerprints = {}
    for name, text in sorted(files.items()):
        path = source / name
        old = path.read_text() if path.exists() else ""
        fingerprints[name] = hashlib.sha256(old.encode()).hexdigest() if old else None
        patch.extend(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                     fromfile="a/" + name if old else "/dev/null", tofile="b/" + name))
    output.write_text("".join(patch))
    output.with_suffix(".sources.json").write_text(json.dumps(fingerprints, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "gpu-release-poc.patch")
    args = parser.parse_args()
    generate(args.source, args.output)
