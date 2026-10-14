/*
 * A wall's pipes on a Pi, a Mac or Linux: each a fifo at its path. The
 * door, a session's `in`, `out` and `data`, a glass stream, and the
 * wakers the painter's threads wait on beside them, as PROTOCOL.md has
 * them; an end is the fifo's descriptor, and every call on it is the one
 * the painter made before there was a Windows. Part of bitwall-paint.c,
 * which includes it (platform.h).
 */

/* -- waiting ------------------------------------------------------------- */

/* A pipe for ringing a thread that waits in select: the read end is
 * waited on with whatever the thread is reading, and a byte down the
 * write end ends the wait at once. Neither end reaches a child. */
static int waker_open(int p[2])
{
    int i;
    p[0] = p[1] = -1;
    if (pipe(p) < 0)
        return -1;
    for (i = 0; i < 2; i++) {
        fcntl(p[i], F_SETFD, FD_CLOEXEC);
        fcntl(p[i], F_SETFL, fcntl(p[i], F_GETFL) | O_NONBLOCK);
    }
    return 0;
}

static void waker_ring(const int p[2])
{
    if (p[1] >= 0 && write(p[1], "", 1) < 0) {
        /* full already: it has been rung */
    }
}

static void waker_close(int p[2])
{
    int i;
    for (i = 0; i < 2; i++)
        if (p[i] >= 0) {
            close(p[i]);
            p[i] = -1;
        }
}

/* Wait up to `ms` for `fd` to be readable or the waker to ring: 1 when
 * `fd` is readable, 0 when it is not (the wait ran out, or the waker
 * rang), and -1 on an error, errno set. */
static int wait_readable(int fd, int waker_fd, int ms)
{
    fd_set rd;
    struct timeval tv = {ms / 1000, (ms % 1000) * 1000};
    int sr, top = fd > waker_fd ? fd : waker_fd;
    FD_ZERO(&rd);
    if (fd >= 0)
        FD_SET(fd, &rd);
    if (waker_fd >= 0)
        FD_SET(waker_fd, &rd);
    /* select, not poll: on a fifo whose writer has gone, macOS marks it
     * readable through select but not through poll, and that end of file
     * is the writer leaving that ends a session. */
    sr = select(top + 1, &rd, NULL, NULL, &tv);
    if (sr <= 0)
        return sr;
    return fd >= 0 && FD_ISSET(fd, &rd);
}

/* Wait up to `ms` for `fd` to take bytes or the waker to ring: as
 * wait_readable, for a writer. */
static int wait_writable(int fd, int waker_fd, int ms)
{
    fd_set rd, wr;
    struct timeval tv = {ms / 1000, (ms % 1000) * 1000};
    int sr, top = fd > waker_fd ? fd : waker_fd;
    FD_ZERO(&rd);
    FD_ZERO(&wr);
    FD_SET(fd, &wr);
    if (waker_fd >= 0)
        FD_SET(waker_fd, &rd);
    sr = select(top + 1, &rd, &wr, NULL, &tv);
    if (sr <= 0)
        return sr;
    return FD_ISSET(fd, &wr);
}

static void waker_drain(const int p[2])
{
    char b[64];
    while (p[0] >= 0 && read(p[0], b, sizeof b) > 0) {
    }
}

/* -- names, files, claiming ---------------------------------------------- */

/* Whether the fifo at `path` has a reader: a live wall's door. Opening it
 * for writing without blocking fails at once when nothing reads it. */
static int door_has_reader(const char *path)
{
    int fd = open(path, O_WRONLY | O_NONBLOCK);
    if (fd < 0)
        return 0;               /* ENXIO: nobody reading; ENOENT: no door */
    close(fd);
    return 1;
}

/* A whole-file lock of fcntl's, taken (F_WRLCK), waited for, or let go
 * (F_UNLCK). Held by the process, and gone with it however it ends. */
static int file_lock(int fd, short type)
{
    struct flock l;
    memset(&l, 0, sizeof l);
    l.l_type = type;
    l.l_whence = SEEK_SET;
    while (fcntl(fd, F_SETLKW, &l) < 0)
        if (errno != EINTR)
            return -1;
    return 0;
}

/* A directory and everything under it, gone. */
static void rmrf(const char *path)
{
    DIR *d = opendir(path);
    struct dirent *e;
    char child[1024];
    if (d) {
        while ((e = readdir(d))) {
            if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
                continue;
            snprintf(child, sizeof child, "%s/%s", path, e->d_name);
            rmrf(child);
        }
        closedir(d);
    }
    if (unlink(path) < 0)
        rmdir(path);
}

/* The wall `dir` claimed: built under a `.` name beside it, a wall a
 * killed receiver left taken over, and renamed into place. Its door,
 * held O_RDWR so it never ends; -1, the reason said, when a live
 * receiver holds the wall or it cannot be made. */
static int ipc_claim(const char *run, const char *name, const char *dir)
{
    char walls[600], tmp[700], path[800], door[700];
    int fd, lock;

    snprintf(walls, sizeof walls, "%s/bitwall", run);
    if (mkdir(walls, 0700) < 0 && errno != EEXIST) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", walls, strerror(errno));
        return -1;
    }
    snprintf(tmp, sizeof tmp, "%s/.%s-%d-%05x", walls, name, (int)getpid(),
             (unsigned)(rand() & 0xfffff));
    if (mkdir(tmp, 0700) < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", tmp, strerror(errno));
        return -1;
    }
    snprintf(path, sizeof path, "%s/s", tmp);
    mkdir(path, 0700);
    snprintf(path, sizeof path, "%s/door.in", tmp);
    if (mkfifo(path, 0600) < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", path, strerror(errno));
        rmrf(tmp);
        return -1;
    }
    fd = open(path, O_RDWR | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", path, strerror(errno));
        rmrf(tmp);
        return -1;
    }
    /* Whether the wall is free, and taking it, are one step under a lock
     * of the name's own: two receivers finding one stale wall would
     * otherwise each clear it, and the second clear the first's new one.
     * The lock is beside the last-seen note, outside bitwall/, and is
     * left there, so that nobody ever locks a file another has removed. */
    snprintf(path, sizeof path, "%s/bitwall-%s.claim", run, name);
    lock = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (lock < 0 || file_lock(lock, F_WRLCK) < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", path, strerror(errno));
        if (lock >= 0)
            close(lock);
        close(fd);
        rmrf(tmp);
        return -1;
    }
    if (access(dir, F_OK) == 0) {
        snprintf(door, sizeof door, "%s/door.in", dir);
        if (door_has_reader(door)) {
            fprintf(stderr, "bitwall-paint: %s is held by a live receiver\n", dir);
            close(lock);
            close(fd);
            rmrf(tmp);
            return -1;
        }
        rmrf(dir);              /* a wall a killed receiver left, reclaimed */
    }
    if (rename(tmp, dir) < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", dir, strerror(errno));
        close(lock);
        close(fd);
        rmrf(tmp);
        return -1;
    }
    close(lock);                /* which lets the lock go */
    return fd;
}

/* The painter's own notes, the last-seen and the claim's lock, are kept
 * in the run directory, beside bitwall/. */
static const char *ipc_notes(const char *run)
{
    return run;
}

/* -- a session's pipes --------------------------------------------------- */

/* A session's directory, made: -1 when the name is in use. */
static int ipc_session_make(const char *dir)
{
    if (access(dir, F_OK) == 0)
        return -1;
    return mkdir(dir, 0700) < 0 ? -1 : 0;
}

static void ipc_in_make(const char *path)
{
    mkfifo(path, 0600);
}

/* `out`, held O_RDWR so a write never blocks and what is said before a
 * reader comes waits in the pipe, with as much room as Linux gives. */
static int ipc_out_make(const char *path)
{
    int fd;
    mkfifo(path, 0600);
    fd = open(path, O_RDWR | O_NONBLOCK);
#ifdef F_SETPIPE_SZ
    if (fd >= 0) {
        int want = 1 << 20;
        while (want > (1 << 16) && fcntl(fd, F_SETPIPE_SZ, want) < 0)
            want >>= 1;
    }
#endif
    return fd;
}

/* `in`, opened without waiting for its writer; read blocking after that
 * unless `nonblocking`. */
static int ipc_in_open(const char *path, int nonblocking)
{
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd >= 0 && !nonblocking) {
        int fl = fcntl(fd, F_GETFL);
        if (fl >= 0)
            fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);
    }
    return fd;
}

static int ipc_data_make(const char *path)
{
    return mkfifo(path, 0600) < 0 && errno != EEXIST ? -1 : 0;
}

/* `data` for the next stream, opened without waiting for a writer. */
static int ipc_data_open(const char *path)
{
    return open(path, O_RDONLY | O_NONBLOCK);
}

static int ipc_glass_make(const char *path)
{
    return mkfifo(path, 0600) < 0 && errno != EEXIST ? -1 : 0;
}

/* A glass stream's fifo opened to write once a reader has it: -1 at once
 * while nobody reads it. */
static int ipc_glass_open(const char *path)
{
    int fd = open(path, O_WRONLY | O_NONBLOCK);
    if (fd < 0)
        return -1;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
#ifdef F_SETPIPE_SZ
    {
        int want = 1 << 20;
        while (want > (1 << 16) && fcntl(fd, F_SETPIPE_SZ, want) < 0)
            want >>= 1;
    }
#endif
    return fd;
}

static void ipc_glass_remove(const char *path)
{
    unlink(path);
}

static int ipc_stdin(void)
{
    return 0;
}

static ssize_t ipc_read(int end, void *buf, size_t n)
{
    return read(end, buf, n);
}

static ssize_t ipc_write(int end, const void *buf, size_t n)
{
    return write(end, buf, n);
}

/* Whether `out` still holds bytes its reader has not taken. */
static int ipc_held(int out)
{
    struct pollfd p = {out, POLLIN, 0};
    return out >= 0 && poll(&p, 1, 0) > 0 && (p.revents & POLLIN);
}

static void ipc_close(int end)
{
    close(end);
}

static void ipc_remove(const char *dir)
{
    rmrf(dir);
}

