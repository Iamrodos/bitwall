/*
 * The decoders on Windows: whatever ffmpeg decodes, on the GPU's own
 * decoder for H.264 and HEVC where ffmpeg lists a way there among its
 * hardware accelerations: D3D11VA first, which any GPU with a Windows
 * driver offers, else Intel's Quick Sync. MJPEG is not taken, as on a
 * Mac.
 *
 * The ffmpeg is the one BITWALL_FFMPEG names, else Chocolatey's, which
 * cost a fraction of the CPU MSYS2's did decoding through D3D11VA on
 * an Intel laptop, else whichever is on the PATH. Chocolatey's is run as itself,
 * not through its shim in chocolatey\bin, so the decoder is the child
 * the painter started. Part of bitwall-paint.c, which includes it
 * (platform.h).
 */

#define CHOCO_FFMPEG "C:\\ProgramData\\chocolatey\\lib\\ffmpeg\\tools\\ffmpeg\\bin\\ffmpeg.exe"

enum { HW_NONE, HW_D3D11VA, HW_QSV };
static int hw_way;

/* Each line `ffmpeg <args>` prints, to `line`, for each of which `take`
 * is called. cmd.exe runs it, so the whole is quoted once more. */
static void ffmpeg_lines(const char *args, void (*take)(const char *line, int *dec), int *dec)
{
    char cmd[400], line[256];
    FILE *f;
    if (strchr(codecs.ffmpeg, '"'))
        return;
    snprintf(cmd, sizeof cmd, "\"\"%s\" -hide_banner %s 2>NUL\"", codecs.ffmpeg, args);
    f = _popen(cmd, "r");
    while (f && fgets(line, sizeof line, f))
        take(line, dec);
    if (f)
        _pclose(f);
}

static void decoder_line(const char *line, int *dec)
{
    char flags[16], name[64];
    if (sscanf(line, " %15s %63s", flags, name) == 2 && flags[0] == 'V') {
        if (!strcmp(name, "hevc"))
            dec[CODEC_HEVC] = 1;
        if (!strcmp(name, "h264"))
            dec[CODEC_H264] = 1;
        if (!strcmp(name, "hevc_qsv"))
            dec[0] |= 2;
        if (!strcmp(name, "h264_qsv"))
            dec[0] |= 4;
    }
}

static void hwaccel_line(const char *line, int *hw)
{
    char name[64];
    if (sscanf(line, " %63s", name) == 1) {
        if (!strcmp(name, "d3d11va"))
            hw[0] = 1;
        if (!strcmp(name, "qsv"))
            hw[1] = 1;
    }
}

static void codecs_probe(void)
{
    const char *env = getenv("BITWALL_FFMPEG");
    int dec[NCODECS] = {0, 0, 0, 0, 0}, hw[NCODECS] = {0, 0, 0, 0, 0};
    if (env && *env)
        snprintf(codecs.ffmpeg, sizeof codecs.ffmpeg, "%s", env);
    else if (GetFileAttributesA(CHOCO_FFMPEG) != INVALID_FILE_ATTRIBUTES)
        snprintf(codecs.ffmpeg, sizeof codecs.ffmpeg, "%s", CHOCO_FFMPEG);
    ffmpeg_lines("-decoders", decoder_line, dec);
    ffmpeg_lines("-hwaccels", hwaccel_line, hw);
    hw_way = hw[0] ? HW_D3D11VA : hw[1] && (dec[0] & 6) == 6 ? HW_QSV : HW_NONE;
    /* BITWALL_HWACCEL picks one, or none, to compare them. */
    if ((env = getenv("BITWALL_HWACCEL")) && *env)
        hw_way = !strcmp(env, "qsv") && hw[1] && (dec[0] & 6) == 6 ? HW_QSV
            : !strcmp(env, "d3d11va") && hw[0] ? HW_D3D11VA : HW_NONE;
    codecs.ok[CODEC_HEVC] = dec[CODEC_HEVC];
    codecs.ok[CODEC_H264] = dec[CODEC_H264];
    codecs.hw[CODEC_HEVC] = dec[CODEC_HEVC] && hw_way != HW_NONE;
    codecs.hw[CODEC_H264] = dec[CODEC_H264] && hw_way != HW_NONE;
    fprintf(stderr, "bitwall-paint: ffmpeg %s, decoding on %s\n", codecs.ffmpeg,
            hw_way == HW_D3D11VA ? "d3d11va" : hw_way == HW_QSV ? "qsv" : "the CPU");
}

/* The GPU's decoder, its frames copied back for the YUV4MPEG2 the
 * painter reads. */
static int decoder_hw(int codec, const char **argv)
{
    int n = 0;
    if ((codec != CODEC_HEVC && codec != CODEC_H264) || !codecs.hw[codec])
        return 0;
    if (hw_way == HW_D3D11VA) {
        argv[n++] = "-hwaccel";
        argv[n++] = "d3d11va";
    } else if (hw_way == HW_QSV) {
        argv[n++] = "-c:v";
        argv[n++] = codec == CODEC_HEVC ? "hevc_qsv" : "h264_qsv";
    }
    return n;
}
