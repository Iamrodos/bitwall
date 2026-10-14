/*
 * Child processes on Windows: CreateProcess with an anonymous pipe each
 * way, the painter's ends made C descriptors in binary mode so the
 * compositor reads and writes them as it does anywhere. The child
 * inherits exactly its three handles and no other of the painter's, so a
 * decoder holds no session's pipe open, whatever another thread is
 * starting at the same moment. Each child runs in a job of its own that
 * goes when the child is let go, so killing it kills whatever it started:
 * a Chocolatey shim's ffmpeg with the shim. Part of bitwall-paint.c,
 * which includes it (platform.h).
 */
#include <io.h>

typedef struct {
    HANDLE process, job;
    volatile int killed;
} WinChild;

/* One argument quoted as CommandLineToArgvW reads it back. */
static void arg_quote(char *out, size_t cap, size_t *len, const char *a)
{
    size_t n = *len;
    int slashes;
#define PUT(c) do { if (n + 1 < cap) out[n++] = (c); } while (0)
    if (n)
        PUT(' ');
    PUT('"');
    for (; *a; a++) {
        slashes = 0;
        while (*a == '\\') {
            slashes++;
            a++;
        }
        if (!*a) {
            while (slashes--) { PUT('\\'); PUT('\\'); }
            break;
        }
        if (*a == '"') {
            while (slashes--) { PUT('\\'); PUT('\\'); }
            PUT('\\');
        } else {
            while (slashes--)
                PUT('\\');
        }
        PUT(*a);
    }
    PUT('"');
#undef PUT
    out[n] = 0;
    *len = n;
}

static int fd_of(HANDLE h, int flags)
{
    int fd = _open_osfhandle((intptr_t)h, flags);
    if (fd >= 0)
        _setmode(fd, _O_BINARY);
    return fd;
}

/* Windows has classes, not nice: a little below the painter, below
 * normal, and from 15, idle. */
static DWORD nice_class(int nice)
{
    return nice >= 15 ? IDLE_PRIORITY_CLASS : nice > 0 ? BELOW_NORMAL_PRIORITY_CLASS : 0;
}

static Child child_start(const char *const *argv, int nice_by, int *to, int *from)
{
    SECURITY_ATTRIBUTES sa = {sizeof sa, NULL, TRUE};
    HANDLE in_r = NULL, in_w = NULL, out_r = NULL, out_w = NULL, err = NULL, list[3];
    STARTUPINFOEXA si;
    PROCESS_INFORMATION pi;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim;
    LPPROC_THREAD_ATTRIBUTE_LIST attrs = NULL;
    SIZE_T asize = 0;
    WinChild *c = NULL;
    char cmd[8192];
    size_t len = 0;
    int i, ok = 0;
    cmd[0] = 0;
    for (i = 0; argv[i]; i++)
        arg_quote(cmd, sizeof cmd, &len, argv[i]);
    if (!CreatePipe(&in_r, &in_w, &sa, 1 << 20) || !CreatePipe(&out_r, &out_w, &sa, 1 << 20))
        goto out;
    SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    /* The painter's stderr, as an inheritable handle of the child's own. */
    if (!DuplicateHandle(GetCurrentProcess(), GetStdHandle(STD_ERROR_HANDLE),
                         GetCurrentProcess(), &err, 0, TRUE, DUPLICATE_SAME_ACCESS))
        err = NULL;
    list[0] = in_r;
    list[1] = out_w;
    list[2] = err;
    InitializeProcThreadAttributeList(NULL, 1, 0, &asize);
    if (!(attrs = malloc(asize)) || !InitializeProcThreadAttributeList(attrs, 1, 0, &asize))
        goto out;
    if (!UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, list,
                                   (err ? 3 : 2) * sizeof(HANDLE), NULL, NULL))
        goto out;
    memset(&si, 0, sizeof si);
    si.StartupInfo.cb = sizeof si;
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdInput = in_r;
    si.StartupInfo.hStdOutput = out_w;
    si.StartupInfo.hStdError = err;
    si.lpAttributeList = attrs;
    if (!(c = calloc(1, sizeof *c)) || !(c->job = CreateJobObjectA(NULL, NULL)))
        goto out;
    memset(&lim, 0, sizeof lim);
    lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(c->job, JobObjectExtendedLimitInformation, &lim, sizeof lim);
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE,
                        CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT
                        | nice_class(nice_by),
                        NULL, NULL, &si.StartupInfo, &pi))
        goto out;
    AssignProcessToJobObject(c->job, pi.hProcess);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    c->process = pi.hProcess;
    *to = fd_of(in_w, 0);
    *from = fd_of(out_r, _O_RDONLY);
    in_w = out_r = NULL;
    ok = 1;
out:
    if (attrs) {
        DeleteProcThreadAttributeList(attrs);
        free(attrs);
    }
    if (in_r)
        CloseHandle(in_r);
    if (out_w)
        CloseHandle(out_w);
    if (err)
        CloseHandle(err);
    if (!ok) {
        if (in_w)
            CloseHandle(in_w);
        if (out_r)
            CloseHandle(out_r);
        if (c && c->job)
            CloseHandle(c->job);
        free(c);
        return 0;
    }
    return (Child)(intptr_t)c;
}

static void child_kill(Child c)
{
    WinChild *w = (WinChild *)(intptr_t)c;
    w->killed = 1;
    TerminateJobObject(w->job, 1);
}

/* A child ended by child_kill is said to be killed, as a signal would
 * be, with the code TerminateJobObject gave it; any other end is an exit. */
static int child_wait(Child c, int *code)
{
    WinChild *w = (WinChild *)(intptr_t)c;
    DWORD st = 0;
    int killed;
    WaitForSingleObject(w->process, INFINITE);
    GetExitCodeProcess(w->process, &st);
    killed = w->killed;
    CloseHandle(w->process);
    CloseHandle(w->job);
    free(w);
    if (code)
        *code = (int)st;
    return !killed;
}
