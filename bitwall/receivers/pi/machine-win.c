/*
 * What Windows says of itself: the painter's CPU time from
 * GetProcessTimes, and how long the machine has been up from
 * GetTickCount64. It says nothing of one process's GPU time here. Part of
 * bitwall-paint.c, which includes it (platform.h).
 */

static double cpu_seconds(void)
{
    FILETIME made, ended, kernel, user;
    ULARGE_INTEGER k, u;
    if (!GetProcessTimes(GetCurrentProcess(), &made, &ended, &kernel, &user))
        return -1;
    k.LowPart = kernel.dwLowDateTime;
    k.HighPart = kernel.dwHighDateTime;
    u.LowPart = user.dwLowDateTime;
    u.HighPart = user.dwHighDateTime;
    return (double)(k.QuadPart + u.QuadPart) / 1e7;
}

static double gpu_seconds(void)
{
    return -1;
}

static double uptime_seconds(void)
{
    return (double)GetTickCount64() / 1000.0;
}

static long machine_booted(void)
{
    return (long)(time(NULL) - (time_t)(GetTickCount64() / 1000));
}
