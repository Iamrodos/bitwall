/*
 * The seams between the painter and the machine it runs on.
 *
 * The compositor -- the parser, the grid, the atlas, the layers, the
 * doors and sessions -- is the same on every machine. What it asks of
 * the machine is here, in six parts, each a few calls with one
 * implementation per platform: the display it paints, the GPU context it
 * draws with, the child processes it starts, the decoders the board has,
 * what the machine says of itself, and the pipes a wall is made of. A
 * Pi's are KMS through SDL (display-kms.c), GLES 2 through SDL's EGL on
 * GBM (gpu-gles.c), fork and pipes (child-posix.c), V4L2 with ffmpeg
 * (probe-v4l2.c), /proc (machine-linux.c) and fifos (ipc-posix.c). A
 * Mac's are an SDL window or no screen at all (display-sdl.c), desktop GL
 * 2.1 through SDL or CGL (gpu-gl.c), the same children, VideoToolbox with
 * ffmpeg (probe-mac.c), sysctl (machine-mac.c) and the same fifos.
 * Windows' are the Mac's display, GL 2.1 through WGL (gpu-win.c),
 * CreateProcess in a job (child-win.c), D3D11VA or QSV with ffmpeg
 * (probe-win.c), the process's own counters (machine-win.c) and named
 * pipes (ipc-win.c, WINDOWS.md), with compat-win.h for the little of
 * POSIX the compositor uses that Windows lacks. Another platform is
 * another file for a seam, not a change to the compositor.
 *
 * Everything is static, because the painter is one translation unit:
 * bitwall-paint.c includes this after the Screen it works on, and the
 * implementations at its end, so the build is one cc of one file and the
 * tests include the whole painter as they always have.
 */

/* ------------------------------------------------------------- display */

/* The glass: the outputs a machine has, a window on each, the window's
 * size and surface, and whether the screen behind it is awake. A machine
 * may also paint no screen at all, its glass a size it was given, where
 * a desktop's display seam offers it. */

/* The driver the display comes up on when `--driver` does not say. */
#ifdef BITWALL_MAC
#define DISPLAY_DRIVER "cocoa"
#elif defined(BITWALL_WIN)
#define DISPLAY_DRIVER "windows"
#else
#define DISPLAY_DRIVER "kmsdrm"
#endif

/* The video system up on `driver`, as SDL names one, or -1 with the reason
 * said; and down again. */
static int display_open(const char *driver);
static void display_close(void);

/* How many walls to drive, from `--walls`: -1 for auto, zero when it was
 * not given; `display_given` for `--display`, `stdin_only` without `--run`. */
static int walls_wanted(int walls, int display_given, int stdin_only, const char *driver);

/* One screen: its window on display `display`, the GPU path on it where
 * gl_setup takes, and the software path's surfaces otherwise. -1 when
 * there is no window to be had. */
static int screen_open(Screen *s, int display, int want_w, int want_h);

/* The output's mode set nearest `w`x`h`, where the display can. */
static void apply_mode(Screen *s, int w, int h, const char *driver);

/* Which output each screen is on, the screens put in wall order, and
 * each woken. */
static void walls_up(Screen **s, int n);

/* The window's size in pixels, as the output shows it. */
static void display_size(Screen *s, int *w, int *h);

/* The window's surface, which the software path paints, made again
 * after a resize; NULL where there is none. */
static SDL_Surface *display_surface(Screen *s);

/* Whether a controller may ask the glass for a size (`glass WxH`): where
 * it is a window or no screen, and not where it is a monitor's mode. */
static int glass_offered(Screen *s);

/* The glass asked to be `w` by `h` pixels: with no screen, the size
 * display_size says from now on; in a window, the window made that size.
 * The wall takes it once it has settled (wall_resized). */
static void display_glass(Screen *s, int w, int h);

/* The software path's picture pushed out to the glass: all of it, or
 * with `rects`, those `n` rectangles. */
static void display_push(Screen *s, const SDL_Rect *rects, int n);

/* The display's housekeeping, run once a frame. */
static void display_pump(void);

/* What the display has to say, heard once a pass of the loop whether or
 * not anything was painted: a window is answered as its system expects.
 * 1 when whoever owns the glass asked the painter to stop, as closing
 * its window does. */
static int display_events(void);

/* The output woken, or put to sleep. Best effort. */
static void power(Screen *s, int on);

/* The window let go. */
static void display_drop(Screen *s);

/* ----------------------------------------------------------------- GPU */

/* A GL context of each screen's own on its window, never shared, so one
 * wall's atlas can never be another's. The shaders are GLES 2's. */

/* What the GPU path is called, in `report` and the log. */
static const char *gpu_name(void);

/* The kind of context every window will get, said before any is made. */
static void gpu_attributes(void);

/* The context on the screen's window, current, its swaps not waiting for
 * the display; -1 where there is none. */
static int gpu_context(Screen *s);

/* What goes before every shader's text so the GPU takes it as GLES 2's:
 * nothing where it speaks GLES 2. */
static const char *gpu_preamble(void);

/* The framebuffer the glass is put up in: the window's, or where there
 * is no window, the context's own picture of the glass's size. */
static GLuint gpu_glass_fb(Screen *s);

/* The glass has taken a new size (phys_w by phys_h): the context's own
 * picture of it made again at that size, where it keeps one; a window's
 * framebuffer follows its window by itself. -1 where it could not be. */
static int gpu_resized(Screen *s);

/* Whether a frame read back may be collected a frame later, through a
 * pixel buffer: 1 with how to map one for reading and let it go; 0 where
 * it is read as it is drawn. */
static int gpu_read_later(void *(**map)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield),
                          GLboolean (**unmap)(GLenum));

/* A grid's vertices, `bytes` of them, into the array buffer bound, for
 * drawing once or a few times; how big the grid is changes with the font
 * and the glass. */
static void gpu_vertices(const void *verts, size_t bytes);

/* The screen's context made the current one. */
static void gl_use(Screen *s);

/* What has been drawn put on the glass; with no screen, only finished. */
static void gpu_swap(Screen *s);

/* The screen's context let go. */
static void gpu_drop(Screen *s);

/* A GL function the headers do not promise, by name, or NULL. */
static void *gpu_proc(const char *name);

/* ----------------------------------------------------- child processes */

/* A program the painter starts and talks to on two pipes: a decoder. */

/* The platform's handle on a child; 0 is none. */
#ifdef BITWALL_WIN
typedef intptr_t Child;         /* the process and its job, child-win.c */
#else
typedef pid_t Child;
#endif

/* `argv` started with a pipe to its stdin and one from its stdout, the
 * painter's own ends in `*to` and `*from`, closed on exec; its stderr is
 * the painter's, and no other descriptor of the painter's is. `nice_by`, 0 to
 * 19, is how far below the painter it runs, as nice(1) has it. 0 when it
 * could not be started. */
static Child child_start(const char *const *argv, int nice_by, int *to, int *from);

/* The child stopped at once, whatever it is doing. */
static void child_kill(Child c);

/* Waited for. 1 when it exited, its status in `*code`; 0 when it was
 * killed, the signal in `*code`. `code` may be NULL. */
static int child_wait(Child c, int *code);

/* ------------------------------------------------------------ decoders */

/* What the board decodes, and how ffmpeg is put on it. */

/* `codecs` filled in: which codecs this wall takes on `data`, which of
 * them the board decodes, and the ffmpeg to run. Once, as the walls come
 * up. */
static void codecs_probe(void);

/* The words that put ffmpeg on the board's decoder for a stream of
 * `codec`, into `argv`; how many. None where ffmpeg decodes it itself. */
static int decoder_hw(int codec, const char **argv);

/* ------------------------------------------------------------- machine */

/* What the machine says of itself, for `measures` and `report`. */

/* The painter's own CPU time in seconds, or -1 where it cannot be read. */
static double cpu_seconds(void);

/* The GPU time this process has used, in seconds, or -1 where the
 * machine does not say. */
static double gpu_seconds(void);

/* How long the machine has been up, in seconds; 0 where it does not say. */
static double uptime_seconds(void);

/* When the machine started, in Unix seconds; 0 where it does not say. */
static long machine_booted(void);

/* --------------------------------------------------------------- pipes */

/* What a wall is made of: its door, each session's `in`, `out` and
 * `data`, and its glass streams, each a pipe at the path PROTOCOL.md
 * names; and the wakers the painter's threads wait on beside them. An end
 * is an int, -1 for none: a fifo's descriptor, or on Windows the painter's
 * own number for a named pipe (WINDOWS.md). */

/* The wall at `dir` (`<run>/bitwall/<name>`) claimed, and its door, read
 * without waiting and never at an end; -1, the reason said, when a live
 * receiver holds it or it cannot be made. */
static int ipc_claim(const char *run, const char *name, const char *dir);

/* The directory the painter's own notes about its walls go in: each
 * wall's last-seen, and on a Pi or a Mac the claim's lock. */
static const char *ipc_notes(const char *run);

/* A session's place made, `dir` being `<wall>/s/<name>`: -1 when the
 * name is a session's already, or it cannot be made. */
static int ipc_session_make(const char *dir);

/* A session's `in`, made; opened by ipc_in_open once it is admitted. */
static void ipc_in_make(const char *path);

/* A session's `out`, made last, and its end: written without ever
 * waiting, and what is said before its reader comes kept for it or left
 * in the session's queue. */
static int ipc_out_make(const char *path);

/* `in`'s end, read blocking from a reader thread, or with `nonblocking`
 * by the loop; at end of file when its writer goes. */
static int ipc_in_open(const char *path, int nonblocking);

/* A video layer's `data`, made; and its end for the next stream, which
 * reads nothing until a writer comes and ends when it goes. */
static int ipc_data_make(const char *path);
static int ipc_data_open(const char *path);

/* A glass stream's pipe, made; its end to write once a reader has it,
 * -1 at once while nobody does; and the pipe gone. */
static int ipc_glass_make(const char *path);
static int ipc_glass_open(const char *path);
static void ipc_glass_remove(const char *path);

/* The painter's own stdin, as an end, for a painter with no run
 * directory. */
static int ipc_stdin(void);

/* As read and write: what has come, 0 at end of file, -1 with errno
 * EAGAIN when nothing has; as much as the pipe takes now, -1 with EAGAIN
 * when it takes nothing. After EAGAIN a glass stream's next write is the
 * same bytes. */
static ssize_t ipc_read(int end, void *buf, size_t n);
static ssize_t ipc_write(int end, const void *buf, size_t n);

/* Whether `out` still holds bytes its reader has not taken. */
static int ipc_held(int out);

/* An end let go. A `data` or glass pipe stays, for its next writer or
 * reader, until its session or stream is removed. */
static void ipc_close(int end);

/* A wall's or a session's directory and every pipe under it, gone. */
static void ipc_remove(const char *dir);

/* A waker, for ringing a thread that waits on a pipe: rung, it ends the
 * wait at once, and stays rung until drained. */
static int waker_open(int p[2]);
static void waker_ring(const int p[2]);
static void waker_drain(const int p[2]);
static void waker_close(int p[2]);

/* Wait up to `ms` for `fd` to be readable (bytes, or its writer gone) or
 * writable, or the waker to ring: 1 when it is, 0 when the wait ran out
 * or the waker rang, -1 on an error with errno set. `fd` may be -1, to
 * wait on the waker alone. */
static int wait_readable(int fd, int waker_fd, int ms);
static int wait_writable(int fd, int waker_fd, int ms);
