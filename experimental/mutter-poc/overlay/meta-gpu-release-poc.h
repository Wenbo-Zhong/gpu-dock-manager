/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once

#include <glib.h>
#include <errno.h>

static inline gboolean
gpu_release_poc_enabled (void)
{
  return g_strcmp0 (g_getenv ("MUTTER_GPU_RELEASE_POC"), "1") == 0;
}

static inline gboolean
gpu_release_poc_trace_device (const char *path)
{
  const char *filter = g_getenv ("MUTTER_GPU_RELEASE_POC_TRACE_DEVICE");

  return gpu_release_poc_enabled () && filter &&
    g_strcmp0 (filter, path) == 0;
}

#define GPU_POC_LOG(...) G_STMT_START { \
  if (gpu_release_poc_enabled ()) \
    g_message ("GPU-RELEASE-POC " __VA_ARGS__); \
} G_STMT_END

/* Low-frequency lifecycle boundaries only; never called from frame dispatch.
 * These are non-atomic observations. fd numbers can be reused or changed by
 * another thread; they are evidence, not ownership or safe-eject decisions.
 */
static inline void
gpu_release_poc_fds (const char *stage,
                     const void *object)
{
  int saved_errno;
  g_autoptr (GDir) directory = NULL;
  const char *name;
  guint scanned = 0, matched = 0;

  if (!gpu_release_poc_enabled () ||
      g_strcmp0 (g_getenv ("MUTTER_GPU_RELEASE_POC_TRACE_FDS"), "1") != 0)
    return;
  saved_errno = errno;
  directory = g_dir_open ("/proc/self/fd", 0, NULL);
  if (!directory)
    {
      GPU_POC_LOG ("event=fd-snapshot-unavailable stage=%s object=%p", stage, object);
      errno = saved_errno;
      return;
    }
  while ((name = g_dir_read_name (directory)) && scanned < 4096)
    {
      g_autofree char *path = g_build_filename ("/proc/self/fd", name, NULL);
      g_autofree char *target = g_file_read_link (path, NULL);

      scanned++;
      if (target && (g_str_has_prefix (target, "/dev/nvidia") ||
                     g_str_has_prefix (target, "/dev/dri/")))
        {
          matched++;
          GPU_POC_LOG ("event=fd-snapshot stage=%s object=%p thread=%p time_us=%" G_GINT64_FORMAT " fd=%s node=%s",
                       stage, object, g_thread_self (), g_get_monotonic_time (), name, target);
        }
    }
  GPU_POC_LOG ("event=fd-snapshot-end stage=%s object=%p matched=%u truncated=%d",
               stage, object, matched, name != NULL);
  g_clear_pointer (&directory, g_dir_close);
  errno = saved_errno;
}
