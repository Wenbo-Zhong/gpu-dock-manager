/* SPDX-License-Identifier: GPL-2.0-or-later
 * Compile the actual PoC interface against inert GPU stubs and real GIO D-Bus.
 * This tests the guards, idle transaction, weak lifetime and shutdown paths;
 * it cannot validate EGL, NVIDIA or real Mutter's reference graph.
 */
#include <gio/gio.h>
#include <string.h>
#include "meta-gpu-release-poc.h"
#include "meta-gpu-drm-only-poc.h"

typedef struct { const char *path, *driver; GList *connectors, *crtcs; } MetaGpuKms;
typedef MetaGpuKms MetaGpu;
typedef MetaGpuKms MetaKmsDevice;
typedef MetaGpuKms MetaCrtc;
typedef struct { MetaCrtc *crtc; } MetaRendererView;
typedef struct { gboolean connected; } MetaKmsConnector;
typedef struct { gboolean present; } MetaKmsConnectorState;
typedef GObject MetaRenderDevice;
typedef struct { MetaRenderDevice *render_device; } MetaRendererNativeGpuData;
typedef struct {
  MetaGpuKms *primary_gpu_kms;
  GList *gpus, *views, *pending_mode_set_views, *detached_onscreens;
  GList *lingering_onscreens, *power_save_page_flip_onscreens;
  gboolean pending_mode_set, active;
  GHashTable *gpu_datas, *mode_set_updates;
  GDBusConnection *poc_connection;
  GDBusMethodInvocation *poc_request;
  char *poc_device;
  guint poc_name_id, poc_registration_id, poc_idle_id;
} MetaRendererNative;
typedef MetaRendererNative MetaRenderer;
typedef MetaRendererNative MetaBackend;
typedef MetaRendererNative ClutterBackend;
typedef MetaRendererNative CoglContext;
typedef MetaRendererNative CoglDisplay;

#define META_RENDERER(x) (x)
#define META_GPU(x) (x)
#define META_IS_GPU_KMS(x) ((x) != NULL)
static gboolean context_ok = TRUE;
static guint context_calls, fail_context_on_call;
static MetaBackend *meta_renderer_get_backend (MetaRenderer *n) { return n; }
static GList *meta_backend_get_gpus (MetaBackend *n) { return n->gpus; }
static const char *meta_gpu_kms_get_file_path (MetaGpuKms *g) { return g->path; }
static MetaKmsDevice *meta_gpu_kms_get_kms_device (MetaGpuKms *g) { return g; }
static const char *meta_kms_device_get_driver_name (MetaKmsDevice *g) { return g->driver; }
static MetaBackend *meta_backend_get_launcher (MetaBackend *n) { return n->active ? n : NULL; }
static gboolean meta_launcher_is_session_active (MetaBackend *n) { return n->active; }
static GList *meta_renderer_get_views (MetaRenderer *n) { return n->views; }
static MetaCrtc *meta_renderer_view_get_crtc (MetaRendererView *v) { return v->crtc; }
static MetaGpu *meta_crtc_get_gpu (MetaCrtc *c) { return c; }
static GList *meta_kms_device_get_connectors (MetaKmsDevice *g) { return g->connectors; }
static const MetaKmsConnectorState *meta_kms_connector_get_current_state (MetaKmsConnector *c)
{ static const MetaKmsConnectorState s = { TRUE }; return c->connected ? &s : NULL; }
static GList *meta_kms_device_get_crtcs (MetaKmsDevice *g) { return g->crtcs; }
static gboolean meta_kms_crtc_is_active (gpointer crtc) { return GPOINTER_TO_INT (crtc); }
static ClutterBackend *meta_backend_get_clutter_backend (MetaBackend *n) { return n; }
static CoglContext *clutter_backend_get_cogl_context (ClutterBackend *n) { return n; }
static CoglDisplay *cogl_context_get_display (CoglContext *n) { return n; }
static gboolean _cogl_winsys_egl_ensure_current (CoglDisplay *n)
{ context_calls++; return context_ok && context_calls != fail_context_on_call; }

#include "meta-renderer-native-poc.inc"

typedef struct {
  MetaRendererNative native;
  MetaGpuKms amd, nvidia;
  MetaRendererView view;
  GDBusConnection *bus;
} Fixture;

static void free_data (gpointer pointer)
{
  MetaRendererNativeGpuData *data = pointer;
  g_object_unref (data->render_device);
  g_free (data);
}

static void setup (Fixture *f, gconstpointer unused)
{
  g_autoptr (GError) error = NULL;
  f->amd.path = "/dev/dri/card1"; f->amd.driver = "amdgpu";
  f->nvidia.path = "/dev/dri/card0"; f->nvidia.driver = "nvidia-drm";
  f->native.primary_gpu_kms = &f->amd;
  f->native.active = TRUE;
  f->native.gpus = g_list_append (g_list_append (NULL, &f->amd), &f->nvidia);
  f->view.crtc = &f->amd;
  f->native.views = g_list_append (NULL, &f->view);
  f->native.gpu_datas = g_hash_table_new_full (NULL, NULL, NULL, free_data);
  f->native.mode_set_updates = g_hash_table_new (NULL, NULL);
  MetaRendererNativeGpuData *data = g_new0 (MetaRendererNativeGpuData, 1);
  data->render_device = g_object_new (G_TYPE_OBJECT, NULL);
  g_hash_table_insert (f->native.gpu_datas, &f->nvidia, data);
  f->bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, &error);
  g_assert_no_error (error);
  gpu_poc_bus_acquired (f->bus, GPU_POC_BUS, &f->native);
  g_assert_cmpuint (f->native.poc_registration_id, >, 0);
  context_ok = TRUE;
  context_calls = fail_context_on_call = 0;
}

static void teardown (Fixture *f, gconstpointer unused)
{
  gpu_poc_stop (&f->native);
  g_clear_object (&f->bus);
  g_hash_table_unref (f->native.gpu_datas);
  g_hash_table_unref (f->native.mode_set_updates);
  g_list_free (f->native.views);
  g_list_free (f->native.gpus);
  g_list_free (f->nvidia.connectors);
  g_list_free (f->nvidia.crtcs);
}

typedef struct { GVariant *value; GError *error; gboolean done; } Reply;
static void replied (GObject *bus, GAsyncResult *result, gpointer pointer)
{
  Reply *reply = pointer;
  reply->value = g_dbus_connection_call_finish (G_DBUS_CONNECTION (bus), result, &reply->error);
  reply->done = TRUE;
}

static void begin (Fixture *f, const char *method, const char *device, Reply *reply)
{
  g_dbus_connection_call (f->bus, g_dbus_connection_get_unique_name (f->bus),
    GPU_POC_PATH, GPU_POC_IFACE, method, g_variant_new ("(s)", device),
    G_VARIANT_TYPE ("(a{sv})"), G_DBUS_CALL_FLAGS_NONE, 3000, NULL, replied, reply);
}

static void finish (Reply *reply)
{
  gint64 deadline = g_get_monotonic_time () + 4000000;
  while (!reply->done && g_get_monotonic_time () < deadline)
    { g_main_context_iteration (NULL, FALSE); g_usleep (1000); }
  g_assert_true (reply->done);
}

static GVariant *call (Fixture *f, const char *method, const char *device)
{
  Reply reply = { 0 };
  begin (f, method, device, &reply);
  finish (&reply);
  g_assert_no_error (reply.error);
  GVariant *dict = g_variant_get_child_value (reply.value, 0);
  g_variant_unref (reply.value);
  return dict;
}

static gboolean flag (GVariant *dict, const char *key)
{ gboolean value = FALSE; g_assert_true (g_variant_lookup (dict, key, "b", &value)); return value; }

static void test_primary (Fixture *f, gconstpointer unused)
{
  g_autoptr (GVariant) reply = call (f, "Reclaim", f->amd.path);
  g_assert_false (flag (reply, "eligible"));
  g_assert_true (g_hash_table_contains (f->native.gpu_datas, &f->nvidia));
}

static void test_release (Fixture *f, gconstpointer unused)
{
  MetaGpuKms *original_primary = f->native.primary_gpu_kms;
  g_autoptr (GVariant) inspect = call (f, "Inspect", f->nvidia.path);
  g_assert_true (flag (inspect, "eligible"));
  g_assert_true (flag (inspect, "cache_present"));
  g_autoptr (GVariant) reply = call (f, "Reclaim", f->nvidia.path);
  g_assert_true (flag (reply, "cache_before"));
  g_assert_false (flag (reply, "cache_present"));
  g_assert_true (flag (reply, "render_device_finalized"));
  g_assert_true (f->native.primary_gpu_kms == original_primary);
  g_assert_true (f->view.crtc == &f->amd);
  g_autoptr (GVariant) again = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (again, "cache_before"));
}

static void test_guards (Fixture *f, gconstpointer unused)
{
  MetaKmsConnector connector = { TRUE };
  f->nvidia.connectors = g_list_append (NULL, &connector);
  g_autoptr (GVariant) connected = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (connected, "eligible"));
  connector.connected = FALSE;
  f->native.pending_mode_set = TRUE;
  g_autoptr (GVariant) pending = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (pending, "eligible"));
  f->native.pending_mode_set = FALSE;
  f->native.active = FALSE;
  g_autoptr (GVariant) inactive = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (inactive, "eligible"));
  f->native.active = TRUE;
  f->view.crtc = &f->nvidia;
  g_autoptr (GVariant) view = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (view, "eligible"));
  g_assert_true (g_hash_table_contains (f->native.gpu_datas, &f->nvidia));
}

static void test_context_failure (Fixture *f, gconstpointer unused)
{
  context_ok = FALSE;
  g_autoptr (GVariant) reply = call (f, "Reclaim", f->nvidia.path);
  g_assert_true (g_hash_table_contains (f->native.gpu_datas, &f->nvidia));
  const char *outcome = NULL;
  g_assert_true (g_variant_lookup (reply, "outcome", "&s", &outcome));
  g_assert_cmpstr (outcome, ==, "cannot make the primary EGL context current");
}

static void test_shutdown (Fixture *f, gconstpointer unused)
{
  Reply reply = { 0 };
  begin (f, "Reclaim", f->nvidia.path, &reply);
  gint64 deadline = g_get_monotonic_time () + 3000000;
  while (!f->native.poc_request && g_get_monotonic_time () < deadline)
    { g_main_context_iteration (NULL, FALSE); g_usleep (1000); }
  g_assert_nonnull (f->native.poc_request);
  gpu_poc_stop (&f->native);
  finish (&reply);
  g_assert_nonnull (reply.error);
  g_assert_nonnull (strstr (reply.error->message, "shutting down"));
  g_clear_error (&reply.error);
  g_assert_true (g_hash_table_contains (f->native.gpu_datas, &f->nvidia));
}

static void test_additional_guards (Fixture *f, gconstpointer unused)
{
  f->nvidia.driver = "other-driver";
  g_autoptr (GVariant) wrong = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (wrong, "eligible"));
  f->nvidia.driver = "nvidia-drm";
  f->amd.driver = "i915";
  g_autoptr (GVariant) primary = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (primary, "eligible"));
  f->amd.driver = "amdgpu";
  f->nvidia.crtcs = g_list_append (NULL, GINT_TO_POINTER (TRUE));
  g_autoptr (GVariant) crtc = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (crtc, "eligible"));
  g_list_free (f->nvidia.crtcs); f->nvidia.crtcs = NULL;
  f->native.lingering_onscreens = g_list_append (NULL, f);
  g_autoptr (GVariant) lingering = call (f, "Reclaim", f->nvidia.path);
  g_assert_false (flag (lingering, "eligible"));
  g_list_free (f->native.lingering_onscreens); f->native.lingering_onscreens = NULL;
  g_autoptr (GVariant) unknown = call (f, "Reclaim", "/dev/dri/card999");
  g_assert_false (flag (unknown, "eligible"));
  g_assert_true (g_hash_table_contains (f->native.gpu_datas, &f->nvidia));
}

static void test_restore_context_failure (Fixture *f, gconstpointer unused)
{
  fail_context_on_call = 2;
  g_autoptr (GVariant) reply = call (f, "Reclaim", f->nvidia.path);
  const char *outcome = NULL;
  g_assert_true (g_variant_lookup (reply, "outcome", "&s", &outcome));
  g_assert_cmpstr (outcome, ==, "cache-reclaimed-primary-context-restore-failed");
}

static void test_fd_snapshot_errno (void)
{
  g_setenv ("MUTTER_GPU_RELEASE_POC_TRACE_FDS", "1", TRUE);
  g_test_expect_message (NULL, G_LOG_LEVEL_MESSAGE,
                         "*event=fd-snapshot-end stage=errno-check*matched=0 truncated=0*");
  errno = EBUSY;
  gpu_release_poc_fds ("errno-check", NULL);
  g_assert_cmpint (errno, ==, EBUSY);
  g_test_assert_expected_messages ();
  g_unsetenv ("MUTTER_GPU_RELEASE_POC_TRACE_FDS");
  errno = ENOTTY;
  gpu_release_poc_fds ("disabled-check", NULL);
  g_assert_cmpint (errno, ==, ENOTTY);
}

static void test_drm_only_policy (void)
{
  const char *pci = "0000:03:00.0";
  GObject *object = g_object_new (G_TYPE_OBJECT, NULL);

  g_assert_true (gpu_poc_internal_policy_matches ("0000:65:00.0", "0000:65:00.0", "0x10de\n", "0x2820\n", "0\n", NULL));
  g_assert_false (gpu_poc_internal_policy_matches (NULL, "0000:65:00.0", "0x10de\n", "0x2820\n", "0\n", NULL));
  g_assert_false (gpu_poc_internal_policy_matches ("0000:65:00.0", "0000:65:00.0", "0x10de\n", "0x2820\n", "1\n", NULL));
  g_assert_false (gpu_poc_internal_policy_matches ("0000:65:00.0", "0000:65:00.0", "0x10de\n", "0x2bb3\n", "0\n", NULL));
  g_assert_false (gpu_poc_internal_policy_matches ("0000:65:00.0", "0000:65:00.0", "0x10de\n", "0x2820\n", "0\n", "removable\n"));

  g_assert_true (gpu_poc_drm_policy_matches (pci, pci, "0x10de\n", "0\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches (pci, pci, "0x1002\n", "0\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches (pci, pci, "0x10de\n", "1\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches (pci, pci, "0x10de\n", "0\n", "fixed\n"));
  g_assert_false (gpu_poc_drm_policy_matches (pci, "0000:65:00.0", "0x10de\n", "0\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches (NULL, pci, "0x10de\n", "0\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches (pci, pci, NULL, "0\n", "removable\n"));
  g_assert_false (gpu_poc_drm_policy_matches ("../card0", "../card0", "0x10de\n", "0\n", "removable\n"));
  g_unsetenv ("MUTTER_GPU_RELEASE_POC");
  g_assert_false (gpu_poc_drm_policy_matches (pci, pci, "0x10de\n", "0\n", "removable\n"));
  g_setenv ("MUTTER_GPU_RELEASE_POC", "1", TRUE);
  g_assert_false (gpu_poc_drm_only (object));
  g_object_set_data (object, GPU_POC_DRM_ONLY_KEY, GINT_TO_POINTER (1));
  g_assert_true (gpu_poc_drm_only (object));
  g_object_unref (object);
}

static void test_child_environment (void)
{
  g_autofree char *old_ld = g_strdup (g_getenv ("LD_LIBRARY_PATH"));
  g_autofree char *old_data = g_strdup (g_getenv ("XDG_DATA_DIRS"));

  g_setenv ("MUTTER_GPU_RELEASE_POC", "1", TRUE);
  g_setenv ("MUTTER_GPU_RELEASE_POC_CLEAN_CHILD_ENV", "1", TRUE);
  g_setenv ("MUTTER_GPU_RELEASE_POC_ORIGINAL_DATA_DIRS", "/usr/share", TRUE);
  g_setenv ("LD_LIBRARY_PATH", "/private-experiment/lib", TRUE);
  g_setenv ("XDG_DATA_DIRS", "/private-experiment/share:/usr/share", TRUE);
  gpu_release_poc_clean_child_environment ();
  g_assert_null (g_getenv ("LD_LIBRARY_PATH"));
  g_assert_cmpstr (g_getenv ("XDG_DATA_DIRS"), ==, "/usr/share");
  g_unsetenv ("MUTTER_GPU_RELEASE_POC_CLEAN_CHILD_ENV");
  if (old_ld) g_setenv ("LD_LIBRARY_PATH", old_ld, TRUE);
  if (old_data) g_setenv ("XDG_DATA_DIRS", old_data, TRUE);
  else g_unsetenv ("XDG_DATA_DIRS");
}

int main (int argc, char **argv)
{
  MetaRendererNative disabled = { 0 };
  g_unsetenv ("MUTTER_GPU_RELEASE_POC");
  gpu_poc_start (&disabled);
  g_assert_cmpuint (disabled.poc_name_id, ==, 0);
  g_setenv ("MUTTER_GPU_RELEASE_POC", "1", TRUE);
  g_test_init (&argc, &argv, NULL);
  g_test_add_func ("/poc/fd-snapshot-preserves-errno", test_fd_snapshot_errno);
  g_test_add_func ("/poc/drm-only-policy", test_drm_only_policy);
  g_test_add_func ("/poc/child-environment", test_child_environment);
  g_test_add ("/poc/primary-protected", Fixture, NULL, setup, test_primary, teardown);
  g_test_add ("/poc/release-and-repeat", Fixture, NULL, setup, test_release, teardown);
  g_test_add ("/poc/guards", Fixture, NULL, setup, test_guards, teardown);
  g_test_add ("/poc/context-failure", Fixture, NULL, setup, test_context_failure, teardown);
  g_test_add ("/poc/shutdown-pending", Fixture, NULL, setup, test_shutdown, teardown);
  g_test_add ("/poc/additional-guards", Fixture, NULL, setup, test_additional_guards, teardown);
  g_test_add ("/poc/restore-context-failure", Fixture, NULL, setup, test_restore_context_failure, teardown);
  return g_test_run ();
}
