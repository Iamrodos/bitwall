/*
 * The decoders on a Pi: which codecs the board decodes in hardware, read
 * from its V4L2 devices, which ffmpeg decodes at all, from its own list,
 * and how ffmpeg is put on the board's decoder for each. Part of
 * bitwall-paint.c, which includes it (platform.h).
 */
#ifdef __linux__
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#endif

/* Whether the first line of the file at `path` has `word` in it. */
static int name_has(const char *path, const char *word)
{
    char text[128] = "";
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    if (!fgets(text, sizeof text, f))
        text[0] = 0;
    fclose(f);
    return strstr(text, word) != NULL;
}

/* Whether the V4L2 device `dev` decodes JPEG: a memory-to-memory decoder
 * takes its stream on its output queue, so MJPEG or JPEG among that
 * queue's formats. */
static int v4l2_decodes_jpeg(const char *dev)
{
#ifdef __linux__
    static const enum v4l2_buf_type queue[] = {V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
                                               V4L2_BUF_TYPE_VIDEO_OUTPUT};
    int fd = open(dev, O_RDWR | O_NONBLOCK | O_CLOEXEC), found = 0;
    size_t q;
    if (fd < 0)
        return 0;
    for (q = 0; q < sizeof queue / sizeof queue[0] && !found; q++) {
        struct v4l2_fmtdesc f;
        memset(&f, 0, sizeof f);
        f.type = queue[q];
        for (f.index = 0; !found && ioctl(fd, VIDIOC_ENUM_FMT, &f) == 0; f.index++)
            found = f.pixelformat == V4L2_PIX_FMT_MJPEG || f.pixelformat == V4L2_PIX_FMT_JPEG;
    }
    close(fd);
    return found;
#else
    (void)dev;
    return 0;
#endif
}

static void codecs_probe(void)
{
    const char *env = getenv("BITWALL_FFMPEG");
    char cmd[300], line[256];
    int dec[NCODECS] = {0, 0, 0, 0, 0}, pi = name_has("/proc/device-tree/model", "Raspberry Pi");
    DIR *d = opendir("/sys/class/video4linux");
    FILE *f;
    if (env && *env)
        snprintf(codecs.ffmpeg, sizeof codecs.ffmpeg, "%s", env);
    while (d) {
        struct dirent *e = readdir(d);
        char path[320];
        if (!e)
            break;
        if (e->d_name[0] == '.')
            continue;
        snprintf(path, sizeof path, "/sys/class/video4linux/%s/name", e->d_name);
        if (name_has(path, "hevc-dec"))
            codecs.hw[CODEC_HEVC] = 1;
        if (name_has(path, "codec-decode"))
            codecs.hw[CODEC_H264] = 1;
        if (name_has(path, "dec")) {
            char dev[300];
            snprintf(dev, sizeof dev, "/dev/%s", e->d_name);
            if (v4l2_decodes_jpeg(dev))
                codecs.hw[CODEC_MJPEG] = 1;
        }
    }
    if (d)
        closedir(d);
    snprintf(cmd, sizeof cmd, "'%s' -hide_banner -decoders 2>/dev/null", codecs.ffmpeg);
    f = strchr(codecs.ffmpeg, '\'') ? NULL : popen(cmd, "r");
    while (f && fgets(line, sizeof line, f)) {
        char flags[16], name[64];
        if (sscanf(line, " %15s %63s", flags, name) == 2 && flags[0] == 'V') {
            if (!strcmp(name, "hevc"))
                dec[CODEC_HEVC] = 1;
            if (!strcmp(name, "h264"))
                dec[CODEC_H264] = 1;
            if (!strcmp(name, "mjpeg_v4l2m2m"))
                dec[CODEC_MJPEG] = 1;
        }
    }
    if (f)
        pclose(f);
    codecs.ok[CODEC_HEVC] = dec[CODEC_HEVC] && (codecs.hw[CODEC_HEVC] || !pi);
    codecs.ok[CODEC_H264] = dec[CODEC_H264]
                            && (codecs.hw[CODEC_H264] || codecs.hw[CODEC_HEVC] || !pi);
    codecs.ok[CODEC_MJPEG] = dec[CODEC_MJPEG] && codecs.hw[CODEC_MJPEG];
}

/* HEVC through the DRM hwaccel, which is the Pi 5's stateless decoder;
 * H.264 and MJPEG through ffmpeg's V4L2 memory-to-memory decoders, each
 * only where the board has the decoder. */
static int decoder_hw(int codec, const char **argv)
{
    int n = 0;
    if (codec == CODEC_HEVC && codecs.hw[CODEC_HEVC]) {
        argv[n++] = "-hwaccel";
        argv[n++] = "drm";
    }
    if (codec == CODEC_H264 && codecs.hw[CODEC_H264]) {
        argv[n++] = "-c:v";
        argv[n++] = "h264_v4l2m2m";
    }
    if (codec == CODEC_MJPEG && codecs.hw[CODEC_MJPEG]) {
        argv[n++] = "-c:v";
        argv[n++] = "mjpeg_v4l2m2m";
    }
    return n;
}
