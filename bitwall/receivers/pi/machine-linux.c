/*
 * What a Linux machine says of itself, read from /proc. Part of
 * bitwall-paint.c, which includes it (platform.h).
 */

/* The fields of /proc/self/stat are counted from the last bracket, since
 * the name in brackets may hold anything, spaces included. */
static double cpu_seconds(void)
{
    char text[1024], *p;
    unsigned long user, sys;
    size_t n;
    FILE *f = fopen("/proc/self/stat", "r");
    if (!f)
        return -1;
    n = fread(text, 1, sizeof text - 1, f);
    fclose(f);
    text[n] = 0;
    p = strrchr(text, ')');
    if (!p || sscanf(p + 1, " %*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %lu %lu",
                     &user, &sys) != 2)
        return -1;
    return (double)(user + sys) / (double)sysconf(_SC_CLK_TCK);
}

/* A DRM driver that keeps per-client statistics puts them in the fdinfo
 * of every fd open on the card, as nanoseconds per engine; all of this
 * process's fds are summed, so every wall's context counts, and a wall's
 * share cannot be told apart from another's. */
static double gpu_seconds(void)
{
    char path[64], line[160];
    double ns = 0;
    int found = 0;
    struct dirent *e;
    DIR *d = opendir("/proc/self/fdinfo");
    if (!d)
        return -1;
    while ((e = readdir(d))) {
        FILE *f;
        if (e->d_name[0] == '.')
            continue;
        snprintf(path, sizeof path, "/proc/self/fdinfo/%.40s", e->d_name);
        if (!(f = fopen(path, "r")))
            continue;
        while (fgets(line, sizeof line, f)) {
            unsigned long long v;
            if (strncmp(line, "drm-engine-", 11) == 0
                && sscanf(strchr(line, ':') ? strchr(line, ':') + 1 : "", "%llu", &v) == 1) {
                ns += (double)v;
                found = 1;
            }
        }
        fclose(f);
    }
    closedir(d);
    return found ? ns / 1e9 : -1;
}

static double uptime_seconds(void)
{
    double up = 0;
    FILE *f = fopen("/proc/uptime", "r");
    if (f) {
        if (fscanf(f, "%lf", &up) != 1)
            up = 0;
        fclose(f);
    }
    return up;
}

static long machine_booted(void)
{
    FILE *f = fopen("/proc/stat", "r");
    char line[256];
    long at = 0;
    if (!f)
        return 0;
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "btime %ld", &at) == 1)
            break;
    fclose(f);
    return at;
}
