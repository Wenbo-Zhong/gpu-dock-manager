/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include <glib-object.h>
#include "meta-gpu-release-poc.h"

#define GPU_POC_DRM_ONLY_KEY "gpu-release-poc-drm-only"
#define GPU_POC_HW_CURSOR_VIEW_KEY "gpu-release-poc-hw-cursor-view"

static inline gboolean
gpu_poc_drm_policy_matches (const char *requested,
                            const char *actual,
                            const char *vendor,
                            const char *boot_vga,
                            const char *removable)
{
  return gpu_release_poc_enabled () && requested &&
    g_regex_match_simple ("^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\\.[0-7]$", requested, 0, 0) &&
    g_strcmp0 (requested, actual) == 0 &&
    g_strcmp0 (vendor, "0x10de\n") == 0 &&
    g_strcmp0 (boot_vga, "0\n") == 0 &&
    g_strcmp0 (removable, "removable\n") == 0;
}

static inline gboolean
gpu_poc_drm_only_path (const char *path)
{
  const char *requested = g_getenv ("MUTTER_GPU_RELEASE_POC_DRM_ONLY_PCI");
  g_autofree char *name = NULL, *base = NULL, *link = NULL, *actual = NULL;
  g_autofree char *vendor = NULL, *boot = NULL, *removable = NULL;
  g_autofree char *vendor_path = NULL, *boot_path = NULL, *removable_path = NULL;

  if (!gpu_release_poc_enabled () || !requested || !path)
    return FALSE;
  name = g_path_get_basename (path);
  if (!g_regex_match_simple ("^(card[0-9]+|renderD[0-9]+)$", name, 0, 0))
    return FALSE;
  base = g_build_filename ("/sys/class/drm", name, "device", NULL);
  link = g_file_read_link (base, NULL);
  if (!link)
    return FALSE;
  actual = g_path_get_basename (link);
  vendor_path = g_build_filename (base, "vendor", NULL);
  boot_path = g_build_filename (base, "boot_vga", NULL);
  removable_path = g_build_filename (base, "removable", NULL);
  if (!g_file_get_contents (vendor_path, &vendor, NULL, NULL) ||
      !g_file_get_contents (boot_path, &boot, NULL, NULL) ||
      !g_file_get_contents (removable_path, &removable, NULL, NULL))
    return FALSE;
  return gpu_poc_drm_policy_matches (requested, actual, vendor, boot, removable);
}

static inline gboolean
gpu_poc_drm_only (gpointer object)
{
  return object && g_object_get_data (G_OBJECT (object), GPU_POC_DRM_ONLY_KEY) != NULL;
}
