/*
 * far_replay: replays fprintd's synaptics0078 verify decisions offline,
 * through libfprint's own code (the fork's libfprint-private.a), against a
 * stored FpPrint as fprintd keeps it on disk.
 *
 * Path reproduced, step by step:
 *   - fp_print_deserialize() of the stored file (fprintd's load path);
 *   - the probe PGM (native 144x56, as the driver dumps it) upscaled with the
 *     exact loop of build_image() in libfprint/drivers/synaptics0078.c;
 *   - fp_image_new() + fp_image_extract_sigfm_info() (the core's GTask
 *     extraction and its < 25 keypoint gate), finished with
 *     fp_image_detect_minutiae_finish() as fpi_image_device_minutiae_detected;
 *   - fpi_print_add_from_image() into a fresh FPI_PRINT_SIGFM print;
 *   - fpi_print_sigfm_match (template, print, threshold): the decision, with
 *     its own per-stage scores captured through FP_PRINT_SCORE_LOG;
 *   - additionally sigfm_match_score (stage_i, probe) for EVERY stage (the
 *     live path stops at the first stage >= threshold).
 *
 * Modes:
 *   far_replay info PRINT
 *   far_replay verify [--threshold N] PRINT IMG.pgm...
 *   far_replay stages PRINT IMG.pgm...     which image each stage came from
 *                                          (byte-identical serialized SIGFM)
 *
 * Output is numbers and file names only; it is biometric-derived, keep it
 * out of git (local/far-diag/).
 *
 * Build: research/far-diag/build_replay.sh FORK_BUILDDIR OUT
 */

#include "fpi-print.h"
#include "fpi-image.h"
#include "fp-print-private.h"
#include "fp-image.h"
#include "sigfm/sigfm.h"

#include <glib.h>
#include <glib/gstdio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NATIVE_W 144
#define NATIVE_H 56
#define UPSCALE 2

static guint8 *
read_pgm (const char *path, GError **error)
{
  g_autofree gchar *buf = NULL;
  gsize len, i = 0;
  long tok[4];
  int n = 0;

  if (!g_file_get_contents (path, &buf, &len, error))
    return NULL;
  if (len < 2 || buf[0] != 'P' || buf[1] != '5')
    goto bad;
  i = 2;
  tok[0] = 5;
  n = 1;
  while (n < 4 && i < len)
    {
      if (g_ascii_isspace (buf[i]))
        {
          i++;
          continue;
        }
      if (buf[i] == '#')
        {
          while (i < len && buf[i] != '\n')
            i++;
          continue;
        }
      tok[n++] = strtol (buf + i, NULL, 10);
      while (i < len && !g_ascii_isspace (buf[i]))
        i++;
    }
  i++; /* the single whitespace after maxval */
  if (n != 4 || tok[1] != NATIVE_W || tok[2] != NATIVE_H || tok[3] != 255 ||
      len < i + NATIVE_W * NATIVE_H)
    goto bad;
  return g_memdup2 (buf + i, NATIVE_W * NATIVE_H);

bad:
  g_set_error (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA, "%s: not a %dx%d P5 PGM", path,
               NATIVE_W, NATIVE_H);
  return NULL;
}

/* The loop of build_image() in synaptics0078.c, verbatim in effect. */
static FpImage *
build_image (const guint8 *pixels)
{
  const gint width = NATIVE_W * UPSCALE;
  const gint height = NATIVE_H * UPSCALE;
  FpImage *img = fp_image_new (width, height);
  gint x, y;

  for (y = 0; y < height; y++)
    {
      const guint8 *src = pixels + (y / UPSCALE) * NATIVE_W;
      guint8 *dst = img->data + y * width;

      for (x = 0; x < width; x++)
        dst[x] = src[x / UPSCALE];
    }
  return img;
}

typedef struct
{
  GMainLoop *loop;
  gboolean   ok;
  GError    *error;
} ExtractWait;

static void
extracted_cb (GObject *source, GAsyncResult *res, gpointer user_data)
{
  ExtractWait *w = user_data;

  w->ok = fp_image_detect_minutiae_finish (FP_IMAGE (source), res, &w->error);
  g_main_loop_quit (w->loop);
}

/* A probe print from a PGM, through the core's extraction; NULL if gated. */
static FpPrint *
probe_print (const char *path, int *n_keypoints, GError **error)
{
  g_autofree guint8 *px = read_pgm (path, error);
  g_autoptr(FpImage) img = NULL;
  g_autoptr(GMainLoop) loop = NULL;
  ExtractWait w = { 0 };
  FpPrint *print;

  *n_keypoints = -1;
  if (!px)
    return NULL;
  img = build_image (px);
  loop = g_main_loop_new (NULL, FALSE);
  w.loop = loop;
  fp_image_extract_sigfm_info (img, NULL, extracted_cb, &w);
  g_main_loop_run (loop);
  if (fp_image_get_sigfm_info (img))
    *n_keypoints = sigfm_keypoints_count (fp_image_get_sigfm_info (img));
  if (!w.ok)
    {
      g_propagate_error (error, w.error);
      return NULL;
    }
  print = g_object_new (FP_TYPE_PRINT, "driver", "synaptics0078", "device-id", "replay", NULL);
  g_object_ref_sink (print);
  fpi_print_set_type (print, FPI_PRINT_SIGFM);
  if (!fpi_print_add_from_image (print, img, error))
    {
      g_object_unref (print);
      return NULL;
    }
  return print;
}

static FpPrint *
load_print (const char *path)
{
  g_autofree gchar *buf = NULL;
  gsize len;
  g_autoptr(GError) error = NULL;
  FpPrint *p;

  if (!g_file_get_contents (path, &buf, &len, &error))
    {
      fprintf (stderr, "%s: %s\n", path, error->message);
      exit (2);
    }
  p = fp_print_deserialize ((const guchar *) buf, len, &error);
  if (!p)
    {
      fprintf (stderr, "%s: deserialize: %s\n", path, error->message);
      exit (2);
    }
  return p;
}

static int
mode_info (FpPrint *t)
{
  printf ("driver\t%s\n", fp_print_get_driver (t));
  printf ("device_id\t%s\n", fp_print_get_device_id (t));
  printf ("finger\t%d\n", fp_print_get_finger (t));
  printf ("type\t%d\n", t->type);
  printf ("stages\t%u\n", t->prints->len);
  for (guint i = 0; i < t->prints->len; i++)
    printf ("stage\t%u\tkeypoints\t%d\n", i,
            sigfm_keypoints_count (g_ptr_array_index (t->prints, i)));
  return 0;
}

static int
mode_verify (FpPrint *t, int threshold, int n, char **imgs)
{
  g_autofree gchar *logpath = g_build_filename (g_get_tmp_dir (), "far_replay_scorelog.XXXXXX", NULL);
  int fd = g_mkstemp (logpath);

  if (fd < 0)
    return 2;
  close (fd);
  g_setenv ("FP_PRINT_SCORE_LOG", logpath, TRUE);

  /* image  keypoints  decision  live_scores(as logged by fpi_print_sigfm_match)  all_stage_scores */
  printf ("image\tkeypoints\tdecision\tlive_scores\tall_scores\n");
  for (int k = 0; k < n; k++)
    {
      g_autoptr(GError) error = NULL;
      g_autoptr(FpPrint) p = NULL;
      g_autoptr(GString) live = g_string_new (NULL);
      g_autoptr(GString) all = g_string_new (NULL);
      g_autofree gchar *logged = NULL;
      int nkp;
      FpiMatchResult r;
      SigfmImgInfo *probe;

      p = probe_print (imgs[k], &nkp, &error);
      if (!p)
        {
          printf ("%s\t%d\tgated(%s)\t-\t-\n", imgs[k], nkp, error ? error->message : "?");
          continue;
        }
      g_assert_cmpint (truncate (logpath, 0), ==, 0);
      r = fpi_print_sigfm_match (t, p, threshold, &error);
      g_file_get_contents (logpath, &logged, NULL, NULL);
      {
        g_auto(GStrv) lines = g_strsplit (logged ? logged : "", "\n", -1);

        for (gchar **it = lines; *it; it++)
          {
            int idx, score, th;
            if (sscanf (*it, "sigfm %d %d %d", &idx, &score, &th) == 3)
              g_string_append_printf (live, "%s%d", live->len ? " " : "", score);
          }
      }
      probe = g_ptr_array_index (p->prints, 0);
      for (guint i = 0; i < t->prints->len; i++)
        g_string_append_printf (all, "%s%d", i ? " " : "",
                                sigfm_match_score (g_ptr_array_index (t->prints, i), probe));
      printf ("%s\t%d\t%s\t%s\t%s\n", imgs[k], nkp,
              r == FPI_MATCH_SUCCESS ? "match" : r == FPI_MATCH_FAIL ? "no-match" : "error",
              live->str, all->str);
      fflush (stdout);
    }
  g_unlink (logpath);
  return 0;
}

static int
mode_stages (FpPrint *t, int n, char **imgs)
{
  g_autoptr(GPtrArray) blobs = g_ptr_array_new_with_free_func (g_free);
  g_autoptr(GArray) lens = g_array_new (FALSE, FALSE, sizeof (int));

  for (int k = 0; k < n; k++)
    {
      g_autoptr(GError) error = NULL;
      g_autoptr(FpPrint) p = NULL;
      int nkp, len = 0;
      unsigned char *b = NULL;

      p = probe_print (imgs[k], &nkp, &error);
      if (p)
        b = sigfm_serialize_binary (g_ptr_array_index (p->prints, 0), &len);
      g_ptr_array_add (blobs, b);
      g_array_append_val (lens, len);
    }
  printf ("stage\tkeypoints\tsource_image\n");
  for (guint i = 0; i < t->prints->len; i++)
    {
      int slen;
      unsigned char *s = sigfm_serialize_binary (g_ptr_array_index (t->prints, i), &slen);
      GString *hits = g_string_new (NULL);

      for (int k = 0; k < n; k++)
        if (g_ptr_array_index (blobs, k) && g_array_index (lens, int, k) == slen &&
            memcmp (g_ptr_array_index (blobs, k), s, slen) == 0)
          g_string_append_printf (hits, "%s%s", hits->len ? "," : "", imgs[k]);
      printf ("%u\t%d\t%s\n", i, sigfm_keypoints_count (g_ptr_array_index (t->prints, i)),
              hits->len ? hits->str : "NONE");
      g_string_free (hits, TRUE);
      free (s);
    }
  return 0;
}

int
main (int argc, char **argv)
{
  int threshold = 105;
  int a = 1;
  const char *mode;
  g_autoptr(FpPrint) t = NULL;

  if (argc < 3)
    {
      fprintf (stderr, "usage: far_replay info|verify|stages [--threshold N] PRINT [IMG.pgm...]\n");
      return 2;
    }
  mode = argv[a++];
  if (strcmp (argv[a], "--threshold") == 0 && a + 1 < argc)
    {
      threshold = atoi (argv[a + 1]);
      a += 2;
    }
  t = load_print (argv[a++]);
  if (t->type != FPI_PRINT_SIGFM)
    {
      fprintf (stderr, "print type %d is not SIGFM\n", t->type);
      return 2;
    }
  if (strcmp (mode, "info") == 0)
    return mode_info (t);
  if (strcmp (mode, "verify") == 0)
    return mode_verify (t, threshold, argc - a, argv + a);
  if (strcmp (mode, "stages") == 0)
    return mode_stages (t, argc - a, argv + a);
  fprintf (stderr, "unknown mode %s\n", mode);
  return 2;
}
