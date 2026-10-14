/*
 * A wall's pipes on Windows: each fifo a named pipe at the same path,
 * under a run directory of `//./pipe/bitwall-<user>` (WINDOWS.md), which
 * Windows takes as the pipe named by the rest of the path. bitwall/local.py
 * is the same mapping for Python, and a caller of either cannot tell
 * which wall it reached.
 *
 * An end is the painter's own number for a pipe it made, an index into
 * the table here, since a pipe is a handle and an overlapped call on it,
 * not a descriptor. Every pipe is made overlapped and waited on through
 * its event, so a thread waits on a pipe and its waker at once as one
 * waits on a fifo and a pipe in select; the loop's reads look and do not
 * wait. Each is made PIPE_REJECT_REMOTE_CLIENTS, and with Windows' default
 * security, so only this machine, its user and its administrators reach
 * it.
 *
 *   the door      several instances, so callers at once each have one; a
 *                 request is handed on whole when its caller closes, so
 *                 requests never mix. The first is made first of its
 *                 name, and that is the claim: it fails while a live
 *                 receiver holds the wall, and a receiver that dies takes
 *                 its pipes with it.
 *   in, data      read a piece at a time, the next not asked for until
 *                 the last is taken, so a wall that stops reading fills
 *                 the pipe and its writer waits; end of file when the
 *                 writer goes. `data` stays for the next writer.
 *   out           written without waiting (PIPE_NOWAIT): what does not
 *                 fit, or comes before the reader, stays in the
 *                 session's queue.
 *   glass         written overlapped, so its writer waits on the reader
 *                 and its waker at once; it stays for the next reader.
 *
 * Part of bitwall-paint.c, which includes it (platform.h).
 */

#ifndef PIPE_REJECT_REMOTE_CLIENTS
#define PIPE_REJECT_REMOTE_CLIENTS 0x8
#endif

/* What a pipe the wall reads asks for at once, and the room it gives a
 * writer: what F_SETPIPE_SZ gives a fifo on a Pi. */
#define PIPE_PIECE (1 << 20)
#define PIPE_ROOM (1 << 20)
/* Callers at the door at once; a fifth waits for an instance. */
#define DOOR_INSTANCES 4
/* The most of one request kept: a request is a line. */
#define DOOR_MAX 4096
/* Ends at once: a wall's door, three a session, a waker a thread. */
#define ENDS 1024

enum { E_FREE, E_DOOR, E_IN, E_DATA, E_OUT, E_GLASS, E_WAKER, E_STDIN };
enum { P_NONE, P_CONNECT, P_READ, P_WRITE };

/* One instance of a named pipe and the overlapped call under way on it. */
typedef struct {
    HANDLE h;
    OVERLAPPED ov;          /* its event manual-reset, set when the call ends */
    int pending;            /* P_CONNECT, P_READ or P_WRITE under way */
    int connected;
    int eof;                /* its writer has gone, or its reader */
    uint8_t *buf;           /* a read's bytes, `off` of `got` taken */
    DWORD cap, got, off;
    char req[DOOR_MAX];     /* a door's request so far */
    size_t nreq;
} Pipe;

typedef struct {
    int kind;
    int held;               /* handed out, and not let go */
    char path[720];
    Pipe p[DOOR_INSTANCES]; /* one, but a door's several */
    int n;
    HANDLE ev;              /* a waker's */
    char *done;             /* a door's whole requests, not yet read */
    size_t ndone;
} End;

static End *ends[ENDS];
static CRITICAL_SECTION ends_mu;
static volatile LONG ends_up;

static void ends_lock(void)
{
    if (InterlockedCompareExchange(&ends_up, 1, 0) == 0) {
        InitializeCriticalSection(&ends_mu);
        ends_up = 2;
    }
    while (ends_up != 2)
        Sleep(0);
    EnterCriticalSection(&ends_mu);
}

static void ends_unlock(void)
{
    LeaveCriticalSection(&ends_mu);
}

static End *end_of(int i)
{
    return i >= 0 && i < ENDS ? ends[i] : NULL;
}

/* A path as Windows' calls take it: `//./pipe/a/b` is `\\.\pipe\a\b`. */
static void native(const char *path, char *out, size_t max)
{
    size_t i;
    for (i = 0; path[i] && i + 1 < max; i++)
        out[i] = path[i] == '/' ? '\\' : path[i];
    out[i] = 0;
}

/* Whether `path` is `dir` or under it; pipe names ignore case. */
static int under(const char *path, const char *dir)
{
    size_t n = strlen(dir);
    return _strnicmp(path, dir, n) == 0 && (path[n] == '/' || path[n] == 0);
}

static int end_new(int kind, const char *path)
{
    int i;
    End *e = calloc(1, sizeof *e);
    if (!e)
        return -1;
    e->kind = kind;
    snprintf(e->path, sizeof e->path, "%s", path ? path : "");
    ends_lock();
    for (i = 0; i < ENDS && ends[i]; i++)
        ;
    if (i < ENDS)
        ends[i] = e;
    ends_unlock();
    if (i == ENDS) {
        free(e);
        errno = EMFILE;
        return -1;
    }
    return i;
}

/* The end made for `path` of this kind, or -1. */
static int end_find(int kind, const char *path)
{
    int i, found = -1;
    ends_lock();
    for (i = 0; i < ENDS && found < 0; i++)
        if (ends[i] && ends[i]->kind == kind && !_stricmp(ends[i]->path, path))
            found = i;
    ends_unlock();
    return found;
}

/* -- one instance -------------------------------------------------------- */

static int pipe_make(Pipe *p, const char *path, DWORD mode, DWORD type, DWORD instances,
                     DWORD out_room, DWORD in_room, DWORD cap)
{
    char name[760];
    native(path, name, sizeof name);
    memset(p, 0, sizeof *p);
    p->h = CreateNamedPipeA(name, mode | FILE_FLAG_OVERLAPPED,
                            type | PIPE_REJECT_REMOTE_CLIENTS, instances,
                            out_room, in_room, 0, NULL);
    if (p->h == INVALID_HANDLE_VALUE) {
        p->h = NULL;
        return -1;
    }
    p->ov.hEvent = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (cap)
        p->buf = malloc(cap);
    p->cap = cap;
    if (!p->ov.hEvent || (cap && !p->buf)) {
        CloseHandle(p->h);
        if (p->ov.hEvent)
            CloseHandle(p->ov.hEvent);
        free(p->buf);
        memset(p, 0, sizeof *p);
        return -1;
    }
    return 0;
}

static void ov_reset(Pipe *p)
{
    HANDLE ev = p->ov.hEvent;
    memset(&p->ov, 0, sizeof p->ov);
    p->ov.hEvent = ev;
    ResetEvent(ev);
}

/* The call under way given up, and waited for, so its buffer is free. */
static void pipe_cancel(Pipe *p)
{
    DWORD n;
    if (p->pending && p->h) {
        CancelIoEx(p->h, &p->ov);
        GetOverlappedResult(p->h, &p->ov, &n, TRUE);
    }
    p->pending = P_NONE;
}

/* Ready for the next caller: the last one's end let go. */
static void pipe_again(Pipe *p)
{
    pipe_cancel(p);
    if (p->h)
        DisconnectNamedPipe(p->h);
    p->connected = p->eof = 0;
    p->got = p->off = 0;
    p->nreq = 0;
}

static void pipe_free(Pipe *p)
{
    pipe_cancel(p);
    if (p->h)
        CloseHandle(p->h);
    if (p->ov.hEvent)
        CloseHandle(p->ov.hEvent);
    free(p->buf);
    memset(p, 0, sizeof *p);
}

/* Waiting for a caller, or connected already. */
static void pipe_connect(Pipe *p)
{
    ov_reset(p);
    if (ConnectNamedPipe(p->h, &p->ov)) {
        p->connected = 1;
        return;
    }
    switch (GetLastError()) {
    case ERROR_PIPE_CONNECTED:
    case ERROR_NO_DATA:             /* came and went: its read says so */
        p->connected = 1;
        break;
    case ERROR_IO_PENDING:
        p->pending = P_CONNECT;
        break;
    default:
        break;                      /* tried again next time */
    }
}

static void pipe_read(Pipe *p)
{
    ov_reset(p);
    p->got = p->off = 0;
    if (ReadFile(p->h, p->buf, p->cap, NULL, &p->ov) || GetLastError() == ERROR_IO_PENDING)
        p->pending = P_READ;
    else
        p->eof = 1;                 /* broken pipe: the writer has gone */
}

/* The call under way, if it has ended: 1 when it has. */
static int pipe_done(Pipe *p, DWORD *n)
{
    int kind = p->pending;
    *n = 0;
    if (!kind || WaitForSingleObject(p->ov.hEvent, 0) != WAIT_OBJECT_0)
        return 0;
    p->pending = P_NONE;
    if (GetOverlappedResult(p->h, &p->ov, n, FALSE)) {
        if (kind == P_CONNECT)
            p->connected = 1;
        return 1;
    }
    if (kind == P_CONNECT)
        p->connected = GetLastError() == ERROR_PIPE_CONNECTED || GetLastError() == ERROR_NO_DATA;
    else
        p->eof = 1;
    *n = 0;
    return 1;
}

/* A pipe the wall reads, moved on without waiting: 1 once it has bytes
 * or its writer has gone. */
static int pipe_ready(Pipe *p)
{
    int turns;
    for (turns = 0; turns < 4; turns++) {
        DWORD n;
        if (p->got > p->off || p->eof)
            return 1;
        if (p->pending) {
            if (!pipe_done(p, &n))
                return 0;
            if (p->connected && !p->eof)
                p->got = n, p->off = 0;
            continue;
        }
        if (!p->connected)
            pipe_connect(p);
        else
            pipe_read(p);
    }
    return p->got > p->off || p->eof;
}

/* -- the door ------------------------------------------------------------ */

/* Each instance moved on, and every request whose caller has closed put
 * with the others to be read. */
static void door_poll(End *e)
{
    int i, turns;
    for (i = 0; i < e->n; i++) {
        Pipe *p = &e->p[i];
        for (turns = 0; turns < 16 && pipe_ready(p); turns++) {
            if (p->got > p->off) {
                size_t k = p->got - p->off;
                if (p->nreq + k <= DOOR_MAX) {
                    memcpy(p->req + p->nreq, p->buf + p->off, k);
                    p->nreq += k;
                } else {
                    p->nreq = DOOR_MAX + 1;     /* too long to be a request: dropped */
                }
                p->got = p->off = 0;
                continue;
            }
            if (p->nreq && p->nreq <= DOOR_MAX) {
                char *more = realloc(e->done, e->ndone + p->nreq);
                if (more) {
                    memcpy(more + e->ndone, p->req, p->nreq);
                    e->done = more;
                    e->ndone += p->nreq;
                }
            }
            pipe_again(p);
        }
    }
}

static int ipc_claim(const char *run, const char *name, const char *dir)
{
    char door[760];
    int i, idx;
    End *e;
    (void)run;
    (void)name;
    snprintf(door, sizeof door, "%s/door.in", dir);
    if ((idx = end_new(E_DOOR, door)) < 0) {
        fprintf(stderr, "bitwall-paint: %s: %s\n", door, strerror(errno));
        return -1;
    }
    e = ends[idx];
    for (i = 0; i < DOOR_INSTANCES; i++) {
        if (pipe_make(&e->p[i], door, PIPE_ACCESS_INBOUND
                      | (i == 0 ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0),
                      PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, DOOR_INSTANCES,
                      4096, DOOR_MAX, DOOR_MAX) < 0) {
            DWORD why = GetLastError();
            /* Denied as first of its name, or busy with every instance
             * the live receiver made: either way the wall is held. */
            if (i == 0 && (why == ERROR_ACCESS_DENIED || why == ERROR_PIPE_BUSY))
                fprintf(stderr, "bitwall-paint: %s is held by a live receiver\n", dir);
            else
                fprintf(stderr, "bitwall-paint: %s: error %lu\n", door, (unsigned long)why);
            while (i--)
                pipe_free(&e->p[i]);
            ends[idx] = NULL;
            free(e);
            return -1;
        }
        e->n++;
    }
    e->held = 1;
    return idx;
}

/* The painter's own notes go in the user's temporary folder, since the
 * run directory holds only pipes. */
static const char *ipc_notes(const char *run)
{
    static char dir[MAX_PATH + 16];
    char tmp[MAX_PATH + 1];
    size_t i;
    (void)run;
    if (!dir[0]) {
        DWORD n = GetTempPathA(sizeof tmp, tmp);
        if (n == 0 || n > MAX_PATH)
            snprintf(tmp, sizeof tmp, ".\\");
        snprintf(dir, sizeof dir, "%sbitwall", tmp);
        for (i = 0; dir[i]; i++)
            if (dir[i] == '\\')
                dir[i] = '/';
        CreateDirectoryA(dir, NULL);
    }
    return dir;
}

/* -- a session's pipes --------------------------------------------------- */

/* A session's name is taken while any pipe under it is the wall's. */
static int ipc_session_make(const char *dir)
{
    int i, taken = 0;
    ends_lock();
    for (i = 0; i < ENDS && !taken; i++)
        taken = ends[i] && ends[i]->kind != E_WAKER && under(ends[i]->path, dir)
            && _stricmp(ends[i]->path, dir) != 0;
    ends_unlock();
    return taken ? -1 : 0;
}

/* A pipe the wall reads, for one writer at a time. */
static int reader_make(int kind, const char *path)
{
    int idx = end_new(kind, path);
    if (idx < 0)
        return -1;
    if (pipe_make(&ends[idx]->p[0], path, PIPE_ACCESS_INBOUND,
                  PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, 1, 4096, PIPE_ROOM,
                  PIPE_PIECE) < 0) {
        free(ends[idx]);
        ends[idx] = NULL;
        errno = EIO;
        return -1;
    }
    ends[idx]->n = 1;
    return idx;
}

static void ipc_in_make(const char *path)
{
    reader_make(E_IN, path);
}

static int ipc_in_open(const char *path, int nonblocking)
{
    int idx = end_find(E_IN, path);
    (void)nonblocking;              /* the loop's reads never wait; a thread waits first */
    if (idx >= 0)
        ends[idx]->held = 1;
    return idx;
}

static int ipc_out_make(const char *path)
{
    int idx = end_new(E_OUT, path);
    char name[760];
    End *e;
    if (idx < 0)
        return -1;
    e = ends[idx];
    native(path, name, sizeof name);
    e->p[0].h = CreateNamedPipeA(name, PIPE_ACCESS_OUTBOUND,
                                 PIPE_TYPE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS, 1,
                                 PIPE_ROOM, 4096, 0, NULL);
    if (e->p[0].h == INVALID_HANDLE_VALUE) {
        ends[idx] = NULL;
        free(e);
        return -1;
    }
    e->n = 1;
    e->held = 1;
    return idx;
}

static int ipc_data_make(const char *path)
{
    if (end_find(E_DATA, path) >= 0)
        return 0;
    return reader_make(E_DATA, path) < 0 ? -1 : 0;
}

static int ipc_data_open(const char *path)
{
    int idx = end_find(E_DATA, path);
    if (idx >= 0)
        ends[idx]->held = 1;
    return idx;
}

/* -- a glass stream ------------------------------------------------------ */

static int ipc_glass_make(const char *path)
{
    int idx;
    if (end_find(E_GLASS, path) >= 0)
        return 0;
    if ((idx = end_new(E_GLASS, path)) < 0)
        return -1;
    if (pipe_make(&ends[idx]->p[0], path, PIPE_ACCESS_OUTBOUND,
                  PIPE_TYPE_BYTE | PIPE_WAIT, 1, PIPE_ROOM, 4096, 0) < 0) {
        free(ends[idx]);
        ends[idx] = NULL;
        return -1;
    }
    ends[idx]->n = 1;
    return idx >= 0 ? 0 : -1;
}

/* Its end once a reader has connected, -1 while none has. */
static int ipc_glass_open(const char *path)
{
    int idx = end_find(E_GLASS, path);
    Pipe *p;
    DWORD n;
    if (idx < 0)
        return -1;
    p = &ends[idx]->p[0];
    if (!p->connected && !p->pending)
        pipe_connect(p);
    if (p->pending == P_CONNECT)
        pipe_done(p, &n);
    if (!p->connected) {
        errno = ENXIO;
        return -1;
    }
    ends[idx]->held = 1;
    return idx;
}

static void end_free(int idx);

static void ipc_glass_remove(const char *path)
{
    int idx = end_find(E_GLASS, path);
    if (idx >= 0)
        end_free(idx);
}

/* -- stdin --------------------------------------------------------------- */

static int ipc_stdin(void)
{
    int idx = end_new(E_STDIN, "");
    if (idx >= 0) {
        ends[idx]->p[0].h = GetStdHandle(STD_INPUT_HANDLE);
        ends[idx]->n = 1;
        ends[idx]->held = 1;
    }
    return idx;
}

/* Whether stdin would read without waiting: bytes on its pipe, or its
 * writer gone; anything that is not a pipe always would. */
static int stdin_ready(End *e)
{
    DWORD avail = 0;
    if (GetFileType(e->p[0].h) != FILE_TYPE_PIPE)
        return 1;
    if (!PeekNamedPipe(e->p[0].h, NULL, 0, NULL, &avail, NULL))
        return 1;
    return avail > 0;
}

/* -- reading and writing ------------------------------------------------- */

static int end_ready(End *e)
{
    switch (e->kind) {
    case E_DOOR:
        door_poll(e);
        return e->ndone > 0;
    case E_IN:
    case E_DATA:
        return pipe_ready(&e->p[0]);
    case E_STDIN:
        return stdin_ready(e);
    default:
        return 0;
    }
}

static ssize_t ipc_read(int end, void *buf, size_t n)
{
    End *e = end_of(end);
    Pipe *p;
    size_t k;
    if (!e) {
        errno = EBADF;
        return -1;
    }
    if (e->kind == E_STDIN) {
        DWORD got = 0;
        if (!ReadFile(e->p[0].h, buf, (DWORD)(n > 0x40000000 ? 0x40000000 : n), &got, NULL))
            return 0;
        return (ssize_t)got;
    }
    if (e->kind == E_DOOR) {
        door_poll(e);
        if (!e->ndone) {
            errno = EAGAIN;
            return -1;
        }
        k = e->ndone < n ? e->ndone : n;
        memcpy(buf, e->done, k);
        memmove(e->done, e->done + k, e->ndone - k);
        e->ndone -= k;
        return (ssize_t)k;
    }
    p = &e->p[0];
    if (!pipe_ready(p)) {
        errno = EAGAIN;
        return -1;
    }
    if (p->got > p->off) {
        k = p->got - p->off;
        if (k > n)
            k = n;
        memcpy(buf, p->buf + p->off, k);
        p->off += (DWORD)k;
        if (p->off == p->got)
            p->got = p->off = 0;
        return (ssize_t)k;
    }
    return 0;                       /* end of file: the writer has gone */
}

/* `out`, a piece at a time, as much as goes now. */
static ssize_t out_write(Pipe *p, const uint8_t *b, size_t n)
{
    size_t done = 0;
    while (done < n) {
        DWORD k = 0, want = (DWORD)(n - done > 65536 ? 65536 : n - done);
        if (!WriteFile(p->h, b + done, want, &k, NULL) || k == 0)
            break;                  /* full, no reader yet, or its reader gone */
        done += k;
        if (k < want)
            break;
    }
    if (!done) {
        errno = EAGAIN;
        return -1;
    }
    return (ssize_t)done;
}

/* A glass stream: one overlapped write at a time, whose count is said
 * when it has ended; the caller asks again with the same bytes. */
static ssize_t glass_write(Pipe *p, const uint8_t *b, size_t n)
{
    DWORD k;
    if (!p->pending) {
        ov_reset(p);
        if (!WriteFile(p->h, b, (DWORD)(n > 0x40000000 ? 0x40000000 : n), NULL, &p->ov)
            && GetLastError() != ERROR_IO_PENDING) {
            errno = EPIPE;
            return -1;
        }
        p->pending = P_WRITE;
    }
    if (!pipe_done(p, &k)) {
        errno = EAGAIN;
        return -1;
    }
    if (p->eof || !k) {
        errno = EPIPE;
        return -1;
    }
    return (ssize_t)k;
}

static ssize_t ipc_write(int end, const void *buf, size_t n)
{
    End *e = end_of(end);
    if (!e) {
        errno = EBADF;
        return -1;
    }
    if (e->kind == E_OUT)
        return out_write(&e->p[0], buf, n);
    if (e->kind == E_GLASS)
        return glass_write(&e->p[0], buf, n);
    errno = EBADF;
    return -1;
}

/* What the wall has written to `out` outlives its handle, so nothing is
 * held once the queue is empty. */
static int ipc_held(int out)
{
    (void)out;
    return 0;
}

static void end_free(int idx)
{
    End *e = end_of(idx);
    int i;
    if (!e)
        return;
    ends_lock();
    ends[idx] = NULL;
    ends_unlock();
    if (e->kind == E_WAKER) {
        CloseHandle(e->ev);
    } else if (e->kind == E_STDIN) {
        /* the process's own handle, left as it is */
    } else {
        for (i = 0; i < e->n; i++)
            pipe_free(&e->p[i]);
    }
    free(e->done);
    free(e);
}

static void ipc_close(int end)
{
    End *e = end_of(end);
    if (!e)
        return;
    if (e->kind == E_DATA || e->kind == E_GLASS) {
        pipe_again(&e->p[0]);       /* kept for the next writer, or reader */
        e->held = 0;
        return;
    }
    end_free(end);
}

static void ipc_remove(const char *dir)
{
    int i;
    for (i = 0; i < ENDS; i++) {
        End *e = ends[i];
        if (e && !e->held && e->kind != E_WAKER && under(e->path, dir))
            end_free(i);
    }
}

/* -- waiting ------------------------------------------------------------- */

static int waker_open(int p[2])
{
    int idx = end_new(E_WAKER, "");
    p[0] = p[1] = -1;
    if (idx < 0)
        return -1;
    ends[idx]->ev = CreateEventA(NULL, TRUE, FALSE, NULL);
    if (!ends[idx]->ev) {
        end_free(idx);
        return -1;
    }
    ends[idx]->held = 1;
    p[0] = p[1] = idx;
    return 0;
}

static void waker_ring(const int p[2])
{
    End *e = end_of(p[1]);
    if (e)
        SetEvent(e->ev);
}

static void waker_drain(const int p[2])
{
    End *e = end_of(p[0]);
    if (e)
        ResetEvent(e->ev);
}

static void waker_close(int p[2])
{
    end_free(p[0]);
    p[0] = p[1] = -1;
}

/* What a wait on `e` waits for: its call's event, or nothing, when it
 * is polled. */
static HANDLE end_event(End *e, int write)
{
    if (!e || e->kind == E_STDIN || e->kind == E_DOOR)
        return NULL;
    if (write)
        return e->p[0].pending ? e->p[0].ov.hEvent : NULL;
    return e->p[0].pending ? e->p[0].ov.hEvent : NULL;
}

static int wait_on(int fd, int waker_fd, int ms, int write)
{
    End *e = end_of(fd), *w = end_of(waker_fd);
    DWORD until = GetTickCount() + (DWORD)ms;
    for (;;) {
        HANDLE hs[2];
        DWORD n = 0, r;
        LONG left;
        if (e) {
            if (write ? !e->p[0].pending || WaitForSingleObject(e->p[0].ov.hEvent, 0)
                        == WAIT_OBJECT_0
                      : end_ready(e))
                return 1;
        }
        if (w && WaitForSingleObject(w->ev, 0) == WAIT_OBJECT_0)
            return 0;
        left = (LONG)(until - GetTickCount());
        if (left <= 0)
            return 0;
        if (e && (hs[n] = end_event(e, write)))
            n++;
        else if (e)
            left = left > 10 ? 10 : left;   /* polled */
        if (w)
            hs[n++] = w->ev;
        if (!n) {
            Sleep((DWORD)left);
            continue;
        }
        r = WaitForMultipleObjects(n, hs, FALSE, (DWORD)left);
        if (r == WAIT_FAILED) {
            errno = EIO;
            return -1;
        }
    }
}

static int wait_readable(int fd, int waker_fd, int ms)
{
    return wait_on(fd, waker_fd, ms, 0);
}

static int wait_writable(int fd, int waker_fd, int ms)
{
    return wait_on(fd, waker_fd, ms, 1);
}
