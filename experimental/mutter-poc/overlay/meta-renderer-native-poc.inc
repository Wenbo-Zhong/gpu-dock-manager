/* SPDX-License-Identifier: GPL-2.0-or-later
 * Included by meta-renderer-native.c, after the normal unused-GPU predicate.
 * Opt-in research interface. It never removes a PCI device or kills clients.
 */
#define GPU_POC_BUS "org.gnome.Mutter.GpuReleasePoC"
#define GPU_POC_PATH "/org/gnome/Mutter/GpuReleasePoC"
#define GPU_POC_IFACE "org.gnome.Mutter.GpuReleasePoC1"

static const char gpu_poc_xml[] =
  "<node><interface name='" GPU_POC_IFACE "'>"
  "<method name='Inspect'><arg type='s' direction='in'/>"
  "<arg type='a{sv}' direction='out'/></method>"
  "<method name='Reclaim'><arg type='s' direction='in'/>"
  "<arg type='a{sv}' direction='out'/></method>"
  "</interface></node>";

static MetaGpuKms *
gpu_poc_find (MetaRendererNative *native,
              const char         *path)
{
  MetaBackend *backend = meta_renderer_get_backend (META_RENDERER (native));
  GList *l;

  for (l = meta_backend_get_gpus (backend); l; l = l->next)
    if (META_IS_GPU_KMS (l->data) &&
        g_strcmp0 (meta_gpu_kms_get_file_path (l->data), path) == 0)
      return l->data;
  return NULL;
}

static const char *
gpu_poc_reject_reason (MetaRendererNative *native,
                       MetaGpuKms         *gpu)
{
  MetaRenderer *renderer = META_RENDERER (native);
  MetaKmsDevice *device;
  gboolean primary_view = FALSE;
  GList *l;

  if (!gpu || !native->primary_gpu_kms)
    return "unknown GPU or no native primary GPU";
  if (gpu == native->primary_gpu_kms)
    return "primary GPU must never be reclaimed";
  if (g_strcmp0 (meta_kms_device_get_driver_name (
        meta_gpu_kms_get_kms_device (native->primary_gpu_kms)), "amdgpu") != 0)
    return "PoC requires an AMD primary GPU";
  device = meta_gpu_kms_get_kms_device (gpu);
  if (g_strcmp0 (meta_kms_device_get_driver_name (device), "nvidia-drm") != 0)
    return "PoC accepts only an NVIDIA DRM secondary GPU";
  if (!meta_backend_get_launcher (meta_renderer_get_backend (renderer)) ||
      !meta_launcher_is_session_active (meta_backend_get_launcher (
        meta_renderer_get_backend (renderer))))
    return "no active native logind session; run the PoC on the active desktop";
  if (native->pending_mode_set || native->pending_mode_set_views ||
      g_hash_table_size (native->mode_set_updates) != 0)
    return "mode set work is pending";
  if (native->detached_onscreens || native->lingering_onscreens ||
      native->power_save_page_flip_onscreens)
    return "onscreen or power-save page-flip resources remain";
  for (l = meta_renderer_get_views (renderer); l; l = l->next)
    {
      MetaCrtc *crtc = meta_renderer_view_get_crtc (l->data);
      MetaGpu *view_gpu = crtc ? meta_crtc_get_gpu (crtc) : NULL;

      if (view_gpu == META_GPU (gpu))
        return "a current view uses the target GPU";
      if (view_gpu == META_GPU (native->primary_gpu_kms))
        primary_view = TRUE;
    }
  if (!primary_view)
    return "no current view on the AMD primary GPU";
  for (l = meta_kms_device_get_connectors (device); l; l = l->next)
    {
      const MetaKmsConnectorState *state =
        meta_kms_connector_get_current_state (l->data);

      if (state)
        return "target has a connected KMS connector state";
    }
  for (l = meta_kms_device_get_crtcs (device); l; l = l->next)
    if (meta_kms_crtc_is_active (l->data))
      return "target CRTC is active";
  return NULL;
}

static void
gpu_poc_add_string (GVariantBuilder *builder,
                    const char      *key,
                    const char      *value)
{
  g_variant_builder_add (builder, "{sv}", key,
                        g_variant_new_string (value ? value : ""));
}

static GVariant *
gpu_poc_snapshot (MetaRendererNative *native,
                  const char         *path,
                  const char         *outcome,
                  gboolean            cache_before,
                  gboolean            finalized)
{
  GVariantBuilder result;
  MetaGpuKms *gpu = gpu_poc_find (native, path);
  const char *reason = gpu_poc_reject_reason (native, gpu);
  const char *primary_path = native->primary_gpu_kms ?
    meta_gpu_kms_get_file_path (native->primary_gpu_kms) : "";

  g_variant_builder_init (&result, G_VARIANT_TYPE_VARDICT);
  gpu_poc_add_string (&result, "target", path);
  gpu_poc_add_string (&result, "primary", primary_path);
  gpu_poc_add_string (&result, "primary_driver", native->primary_gpu_kms ?
    meta_kms_device_get_driver_name (meta_gpu_kms_get_kms_device (native->primary_gpu_kms)) : "");
  gpu_poc_add_string (&result, "reason", reason);
  gpu_poc_add_string (&result, "outcome", outcome);
  g_variant_builder_add (&result, "{sv}", "eligible", g_variant_new_boolean (!reason));
  g_variant_builder_add (&result, "{sv}", "cache_before", g_variant_new_boolean (cache_before));
  g_variant_builder_add (&result, "{sv}", "cache_present", g_variant_new_boolean (
    gpu && g_hash_table_contains (native->gpu_datas, gpu)));
  g_variant_builder_add (&result, "{sv}", "render_device_finalized", g_variant_new_boolean (finalized));
  g_variant_builder_add (&result, "{sv}", "view_count", g_variant_new_uint32 (
    g_list_length (meta_renderer_get_views (META_RENDERER (native)))));
  g_variant_builder_add (&result, "{sv}", "lingering_count", g_variant_new_uint32 (
    g_list_length (native->lingering_onscreens)));
  return g_variant_new ("(a{sv})", &result);
}

static gboolean
gpu_poc_reclaim_idle (gpointer data)
{
  MetaRendererNative *native = data;
  MetaGpuKms *gpu = gpu_poc_find (native, native->poc_device);
  const char *reason = gpu_poc_reject_reason (native, gpu);
  gboolean had_cache = gpu && g_hash_table_contains (native->gpu_datas, gpu);
  gboolean finalized = FALSE;
  const char *outcome = "rejected";

  native->poc_idle_id = 0;
  if (!reason && had_cache)
    {
      MetaBackend *backend = meta_renderer_get_backend (META_RENDERER (native));
      ClutterBackend *clutter_backend = meta_backend_get_clutter_backend (backend);
      CoglContext *context = clutter_backend_get_cogl_context (clutter_backend);
      CoglDisplay *display = cogl_context_get_display (context);
      MetaRendererNativeGpuData *gpu_data = g_hash_table_lookup (native->gpu_datas, gpu);
      MetaRenderDevice *weak_device = gpu_data->render_device;

      if (!_cogl_winsys_egl_ensure_current (display))
        reason = "cannot make the primary EGL context current";
      else
        {
          GPU_POC_LOG ("event=reclaim-begin path=%s", native->poc_device);
          g_object_add_weak_pointer (G_OBJECT (weak_device), (gpointer *) &weak_device);
          /* Same value destructor used by free_unused_gpu_datas(), target only.
           * Guards are re-evaluated here, at an idle outside frame dispatch.
           */
          g_hash_table_remove (native->gpu_datas, gpu);
          finalized = weak_device == NULL;
          if (weak_device)
            g_object_remove_weak_pointer (G_OBJECT (weak_device), (gpointer *) &weak_device);
          outcome = "cache-reclaimed-not-a-gpu-eject";
          if (!_cogl_winsys_egl_ensure_current (display))
            outcome = "cache-reclaimed-primary-context-restore-failed";
          GPU_POC_LOG ("event=reclaim-end path=%s finalized=%d outcome=%s",
                       native->poc_device, finalized, outcome);
        }
    }
  else if (!reason)
    outcome = "cache-already-absent-no-mutation";

  if (reason)
    GPU_POC_LOG ("event=reclaim-rejected path=%s reason=%s", native->poc_device, reason);
  g_dbus_method_invocation_return_value (native->poc_request,
    gpu_poc_snapshot (native, native->poc_device, reason ? reason : outcome, had_cache, finalized));
  g_clear_object (&native->poc_request);
  g_clear_pointer (&native->poc_device, g_free);
  return G_SOURCE_REMOVE;
}

static void
gpu_poc_method_call (GDBusConnection       *connection,
                     const char            *sender,
                     const char            *path,
                     const char            *interface,
                     const char            *method,
                     GVariant              *parameters,
                     GDBusMethodInvocation *invocation,
                     gpointer               data)
{
  MetaRendererNative *native = data;
  const char *device;

  g_variant_get (parameters, "(&s)", &device);
  if (strlen (device) > 128)
    {
      g_dbus_method_invocation_return_error (invocation, G_IO_ERROR,
                                            G_IO_ERROR_INVALID_ARGUMENT, "device path too long");
      return;
    }
  if (g_str_equal (method, "Inspect"))
    {
      g_dbus_method_invocation_return_value (invocation,
        gpu_poc_snapshot (native, device, "inspect-only", FALSE, FALSE));
      return;
    }
  if (!g_str_equal (method, "Reclaim") || native->poc_request)
    {
      g_dbus_method_invocation_return_error (invocation, G_IO_ERROR,
                                            G_IO_ERROR_BUSY, "unsupported request or PoC busy");
      return;
    }
  native->poc_request = g_object_ref (invocation);
  native->poc_device = g_strdup (device);
  native->poc_idle_id = g_idle_add_full (G_PRIORITY_DEFAULT_IDLE,
                                       gpu_poc_reclaim_idle, native, NULL);
}

static const GDBusInterfaceVTable gpu_poc_vtable = {
  .method_call = gpu_poc_method_call,
};

static void
gpu_poc_bus_acquired (GDBusConnection *connection,
                      const char      *name,
                      gpointer         data)
{
  MetaRendererNative *native = data;
  g_autoptr (GDBusNodeInfo) node = NULL;
  g_autoptr (GError) error = NULL;

  node = g_dbus_node_info_new_for_xml (gpu_poc_xml, &error);
  if (node)
    native->poc_registration_id = g_dbus_connection_register_object (
      connection, GPU_POC_PATH, node->interfaces[0], &gpu_poc_vtable, native, NULL, &error);
  if (error)
    GPU_POC_LOG ("event=interface-error message=%s", error->message);
  else
    {
      native->poc_connection = g_object_ref (connection);
      GPU_POC_LOG ("event=interface-ready version=1 primary=%s",
        native->primary_gpu_kms ? meta_gpu_kms_get_file_path (native->primary_gpu_kms) : "none");
    }
}

static void
gpu_poc_start (MetaRendererNative *native)
{
  gpu_release_poc_clean_child_environment ();
  if (gpu_release_poc_enabled ())
    native->poc_name_id = g_bus_own_name (G_BUS_TYPE_SESSION, GPU_POC_BUS,
      G_BUS_NAME_OWNER_FLAGS_NONE, gpu_poc_bus_acquired, NULL, NULL, native, NULL);
}

static void
gpu_poc_stop (MetaRendererNative *native)
{
  g_clear_handle_id (&native->poc_idle_id, g_source_remove);
  if (native->poc_request)
    {
      g_dbus_method_invocation_return_error (native->poc_request, G_IO_ERROR,
                                            G_IO_ERROR_CANCELLED, "renderer shutting down");
      g_clear_object (&native->poc_request);
    }
  if (native->poc_registration_id)
    {
      g_dbus_connection_unregister_object (native->poc_connection, native->poc_registration_id);
      native->poc_registration_id = 0;
    }
  if (native->poc_name_id)
    {
      g_bus_unown_name (native->poc_name_id);
      native->poc_name_id = 0;
    }
  g_clear_object (&native->poc_connection);
  g_clear_pointer (&native->poc_device, g_free);
}
