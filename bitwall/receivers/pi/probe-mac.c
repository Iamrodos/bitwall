/*
 * The decoders on a Mac: whatever ffmpeg decodes, and VideoToolbox, the
 * Mac's own decoder, for H.264 and HEVC where ffmpeg lists it among its
 * hardware accelerations. MJPEG is not taken: VideoToolbox does not
 * decode it, and a wall takes MJPEG only where the board does. Part of
 * bitwall-paint.c, which includes it (platform.h).
 */

/* Each line `ffmpeg <args>` prints, to `line`, for each of which `take`
 * is called. */
static void ffmpeg_lines(const char *args, void (*take)(const char *line, int *dec), int *dec)
{
    char cmd[300], line[256];
    FILE *f;
    if (strchr(codecs.ffmpeg, '\''))
        return;
    snprintf(cmd, sizeof cmd, "'%s' -hide_banner %s 2>/dev/null", codecs.ffmpeg, args);
    f = popen(cmd, "r");
    while (f && fgets(line, sizeof line, f))
        take(line, dec);
    if (f)
        pclose(f);
}

static void decoder_line(const char *line, int *dec)
{
    char flags[16], name[64];
    if (sscanf(line, " %15s %63s", flags, name) == 2 && flags[0] == 'V') {
        if (!strcmp(name, "hevc"))
            dec[CODEC_HEVC] = 1;
        if (!strcmp(name, "h264"))
            dec[CODEC_H264] = 1;
    }
}

static void hwaccel_line(const char *line, int *dec)
{
    char name[64];
    if (sscanf(line, " %63s", name) == 1 && !strcmp(name, "videotoolbox"))
        dec[0] = 1;
}

static void codecs_probe(void)
{
    const char *env = getenv("BITWALL_FFMPEG");
    int dec[NCODECS] = {0, 0, 0, 0, 0}, vt[1] = {0};
    if (env && *env)
        snprintf(codecs.ffmpeg, sizeof codecs.ffmpeg, "%s", env);
    ffmpeg_lines("-decoders", decoder_line, dec);
    ffmpeg_lines("-hwaccels", hwaccel_line, vt);
    codecs.ok[CODEC_HEVC] = dec[CODEC_HEVC];
    codecs.ok[CODEC_H264] = dec[CODEC_H264];
    codecs.hw[CODEC_HEVC] = dec[CODEC_HEVC] && vt[0];
    codecs.hw[CODEC_H264] = dec[CODEC_H264] && vt[0];
}

/* VideoToolbox, its frames copied back for the YUV4MPEG2 the painter
 * reads. ffmpeg decodes in software what VideoToolbox will not take. */
static int decoder_hw(int codec, const char **argv)
{
    int n = 0;
    if ((codec == CODEC_HEVC || codec == CODEC_H264) && codecs.hw[codec]) {
        argv[n++] = "-hwaccel";
        argv[n++] = "videotoolbox";
    }
    return n;
}
