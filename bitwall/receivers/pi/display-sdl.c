/*
 * The display on a desktop: a window, through SDL, or no screen at all.
 *
 * In a window the glass is the window's drawable, in real pixels where
 * the screen is a dense one, and the window is answered every pass of the
 * loop, as a desktop expects of a program it shows. Closing it stops the
 * painter.
 *
 * With no screen, `--driver none` or `--glass WxH`, the glass is the size
 * it was given and nothing shows it: the GPU's context has no window
 * (gpu-gl.c), the software path paints a surface of its own, and the
 * picture is drawn only when something looks at it, a stream or a
 * picture. Blank, doze and power have nothing to act on and only keep
 * their state, which is still said; presence is followed as anywhere.
 * Frames are paced as they always are, by the senders and the wall's
 * clock, never by a display.
 *
 * Part of bitwall-paint.c, which includes it (platform.h).
 */

/* Whether this painter has no screen. */
static int no_screen;

static int display_open(const char *driver)
{
    /* SDL would otherwise turn SIGTERM into an event, and a supervisor
     * could no longer stop this process. */
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS, "1");
    if (strcmp(driver, "none") == 0) {
        no_screen = 1;
        return 0;
    }
    setenv("SDL_VIDEODRIVER", driver, 1);
    if (SDL_Init(SDL_INIT_VIDEO) < 0) {
        fprintf(stderr, "bitwall-paint: SDL_Init: %s\n", SDL_GetError());
        return -1;
    }
    return 0;
}

static void display_close(void)
{
    SDL_Quit();
}

/* With no screen, one wall unless more are asked for. In windows, one
 * unless `--walls` says, `auto` being a window on every display. */
static int walls_wanted(int walls, int display_given, int stdin_only, const char *driver)
{
    int ndisp = no_screen ? MAX_WALLS : SDL_GetNumVideoDisplays(), n;
    (void)driver;
    if (stdin_only || (display_given && walls == 0) || walls == 0)
        return 1;
    n = walls < 0 ? (no_screen ? 1 : ndisp) : walls;
    if (n > ndisp) {
        fprintf(stderr, "bitwall-paint: %d walls asked for and %d displays found\n", n, ndisp);
        n = ndisp;
    }
    if (n > MAX_WALLS)
        n = MAX_WALLS;
    return n < 1 ? 1 : n;
}

static void display_size(Screen *s, int *w, int *h)
{
    if (!s->win) {
        /* No screen: the glass is the size it was last asked to be. */
        *w = s->ask_w;
        *h = s->ask_h;
    } else if (s->gpu || s->gl.ctx) {
        SDL_GL_GetDrawableSize(s->win, w, h);
    } else {
        SDL_GetWindowSize(s->win, w, h);
    }
}

/* With no screen, the software path's glass is a surface of its own,
 * made again when the glass is another size. */
static SDL_Surface *display_surface(Screen *s)
{
    if (s->win)
        return SDL_GetWindowSurface(s->win);
    if (s->glass && (s->glass->w != s->width || s->glass->h != s->height)) {
        SDL_Surface *was = s->glass;
        s->glass = SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height, 32,
                                                  SDL_PIXELFORMAT_ARGB8888);
        if (!s->glass) {
            fprintf(stderr, "bitwall-paint: the glass's surface: %s\n", SDL_GetError());
            exit(1);
        }
        SDL_FreeSurface(was);
    }
    return s->glass;
}

static int glass_offered(Screen *s)
{
    (void)s;
    return 1;
}

/* In a window the size asked is real pixels, as the glass is said, and
 * the window is sized in the desktop's points: the two differ by the
 * screen's density, read off the window as it is. */
static void display_glass(Screen *s, int w, int h)
{
    int pw = 0, ph = 0, dw = 0, dh = 0;
    if (!s->win) {
        s->ask_w = w;
        s->ask_h = h;
        return;
    }
    SDL_GetWindowSize(s->win, &pw, &ph);
    display_size(s, &dw, &dh);
    if (pw < 1 || ph < 1 || dw < 1 || dh < 1)
        return;
    SDL_SetWindowSize(s->win, (int)(((long)w * pw + dw / 2) / dw),
                      (int)(((long)h * ph + dh / 2) / dh));
}

static void display_push(Screen *s, const SDL_Rect *rects, int n)
{
    if (!s->win)
        return;
    if (rects)
        SDL_UpdateWindowSurfaceRects(s->win, rects, n);
    else
        SDL_UpdateWindowSurface(s->win);
}

static void display_pump(void)
{
    if (!no_screen)
        SDL_PumpEvents();
}

static int display_events(void)
{
    SDL_Event e;
    int quit = 0;
    if (no_screen)
        return 0;
    while (SDL_PollEvent(&e))
        if (e.type == SDL_QUIT)
            quit = 1;
    return quit;
}

/* Nothing to wake or put to sleep: the state is kept, and said. */
static void power(Screen *s, int on)
{
    s->lit = on;
}

static void display_drop(Screen *s)
{
    SDL_DestroyWindow(s->win);
}

/* The software path's glass and paper, the size of the glass; the glass
 * is the window's surface, or with no screen one of its own. */
static int soft_open(Screen *s)
{
    s->glass = s->win ? SDL_GetWindowSurface(s->win)
                      : SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height, 32,
                                                       SDL_PIXELFORMAT_ARGB8888);
    if (!s->glass) {
        fprintf(stderr, "bitwall-paint: the glass's surface: %s\n", SDL_GetError());
        return -1;
    }
    s->paper = SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height,
                                              s->glass->format->BitsPerPixel,
                                              s->glass->format->format);
    if (!s->paper) {
        fprintf(stderr, "bitwall-paint: SDL_CreateRGBSurface: %s\n", SDL_GetError());
        return -1;
    }
    SDL_SetSurfaceBlendMode(s->glass, SDL_BLENDMODE_NONE);
    SDL_SetSurfaceBlendMode(s->paper, SDL_BLENDMODE_NONE);
    s->target = s->glass;
    s->backend = "software";
    return 0;
}

/* A window `want_w` by `want_h` real pixels, on display `display`: made
 * in the desktop's points, and made again at the size that gives that
 * many pixels where the screen has more than one to a point. */
static SDL_Window *window_make(int display, int want_w, int want_h, Uint32 flags)
{
    SDL_Window *win = SDL_CreateWindow("bitwall", SDL_WINDOWPOS_CENTERED_DISPLAY(display),
                                       SDL_WINDOWPOS_CENTERED_DISPLAY(display), want_w, want_h,
                                       flags | SDL_WINDOW_ALLOW_HIGHDPI | SDL_WINDOW_RESIZABLE);
    int dw = 0, dh = 0;
    if (!win || !(flags & SDL_WINDOW_OPENGL))
        return win;
    SDL_GL_GetDrawableSize(win, &dw, &dh);
    if (dw > 0 && dh > 0 && (dw != want_w || dh != want_h))
        SDL_SetWindowSize(win, (int)((long)want_w * want_w / dw),
                          (int)((long)want_h * want_h / dh));
    return win;
}

/* One screen: a window on SDL display `display`, or with no screen a
 * glass `want_w` by `want_h`; the GL path on it, or the software one. */
static int screen_open(Screen *s, int display, int want_w, int want_h)
{
    int ndisp;
    s->drm = -1;
    s->display = 0;
    if (want_w <= 0 || want_h <= 0) {
        want_w = 1920;
        want_h = 1080;
    }
    if (no_screen) {
        s->phys_w = s->ask_w = want_w;
        s->phys_h = s->ask_h = want_h;
        s->turn = 0;
        take_size(s);
        if (!getenv("BITWALL_PAINT_SOFTWARE") && gl_setup(s) == 0) {
            s->gpu = 1;
            s->backend = gpu_name();
        } else {
            if (s->gl.ctx)
                gpu_drop(s);
            memset(&s->gl, 0, sizeof s->gl);
            if (soft_open(s) < 0)
                return -1;
        }
        fprintf(stderr, "bitwall-paint: no screen, %s renderer, %dx%d\n",
                s->backend, s->width, s->height);
        return 0;
    }
    ndisp = SDL_GetNumVideoDisplays();
    if (display < 0 || display >= ndisp)
        display = 0;
    s->display = display;
    if (!getenv("BITWALL_PAINT_SOFTWARE")) {
        s->win = window_make(display, want_w, want_h, SDL_WINDOW_OPENGL);
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
        s->win = window_make(display, want_w, want_h, 0);
        if (!s->win) {
            fprintf(stderr, "bitwall-paint: SDL_CreateWindow: %s\n", SDL_GetError());
            return -1;
        }
        take_size(s);
        if (soft_open(s) < 0)
            return -1;
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

/* A window is the size it was made; a desktop's mode is not the
 * painter's to change. */
static void apply_mode(Screen *s, int w, int h, const char *driver)
{
    (void)s;
    (void)w;
    (void)h;
    (void)driver;
}

/* Walls in the order they were opened, each a window or no screen, each
 * awake. */
static void walls_up(Screen **s, int n)
{
    int i;
    for (i = 0; i < n; i++) {
        Screen *w = s[i];
        w->index = i;
        w->count = n;
        w->pin = 0;
        if (!w->win)
            snprintf(w->output, sizeof w->output, "none");
        else
            snprintf(w->output, sizeof w->output, "window-%d", w->display);
        fprintf(stderr, "bitwall-paint: wall %d: %s\n", i, w->output);
        power(w, 1);
    }
}
