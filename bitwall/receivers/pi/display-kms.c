/*
 * The display on a Pi: KMS, through SDL's kmsdrm driver, a fullscreen
 * window on each connected output with the card itself borrowed from SDL
 * for what SDL does not do -- telling the outputs apart, DPMS, the
 * colour range. Part of bitwall-paint.c, which includes it last
 * (platform.h), since SDL_syswm.h brings Xlib with it.
 */
#include <xf86drm.h>
#include <xf86drmMode.h>

/* SDL_syswm.h drags in Xlib, which has a Screen of its own and would
 * collide with ours. Only the KMS part of it is wanted, so theirs is
 * renamed on the way past. */
#define Screen X11Screen
#include "SDL_syswm.h"
#undef Screen

/* SDL up on `driver`, with no cursor on the glass. */
static int display_open(const char *driver)
{
    /* SDL would otherwise turn SIGTERM into an event nobody reads, and a
     * supervisor could no longer stop this process. */
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS, "1");
    setenv("SDL_VIDEODRIVER", driver, 1);

    if (SDL_Init(SDL_INIT_VIDEO) < 0) {
        fprintf(stderr, "bitwall-paint: SDL_Init: %s\n", SDL_GetError());
        return -1;
    }
    SDL_ShowCursor(SDL_DISABLE);
    return 0;
}

static void display_close(void)
{
    SDL_Quit();
}

static void display_size(Screen *s, int *w, int *h)
{
    SDL_GetWindowSize(s->win, w, h);
}

static SDL_Surface *display_surface(Screen *s)
{
    return SDL_GetWindowSurface(s->win);
}

/* A monitor's glass is the mode it shows, set when the painter starts
 * (`--mode`), and not a controller's to change. */
static int glass_offered(Screen *s)
{
    (void)s;
    return 0;
}

static void display_glass(Screen *s, int w, int h)
{
    (void)s;
    (void)w;
    (void)h;
}

static void display_push(Screen *s, const SDL_Rect *rects, int n)
{
    if (rects)
        SDL_UpdateWindowSurfaceRects(s->win, rects, n);
    else
        SDL_UpdateWindowSurface(s->win);
}

static void display_pump(void)
{
    SDL_PumpEvents();
}

/* KMS has nobody to answer and nothing to ask the painter. */
static int display_events(void)
{
    return 0;
}

static void display_drop(Screen *s)
{
    SDL_DestroyWindow(s->win);
}

/* Find the card and the output to put to sleep. SDL is the DRM master
 * here -- it opened the card -- so the fd is borrowed from it rather
 * than opened again, which a second open would not be allowed to do.
 *
 * The output is the connected one whose encoder is driving a CRTC, which
 * is the one showing a picture; a board with a second output plugged in
 * but idle would otherwise have its idle one put to sleep. A connected
 * output nothing drives is the choice only when there is no other. A
 * screen pinned to a connector looks at that one alone, because the
 * output being driven may be another wall's. */
static void power_find(Screen *s)
{
    SDL_SysWMinfo info;
    drmModeRes *res;
    uint32_t spare = 0, spare_dpms = 0;
    int fd, i, j;

    s->drm = -1;
    s->connector = 0;
    SDL_VERSION(&info.version);
    if (!SDL_GetWindowWMInfo(s->win, &info)
        || info.subsystem != SDL_SYSWM_KMSDRM)
        return;
    fd = info.info.kmsdrm.drm_fd;
    res = drmModeGetResources(fd);
    if (!res)
        return;
    for (i = 0; i < res->count_connectors && !s->connector; i++) {
        drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
        uint32_t dpms = 0;
        if (!c)
            continue;
        if (c->connection == DRM_MODE_CONNECTED && (!s->pin || c->connector_id == s->pin)) {
            for (j = 0; j < c->count_props && !dpms; j++) {
                drmModePropertyRes *pr = drmModeGetProperty(fd, c->props[j]);
                if (!pr)
                    continue;
                if (strcmp(pr->name, "DPMS") == 0)
                    dpms = pr->prop_id;
                drmModeFreeProperty(pr);
            }
        }
        if (dpms) {
            drmModeEncoder *e = c->encoder_id ? drmModeGetEncoder(fd, c->encoder_id) : NULL;
            if (e && e->crtc_id) {
                s->connector = c->connector_id;
                s->dpms = dpms;
            } else if (!spare) {
                spare = c->connector_id;
                spare_dpms = dpms;
            }
            if (e)
                drmModeFreeEncoder(e);
        }
        drmModeFreeConnector(c);
    }
    drmModeFreeResources(res);
    if (!s->connector && spare) {
        s->connector = spare;
        s->dpms = spare_dpms;
    }
    if (s->connector)
        s->drm = fd;
}

/* Wake the monitor, or put it to sleep. Best effort by design: a board
 * that will not do this still blanks, and the screen simply stays lit
 * and black. The output is sought each time until one is found, because
 * a monitor switched on after the wall came up is not there to find at
 * first. */
static void power(Screen *s, int on)
{
    int err;
    if (on == s->lit)
        return;
    if (!s->connector)
        power_find(s);
    s->lit = on;
    if (s->drm < 0 || !s->connector)
        return;
    err = drmModeConnectorSetProperty(s->drm, s->connector, s->dpms,
                                      on ? DRM_MODE_DPMS_ON : DRM_MODE_DPMS_OFF);
    if (err)
        fprintf(stderr, "bitwall-paint: DPMS %s on connector %u: %s\n",
                on ? "on" : "off", s->connector, strerror(err < 0 ? -err : err));
}

/* Send the output's colours over their whole range, 0 to 255. Left to
 * itself the kernel sends a TV mode like 1080p as limited range, 16 to
 * 235, and a monitor or projector that takes its input as full shows it
 * washed out: grey blacks and dull colours, while the picture of the
 * glass looks right. The AVI infoframe says which range is sent, so a
 * screen that reads it follows either way. Best effort: an output with
 * no `Broadcast RGB` keeps what it has. */
static void full_range(Screen *s)
{
    drmModeConnector *c;
    int i, j, err;

    if (s->drm < 0 || !s->connector)
        return;
    c = drmModeGetConnector(s->drm, s->connector);
    if (!c)
        return;
    for (i = 0; i < c->count_props; i++) {
        drmModePropertyRes *pr = drmModeGetProperty(s->drm, c->props[i]);
        if (!pr)
            continue;
        if (strcmp(pr->name, "Broadcast RGB") == 0 && (pr->flags & DRM_MODE_PROP_ENUM)) {
            for (j = 0; j < pr->count_enums; j++)
                if (strcmp(pr->enums[j].name, "Full") == 0)
                    break;
            if (j < pr->count_enums) {
                err = drmModeConnectorSetProperty(s->drm, s->connector, pr->prop_id,
                                                  pr->enums[j].value);
                if (err)
                    fprintf(stderr, "bitwall-paint: full range on connector %u: %s\n",
                            s->connector, strerror(err < 0 ? -err : err));
            }
        }
        drmModeFreeProperty(pr);
    }
    drmModeFreeConnector(c);
}

/* === screen_open === */
/* One screen: a window on SDL display `display`, and the GL path on it
 * or the software one. Which output it really is, and whether that
 * output is woken, is for walls_up to settle once every screen is
 * open. */
static int screen_open(Screen *s, int display, int want_w, int want_h)
{
    Uint32 flags = 0;
    int w = want_w, h = want_h, ndisp;

    /* No card found yet. */
    s->drm = -1;
    ndisp = SDL_GetNumVideoDisplays();
    if (display < 0 || display >= ndisp)
        display = 0;
    s->display = display;
    if (w <= 0 || h <= 0) {
        SDL_DisplayMode m;
        if (SDL_GetCurrentDisplayMode(display, &m) == 0 && m.w > 0 && m.h > 0) {
            w = m.w;
            h = m.h;
        } else {
            w = 1920;
            h = 1080;
        }
        flags |= SDL_WINDOW_FULLSCREEN;
    }

    /* A driver without GL, like the test driver, gets the software path
     * on a window made without it. */
    if (!getenv("BITWALL_PAINT_SOFTWARE")) {
        s->win = SDL_CreateWindow("bitwall",
                                  SDL_WINDOWPOS_UNDEFINED_DISPLAY(display),
                                  SDL_WINDOWPOS_UNDEFINED_DISPLAY(display),
                                  w, h, flags | SDL_WINDOW_OPENGL);
        if (s->win) {
            take_size(s);
            if (gl_setup(s) == 0) {
                s->gpu = 1;
                s->backend = gpu_name();
                take_size(s);
            } else {
                if (s->gl.ctx)
                    gpu_drop(s);
                memset(&s->gl, 0, sizeof s->gl);
                SDL_DestroyWindow(s->win);
                s->win = NULL;
            }
        }
    }
    if (!s->win) {
        s->win = SDL_CreateWindow("bitwall",
                                  SDL_WINDOWPOS_UNDEFINED_DISPLAY(display),
                                  SDL_WINDOWPOS_UNDEFINED_DISPLAY(display),
                                  w, h, flags);
        if (!s->win) {
            fprintf(stderr, "bitwall-paint: SDL_CreateWindow: %s\n", SDL_GetError());
            return -1;
        }
        take_size(s);
    }
    if (!s->gpu) {
        /* The software path: the picture is painted on the window surface
         * itself and presenting pushes out the rectangles that changed.
         * The paper only holds it while the glass is blank. */
        s->glass = SDL_GetWindowSurface(s->win);
        if (!s->glass) {
            fprintf(stderr, "bitwall-paint: SDL_GetWindowSurface: %s\n",
                    SDL_GetError());
            return -1;
        }
        s->paper = SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height,
                                                  s->glass->format->BitsPerPixel,
                                                  s->glass->format->format);
        if (!s->paper) {
            fprintf(stderr, "bitwall-paint: SDL_CreateRGBSurface: %s\n",
                    SDL_GetError());
            return -1;
        }
        SDL_SetSurfaceBlendMode(s->glass, SDL_BLENDMODE_NONE);
        SDL_SetSurfaceBlendMode(s->paper, SDL_BLENDMODE_NONE);
        s->target = s->glass;
        s->backend = "software";
    }
    if (s->turn && !s->gpu) {
        fprintf(stderr, "bitwall-paint: display %d: turning the picture needs the GPU; "
                "it is drawn as it comes\n", display);
        s->turn = 0;
    }
    fprintf(stderr, "bitwall-paint: %s driver, %s renderer, %dx%d on display %d%s%s\n",
            SDL_GetCurrentVideoDriver(), s->backend, s->width, s->height, display,
            s->turn ? ", turned " : "", s->turn == 90 ? "90" : s->turn == 180 ? "180"
            : s->turn == 270 ? "270" : "");
    return 0;
}

/* === Output === */
/* A connected output as the card lists it: its connector, the CRTC
 * driving it, and the framebuffer that CRTC is showing. */
typedef struct {
    uint32_t connector, crtc, fb;
    char name[32];
} Output;

/* === output_name === */
/* The kernel's name for a connector, as `HDMI-A-1`. */
static void output_name(const drmModeConnector *c, char *out, size_t max)
{
    static const char *kinds[] = {
        "Unknown", "VGA", "DVI-I", "DVI-D", "DVI-A", "Composite", "SVIDEO", "LVDS",
        "Component", "DIN", "DP", "HDMI-A", "HDMI-B", "TV", "eDP", "Virtual", "DSI",
        "DPI", "Writeback", "SPI", "USB"};
    uint32_t t = c->connector_type;
    snprintf(out, max, "%s-%u",
             t < sizeof kinds / sizeof kinds[0] ? kinds[t] : "Unknown",
             (unsigned)c->connector_type_id);
}

/* === outputs_read === */
/* Every connected output, in the order the card gives them, which is the
 * order walls are numbered in. */
static int outputs_read(int fd, Output *out, int max)
{
    drmModeRes *res = drmModeGetResources(fd);
    int i, n = 0;
    if (!res)
        return 0;
    for (i = 0; i < res->count_connectors && n < max; i++) {
        drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
        Output *o = &out[n];
        if (!c)
            continue;
        if (c->connection == DRM_MODE_CONNECTED) {
            memset(o, 0, sizeof *o);
            o->connector = c->connector_id;
            output_name(c, o->name, sizeof o->name);
            if (c->encoder_id) {
                drmModeEncoder *e = drmModeGetEncoder(fd, c->encoder_id);
                if (e) {
                    o->crtc = e->crtc_id;
                    drmModeFreeEncoder(e);
                }
            }
            if (o->crtc) {
                drmModeCrtc *k = drmModeGetCrtc(fd, o->crtc);
                if (k) {
                    o->fb = k->buffer_id;
                    drmModeFreeCrtc(k);
                }
            }
            n++;
        }
        drmModeFreeConnector(c);
    }
    drmModeFreeResources(res);
    return n;
}

/* === drm_of === */
/* The card, borrowed from SDL, or -1 off KMS. */
static int drm_of(Screen *s)
{
    SDL_SysWMinfo info;
    SDL_VERSION(&info.version);
    if (!SDL_GetWindowWMInfo(s->win, &info) || info.subsystem != SDL_SYSWM_KMSDRM)
        return -1;
    return info.info.kmsdrm.drm_fd;
}

/* === screen_flash === */
/* One black frame on the glass, which is what makes a window's CRTC take
 * up its framebuffer. */
static void screen_flash(Screen *s)
{
    if (s->gpu) {
        gl_use(s);
        glClearColor(0, 0, 0, 1);
        glClear(GL_COLOR_BUFFER_BIT);
        gpu_swap(s);
    } else {
        SDL_FillRect(s->glass, NULL, SDL_MapRGB(s->glass->format, 0, 0, 0));
        SDL_UpdateWindowSurface(s->win);
    }
}

/* === walls_up === */
/* Settle which output each screen is on and put the screens in wall
 * order, wall i on the i-th connected output; then wake them. SDL says
 * which of its displays a window is on, not which connector, and its
 * numbering is its own. So each window is asked in turn: it puts up a
 * frame, and the one output whose CRTC changed framebuffer is the one
 * it drives. A window that cannot be told apart that way is taken to be
 * on the output its display number would be, or the next nobody has.
 * Where SDL's order and the card's disagree the card's is used, and
 * that is said, because a wall that moved screen is a sender composing
 * for the wrong glass. `s` is in SDL's display order on the way in, and
 * in wall order on the way out. */
static void walls_up(Screen **s, int n)
{
    Output was[2 * MAX_WALLS], now[2 * MAX_WALLS];
    int at[MAX_WALLS], display[MAX_WALLS], taken[2 * MAX_WALLS];
    int fd = n > 0 ? drm_of(s[0]) : -1, m = 0, i, j;

    memset(taken, 0, sizeof taken);
    for (i = 0; i < n; i++) {
        int k = fd >= 0 ? outputs_read(fd, was, 2 * MAX_WALLS) : 0, changed = 0, found = -1;
        at[i] = -1;
        display[i] = s[i]->display;
        screen_flash(s[i]);
        if (fd < 0)
            continue;
        m = outputs_read(fd, now, 2 * MAX_WALLS);
        for (j = 0; j < m && k == m; j++)
            if (now[j].connector == was[j].connector && now[j].crtc && now[j].fb != was[j].fb) {
                changed++;
                found = j;
            }
        if (changed == 1 && !taken[found]) {
            at[i] = found;
            taken[found] = 1;
        }
    }
    for (i = 0; i < n; i++) {
        if (at[i] >= 0 || !m)
            continue;
        j = display[i] < m && !taken[display[i]] ? display[i] : 0;
        while (j < m && taken[j])
            j++;
        if (j == m)
            continue;
        at[i] = j;
        taken[j] = 1;
        fprintf(stderr, "bitwall-paint: warning: cannot tell which output display %d "
                "drives; taking it to be %s\n", display[i], now[j].name);
    }
    /* Into the card's order. A screen on no output at all goes last. */
    for (i = 1; i < n; i++) {
        Screen *t = s[i];
        int a = at[i], d = display[i];
        for (j = i; j > 0 && (a >= 0 ? a : 2 * MAX_WALLS + d)
                              < (at[j - 1] >= 0 ? at[j - 1] : 2 * MAX_WALLS + display[j - 1]); j--) {
            s[j] = s[j - 1];
            at[j] = at[j - 1];
            display[j] = display[j - 1];
        }
        s[j] = t;
        at[j] = a;
        display[j] = d;
    }
    for (i = 0; i < n; i++) {
        Screen *w = s[i];
        w->index = i;
        w->count = n;
        if (at[i] >= 0) {
            snprintf(w->output, sizeof w->output, "%s", now[at[i]].name);
            w->pin = n > 1 ? now[at[i]].connector : 0;
        } else {
            snprintf(w->output, sizeof w->output, "display %d", display[i]);
            w->pin = 0;
        }
        if (n > 1 && display[i] != i)
            fprintf(stderr, "bitwall-paint: warning: SDL display %d is on %s, which is wall %d "
                    "in the card's order; walls follow the card\n", display[i], w->output, i);
        fprintf(stderr, "bitwall-paint: wall %d: %s\n", i, w->output);
        /* Woken whatever it is thought to be: a painter that went before
         * may have left the monitor asleep, and nothing here would know. */
        w->lit = 0;
        power(w, 1);
        full_range(w);
    }
}

/* === walls_wanted === */
/* How many walls to drive, from `--walls`: -1 for auto, zero when it
 * was not given. One on stdin, which is a single stream. `--walls auto`
 * is one for every display SDL found, and a number is that many of them.
 * Without it, a named display is one wall on it, and otherwise auto is
 * the default on KMS and one wall anywhere else. */
static int walls_wanted(int walls, int display_given, int stdin_only, const char *driver)
{
    int ndisp = SDL_GetNumVideoDisplays(), n;
    if (stdin_only || (display_given && walls == 0))
        return 1;
    if (walls == 0)
        walls = strcmp(driver, "kmsdrm") == 0 ? -1 : 1;
    n = walls < 0 ? ndisp : walls;
    if (n > ndisp) {
        fprintf(stderr, "bitwall-paint: %d walls asked for and %d displays found\n", n, ndisp);
        n = ndisp;
    }
    if (n > MAX_WALLS)
        n = MAX_WALLS;
    return n < 1 ? 1 : n;
}

/* Set the connector's mode to the one nearest `w`x`h`, or stay fullscreen
 * at the current mode where nothing matches or the mode will not set. Only
 * on a real KMS driver; the test driver takes its size from the window it
 * was made with. */
static void apply_mode(Screen *s, int w, int h, const char *driver)
{
    SDL_DisplayMode want, got, cur;
    if (strcmp(driver, "kmsdrm") != 0 || w <= 0 || h <= 0)
        return;
    memset(&want, 0, sizeof want);
    want.w = w;
    want.h = h;
    if (!SDL_GetClosestDisplayMode(s->display, &want, &got)) {
        fprintf(stderr, "bitwall-paint: no mode near %dx%d on display %d; keeping the current mode\n",
                w, h, s->display);
    } else if (SDL_SetWindowDisplayMode(s->win, &got) != 0 ||
               SDL_SetWindowFullscreen(s->win, SDL_WINDOW_FULLSCREEN) != 0) {
        fprintf(stderr, "bitwall-paint: mode %dx%d did not set on display %d (%s); keeping the current mode\n",
                got.w, got.h, s->display, SDL_GetError());
    } else {
        take_size(s);
        return;
    }
    if (SDL_GetCurrentDisplayMode(s->display, &cur) == 0)
        SDL_SetWindowDisplayMode(s->win, &cur);
    SDL_SetWindowFullscreen(s->win, SDL_WINDOW_FULLSCREEN);
    take_size(s);
}
