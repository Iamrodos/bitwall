/*
 * The little of POSIX the compositor uses that Windows lacks, under
 * BITWALL_WIN, included by bitwall-paint.c in place of the POSIX network,
 * directory and poll headers. The pipes are not here: they are a seam of
 * their own (ipc-win.c).
 */
#ifndef BITWALL_COMPAT_WIN_H
#define BITWALL_COMPAT_WIN_H

#define WIN32_LEAN_AND_MEAN
/* windef.h names a type POINTS and wingdi near and far, which the
 * compositor uses for its own. */
#define POINTS POINTS_win
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <iphlpapi.h>
#undef POINTS
#undef near
#undef far
#include <stdlib.h>
#include <string.h>

/* setenv, for SDL_VIDEODRIVER. */
static int setenv(const char *name, const char *value, int overwrite)
{
    if (!overwrite && getenv(name))
        return 0;
    return _putenv_s(name, value) == 0 ? 0 : -1;
}

/* getifaddrs, as far as the status page asks it: each adapter that is up
 * and its IPv4 addresses, loopback marked as such. */
struct ifaddrs {
    struct ifaddrs *ifa_next;
    struct sockaddr *ifa_addr;
    unsigned ifa_flags;
    struct sockaddr_in in;
};

static void freeifaddrs(struct ifaddrs *list)
{
    while (list) {
        struct ifaddrs *next = list->ifa_next;
        free(list);
        list = next;
    }
}

static int getifaddrs(struct ifaddrs **list)
{
    ULONG size = 16384;
    IP_ADAPTER_ADDRESSES *all = NULL, *a;
    struct ifaddrs **tail = list;
    ULONG r = ERROR_BUFFER_OVERFLOW;
    int tries;
    *list = NULL;
    for (tries = 0; tries < 3 && r == ERROR_BUFFER_OVERFLOW; tries++) {
        free(all);
        if (!(all = malloc(size)))
            return -1;
        r = GetAdaptersAddresses(AF_INET, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
                                 | GAA_FLAG_SKIP_DNS_SERVER, NULL, all, &size);
    }
    if (r != NO_ERROR) {
        free(all);
        return -1;
    }
    for (a = all; a; a = a->Next) {
        IP_ADAPTER_UNICAST_ADDRESS *u;
        if (a->OperStatus != IfOperStatusUp)
            continue;
        for (u = a->FirstUnicastAddress; u; u = u->Next) {
            struct ifaddrs *f;
            if (u->Address.lpSockaddr->sa_family != AF_INET)
                continue;
            if (!(f = calloc(1, sizeof *f)))
                break;
            memcpy(&f->in, u->Address.lpSockaddr, sizeof f->in);
            f->ifa_addr = (struct sockaddr *)&f->in;
            f->ifa_flags = a->IfType == IF_TYPE_SOFTWARE_LOOPBACK ? 0x8 : 0;
            *tail = f;
            tail = &f->ifa_next;
        }
    }
    free(all);
    return 0;
}

#endif
