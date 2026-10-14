/*
 * What a Mac says of itself: the painter's CPU time from getrusage and
 * when the machine started from the kernel's boot time. A Mac says
 * nothing of one process's GPU time. Part of bitwall-paint.c, which
 * includes it (platform.h).
 */
#include <sys/resource.h>
#include <sys/sysctl.h>

static double cpu_seconds(void)
{
    struct rusage u;
    if (getrusage(RUSAGE_SELF, &u) < 0)
        return -1;
    return (double)u.ru_utime.tv_sec + u.ru_utime.tv_usec / 1e6
           + (double)u.ru_stime.tv_sec + u.ru_stime.tv_usec / 1e6;
}

static double gpu_seconds(void)
{
    return -1;
}

static long machine_booted(void)
{
    struct timeval at;
    size_t len = sizeof at;
    int mib[2] = {CTL_KERN, KERN_BOOTTIME};
    if (sysctl(mib, 2, &at, &len, NULL, 0) < 0 || at.tv_sec <= 0)
        return 0;
    return (long)at.tv_sec;
}

static double uptime_seconds(void)
{
    long at = machine_booted();
    return at > 0 ? difftime(time(NULL), (time_t)at) : 0;
}
