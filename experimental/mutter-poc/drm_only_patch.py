"""GPL-2.0-or-later: explicitly enabled, device-scoped DRM-only/CPU-copy PoC."""
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE = 'src/backends/native/'


def apply(files, source, once):
    for name in ('meta-backend-native.c', 'meta-renderer-native.c', 'meta-render-device-gbm.c', 'meta-onscreen-native.c', 'meta-cursor-renderer-native.c', 'meta-drm-lease.c'):
        path = NATIVE + name
        files.setdefault(path, (source / path).read_text())
        files[path] = once(files[path], '#include "config.h"',
            '#include "config.h"\n#include "backends/native/meta-gpu-drm-only-poc.h"')

    name = NATIVE + 'meta-render-device-gbm.c'
    text = files[name]
    text = once(text, '  render_device_gbm->gbm_device = gbm_device;', '''  render_device_gbm->gbm_device = gbm_device;
  if (gpu_poc_drm_only_path (meta_device_file_get_path (device_file)))
    {
      g_object_set_data (G_OBJECT (render_device), GPU_POC_DRM_ONLY_KEY, GINT_TO_POINTER (1));
      GPU_POC_LOG ("event=drm-only-device path=%s", meta_device_file_get_path (device_file));
    }''')
    # All these entry points have declarations before the unique anchor.
    for anchor, returns in (
        ('  if (!META_IS_DRM_BUFFER_GBM (buffer))', 'NULL'),
        ('  egl_display = meta_render_device_get_egl_display (render_device);', 'NULL'),
        ('  if (!meta_egl_has_extensions (egl, EGL_NO_DISPLAY, NULL,', 'EGL_NO_DISPLAY'),
    ):
        text = once(text, anchor, f'''  if (gpu_poc_drm_only (render_device))
    {{
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                   "DRM-only PoC forbids NVIDIA EGL, GBM imports and GPU allocation");
      return {returns};
    }}

''' + anchor)
    text = once(text, '  MetaDrmBufferGbm *buffer_gbm;\n\n  if (n_modifiers == 0)',
        '''  MetaDrmBufferGbm *buffer_gbm;

  if (gpu_poc_drm_only (render_device))
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                   "DRM-only PoC forbids NVIDIA GPU allocation");
      return NULL;
    }
  if (n_modifiers == 0)''')
    files[name] = text

    name = NATIVE + 'meta-backend-native.c'
    files[name] = once(files[name], '          if (meta_render_device_is_hardware_accelerated (render_device))',
        '''          if (gpu_poc_drm_only (render_device))
            return META_RENDER_DEVICE (g_steal_pointer (&render_device_gbm));

          if (meta_render_device_is_hardware_accelerated (render_device))''')

    name = NATIVE + 'meta-renderer-native.c'
    text = files[name]
    start = text.index('meta_gbm_device_from_gpu (MetaGpuKms *gpu_kms)')
    end = text.index('\n}', start) + 2
    body = once(text[start:end], '  render_device = renderer_gpu_data->render_device;',
        '''  if (!renderer_gpu_data || gpu_poc_drm_only (renderer_gpu_data->render_device))
    return NULL;
  render_device = renderer_gpu_data->render_device;''')
    text = text[:start] + body + text[end:]
    text = once(text, '  init_secondary_gpu_data (renderer_gpu_data);', '''  if (gpu_poc_drm_only (render_device))
    {
      renderer_gpu_data->secondary.copy_mode = META_SHARED_FRAMEBUFFER_COPY_MODE_PRIMARY;
      renderer_gpu_data->secondary.copy_mode_primary_force_cpu = TRUE;
      GPU_POC_LOG ("event=drm-only-cpu-copy path=%s", meta_gpu_kms_get_file_path (gpu_kms));
    }
  else
    init_secondary_gpu_data (renderer_gpu_data);''')
    text = once(text, '      flags = meta_kms_device_get_flags (kms_device);', '''      if (gpu_release_poc_enabled () && g_getenv ("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI") &&
          g_strcmp0 (meta_kms_device_get_driver_name (kms_device), "amdgpu") != 0)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                       "DRM-only PoC requires an AMD primary GPU");
          return FALSE;
        }
      flags = meta_kms_device_get_flags (kms_device);''')
    files[name] = text

    name = NATIVE + 'meta-onscreen-native.c'
    text = files[name]
    text = once(text, '  MetaFrameNative *frame_native = meta_frame_native_from_frame (frame);\n\n  renderer_gpu_data =',
        '''  MetaFrameNative *frame_native = meta_frame_native_from_frame (frame);

  {
    MetaGpuKms *output_gpu = META_GPU_KMS (meta_crtc_get_gpu (onscreen_native->crtc));
    MetaRendererNativeGpuData *output_data = meta_renderer_native_get_gpu_data (renderer_native, output_gpu);
    if (output_data && gpu_poc_drm_only (output_data->render_device))
      {
        g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                     "DRM-only output requires compositing and CPU copy");
        return FALSE;
      }
  }

  renderer_gpu_data =''')
    # One diagnostic per output allocation, not per frame.
    text = once(text, '  onscreen_native->secondary_gpu_state = secondary_gpu_state;\n\n  return TRUE;\n}\n\nstatic gboolean\ninit_secondary_gpu_state (',
        '''  onscreen_native->secondary_gpu_state = secondary_gpu_state;
  if (gpu_poc_drm_only (render_device))
    GPU_POC_LOG ("event=drm-only-output-allocated path=%s width=%d height=%d format=0x%x",
                 meta_render_device_get_name (render_device), width, height, drm_format);

  return TRUE;
}

static gboolean
init_secondary_gpu_state (''')
    files[name] = text
    name = NATIVE + 'meta-cursor-renderer-native.c'
    text = once(files[name],
        '  cursor_renderer_gpu_data =\n    meta_create_cursor_renderer_native_gpu_data (gpu_kms);\n\n  gbm_device =',
        '''  cursor_renderer_gpu_data =
    meta_create_cursor_renderer_native_gpu_data (gpu_kms);
  if (gpu_poc_drm_only_path (meta_kms_device_get_path (kms_device)))
    {
      cursor_renderer_gpu_data->hw_cursor_broken = TRUE;
      GPU_POC_LOG ("event=drm-only-software-cursor path=%s", meta_kms_device_get_path (kms_device));
      return;
    }

  gbm_device =''')
    text = once(text, '  gboolean cursor_changed, cursor_hw_managed = FALSE;',
        '''  gboolean cursor_changed, cursor_hw_managed = FALSE;
  gboolean per_view_overlay = gpu_release_poc_enabled () &&
    g_getenv ("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI") != NULL;
  gboolean any_software_view = FALSE;''')
    text = once(text, '          cursor_stage_view->has_hw_cursor = has_hw_cursor;',
        '''          cursor_stage_view->has_hw_cursor = has_hw_cursor;

          /* Normal view painting, cursor damage and direct scanout all
           * respect this existing per-view overlay inhibition. */
          if (per_view_overlay)
            {
              if (has_hw_cursor)
                meta_stage_view_inhibit_cursor_overlay (view);
              else
                {
                  meta_stage_view_uninhibit_cursor_overlay (view);
                  cursor_stage_view->needs_emit_painted = FALSE;
                }
              g_object_set_data (G_OBJECT (view), GPU_POC_HW_CURSOR_VIEW_KEY,
                                 GINT_TO_POINTER (has_hw_cursor));
            }''')
    text = once(text, '      cursor_hw_managed |= cursor_stage_view->has_hw_cursor;',
        '''      cursor_hw_managed |= cursor_stage_view->has_hw_cursor;
      any_software_view |= !cursor_stage_view->has_hw_cursor;''')
    text = once(text, '  return (!cursor_hw_managed &&',
        '  return ((per_view_overlay ? any_software_view : !cursor_hw_managed) &&')
    text = once(text, '  cursor_renderer_gpu_data->cursor_height = height;',
        '''  cursor_renderer_gpu_data->cursor_height = height;
  if (gpu_release_poc_enabled () && g_getenv ("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI"))
    GPU_POC_LOG ("event=drm-only-hardware-cursor-available path=%s",
                 meta_kms_device_get_path (kms_device));''')
    files[name] = text

    name = 'src/backends/meta-cursor-renderer.c'
    text = once((source / name).read_text(), '#include "backends/meta-stage-private.h"',
        '#include "backends/meta-stage-private.h"\n#include "backends/native/meta-gpu-drm-only-poc.h"')
    files[name] = once(text, '  if (priv->displayed_cursor && priv->needs_overlay)',
        '''  /* Native prepare_frame already emits cursor-painted for this view. */
  if (priv->displayed_cursor && priv->needs_overlay &&
      !g_object_get_data (G_OBJECT (stage_view), GPU_POC_HW_CURSOR_VIEW_KEY))''')

    name = NATIVE + 'meta-drm-lease.c'
    text = once(files[name], '''  GList *new_devices;
  GList *l;

  new_devices = g_list_copy (meta_kms_get_devices (kms));

  for (l = new_devices; l; l = l->next)
    {
      MetaKmsDevice *kms_device = l->data;''',
        '''  GList *new_devices = NULL;
  GList *l;

  for (l = meta_kms_get_devices (kms); l; l = l->next)
    {
      MetaKmsDevice *kms_device = l->data;
      const char *path = meta_kms_device_get_path (kms_device);

      /* Binding the lease global hands clients a persistent card fd,
       * even when no connector is available for leasing. */
      if (g_object_get_data (G_OBJECT (kms_device), GPU_POC_DRM_ONLY_KEY))
        continue;
      if (gpu_poc_drm_only_path (path))
        {
          g_object_set_data (G_OBJECT (kms_device), GPU_POC_DRM_ONLY_KEY,
                             GINT_TO_POINTER (1));
          GPU_POC_LOG ("event=drm-only-lease-device-skipped path=%s", path);
          continue;
        }
      new_devices = g_list_prepend (new_devices, kms_device);''')
    text = once(text, '  lease_manager->devices = new_devices;',
        '  lease_manager->devices = g_list_reverse (new_devices);')
    text = once(text, '''  MetaKms *kms =
    meta_backend_native_get_kms (META_BACKEND_NATIVE (lease_manager->backend));
  GList *new_connectors = NULL;''', '  GList *new_connectors = NULL;')
    files[name] = once(text, '''  for (l = meta_kms_get_devices (kms); l; l = l->next)
    {
      MetaKmsDevice *kms_device = l->data;

      for (o = meta_kms_device_get_connectors''',
        '''  for (l = lease_manager->devices; l; l = l->next)
    {
      MetaKmsDevice *kms_device = l->data;

      for (o = meta_kms_device_get_connectors''')
    files[NATIVE + 'meta-gpu-drm-only-poc.h'] = (HERE / 'overlay/meta-gpu-drm-only-poc.h').read_text()
    return files
