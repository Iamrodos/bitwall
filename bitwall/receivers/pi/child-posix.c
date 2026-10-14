/*
 * Child processes on a POSIX machine: fork, a pipe each way, and exec.
 * Part of bitwall-paint.c, which includes it (platform.h).
 */
#include <sys/resource.h>
#include <sys/wait.h>

/* The child closes every descriptor but its three before it execs, so a
 * decoder holds none of the painter's fifos open: a session's pipe that
 * a child kept would never see its writer go. */
static Child child_start(const char *const *argv, int nice_by, int *to, int *from)
{
    int in[2], out[2];
    pid_t pid;
    if (pipe(in) < 0)
        return 0;
    if (pipe(out) < 0) {
        close(in[0]);
        close(in[1]);
        return 0;
    }
    pid = fork();
    if (pid == 0) {
        int fd;
        dup2(in[0], 0);
        dup2(out[1], 1);
        for (fd = 3; fd < 1024; fd++)
            close(fd);
        signal(SIGPIPE, SIG_DFL);
        if (nice_by > 0)
            setpriority(PRIO_PROCESS, 0, getpriority(PRIO_PROCESS, 0) + nice_by);
        execvp(argv[0], (char *const *)argv);
        _exit(127);
    }
    close(in[0]);
    close(out[1]);
    if (pid < 0) {
        close(in[1]);
        close(out[0]);
        return 0;
    }
    fcntl(in[1], F_SETFD, FD_CLOEXEC);
    fcntl(out[0], F_SETFD, FD_CLOEXEC);
    *to = in[1];
    *from = out[0];
    return pid;
}

static void child_kill(Child c)
{
    kill(c, SIGKILL);
}

static int child_wait(Child c, int *code)
{
    int st = 0;
    waitpid(c, &st, 0);
    if (code)
        *code = WIFEXITED(st) ? WEXITSTATUS(st) : WTERMSIG(st);
    return WIFEXITED(st);
}
