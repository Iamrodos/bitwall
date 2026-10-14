/*
 * The fakes behind the stub headers. Enough behaviour to run the painter
 * for real -- surfaces hold pixels, the atlas holds masks, threads run --
 * and no more, so that a test failing means the painter is wrong rather
 * than the fake.
 */
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "SDL.h"
#include "SDL_syswm.h"
#include "SDL_opengles2.h"
#include "SDL_ttf.h"
#include "xf86drmMode.h"
#include "fake.h"

Fake fake;

/* The windows made so far. A window knows its display, and its surface
 * on the software path. */
struct SDL_Window {
    int display;
    int w, h;                   /* the size it was made with, or 0 */
    SDL_Surface *glass;
};

static struct SDL_Window windows[FAKE_CONTEXTS];
static int nwindows;

/* Pixel buffers' storage, by name, and the one bound to be read into. */
static uint8_t *pack_store[FAKE_BUFFERS];
static size_t pack_len[FAKE_BUFFERS];
static GLuint pack_bound;

void fake_reset(void)
{
    int i;
    for (i = 0; i < FAKE_TEXTURES; i++)
        free(fake.tex[i].pixels);
    for (i = 0; i < FAKE_BUFFERS; i++) {
        free(pack_store[i]);
        pack_store[i] = NULL;
        pack_len[i] = 0;
    }
    pack_bound = 0;
    memset(&fake, 0, sizeof fake);
    memset(windows, 0, sizeof windows);
    nwindows = 0;
    fake.max_texture = 2048;
    fake.max_point = 64;
    fake.win_w = 1920;
    fake.win_h = 1080;
    fake.kmsdrm = 1;
}

/* The standalone painter, built against these fakes for the contracts,
 * never calls fake_reset (that is the per-test harness's), so give the
 * fakes usable defaults at load: a GPU with an atlas, and a 1080p glass. */
__attribute__((constructor))
static void fake_init(void)
{
    fake_reset();
}

int fake_wide(uint32_t cp)
{
    return (cp >= 0x1100 && cp <= 0x115F) || (cp >= 0x2E80 && cp <= 0xA4CF)
        || (cp >= 0xAC00 && cp <= 0xD7A3) || (cp >= 0xFF00 && cp <= 0xFF60)
        || (cp >= 0x1F300 && cp <= 0x1FAFF);
}

const FakeTexture *fake_texture(unsigned name)
{
    return &fake.tex[name < FAKE_TEXTURES ? name : 0];
}

/* ------------------------------------------------------------ SDL core */

static void display_size(int d, int *w, int *h)
{
    *w = fake.win_w;
    *h = fake.win_h;
    if (d >= 0 && d < 4 && fake.disp[d].w > 0) {
        *w = fake.disp[d].w;
        *h = fake.disp[d].h;
    }
}

/* A window's size: the one it was made with, or the display's; and, when
 * BITWALL_FAKE_SIZE names a file holding WxH, that instead, so a test can
 * resize the glass under a running painter to try its area-change path. */
static void window_size(SDL_Window *win, int *w, int *h)
{
    const char *path = getenv("BITWALL_FAKE_SIZE");
    if (path) {
        FILE *f = fopen(path, "r");
        int fw = 0, fh = 0;
        if (f) {
            int got = fscanf(f, "%dx%d", &fw, &fh);
            fclose(f);
            if (got == 2 && fw > 0 && fh > 0) {
                *w = fw;
                *h = fh;
                return;
            }
        }
    }
    if (win && win->w > 0 && win->h > 0) {
        *w = win->w;
        *h = win->h;
        return;
    }
    display_size(win ? win->display : 0, w, h);
}

/* A frame reaching the output a window is on: its CRTC shows a new
 * framebuffer, which is how the painter tells which output that is. */
static void scan_out(SDL_Window *win)
{
    int on = win && win->display < 4 ? fake.disp[win->display].on : 0;
    if (on > 0 && on <= 4 && fake.conn[on - 1].crtc)
        fake.fb[fake.conn[on - 1].crtc & 15]++;
}

int SDL_Init(Uint32 flags) { (void)flags; return 0; }
void SDL_Quit(void) {}
const char *SDL_GetError(void) { return "fake"; }
SDL_bool SDL_SetHint(const char *n, const char *v) { (void)n; (void)v; return SDL_TRUE; }
int SDL_ShowCursor(int t) { (void)t; return 0; }
void SDL_PumpEvents(void) {}

void SDL_Delay(Uint32 ms)
{
    struct timespec t = {ms / 1000, (long)(ms % 1000) * 1000000L};
    nanosleep(&t, NULL);
}

int SDL_GetNumVideoDisplays(void) { return fake.ndisplays ? fake.ndisplays : 1; }

int SDL_GetCurrentDisplayMode(int d, SDL_DisplayMode *m)
{
    memset(m, 0, sizeof *m);
    display_size(d, &m->w, &m->h);
    return 0;
}

const char *SDL_GetCurrentVideoDriver(void) { return "fake"; }

SDL_Window *SDL_CreateWindow(const char *t, int x, int y, int w, int h, Uint32 f)
{
    SDL_Window *win;
    unsigned ux = (unsigned)x;
    if (nwindows == FAKE_CONTEXTS) {
        fprintf(stderr, "fake: more than %d windows\n", FAKE_CONTEXTS);
        abort();
    }
    win = &windows[nwindows++];
    (void)t; (void)y; (void)f;
    win->display = (ux & 0xFFFF0000u) == 0x1FFF0000u ? (int)(ux & 0xFFFF) : 0;
    win->w = w;
    win->h = h;
    return win;
}

void SDL_DestroyWindow(SDL_Window *w) { (void)w; }

void SDL_GetWindowSize(SDL_Window *win, int *w, int *h)
{
    window_size(win, w, h);
}

SDL_DisplayMode *SDL_GetClosestDisplayMode(int d, const SDL_DisplayMode *want,
                                           SDL_DisplayMode *got)
{
    (void)d;
    if (!want || !got)
        return NULL;
    *got = *want;
    return got;
}

int SDL_SetWindowDisplayMode(SDL_Window *win, const SDL_DisplayMode *m)
{
    if (win && m && m->w > 0 && m->h > 0) {
        win->w = m->w;
        win->h = m->h;
    }
    return 0;
}

int SDL_SetWindowFullscreen(SDL_Window *win, Uint32 flags)
{
    (void)win; (void)flags;
    return 0;
}

SDL_Surface *SDL_GetWindowSurface(SDL_Window *win)
{
    int w, h;
    if (win->glass)
        SDL_FreeSurface(win->glass);
    window_size(win, &w, &h);
    win->glass = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, SDL_PIXELFORMAT_ARGB8888);
    return win->glass;
}

int SDL_UpdateWindowSurface(SDL_Window *w) { scan_out(w); return 0; }

int SDL_UpdateWindowSurfaceRects(SDL_Window *w, const SDL_Rect *r, int n)
{
    (void)w; (void)r; (void)n;
    return 0;
}

SDL_bool SDL_GetWindowWMInfo(SDL_Window *win, SDL_SysWMinfo *info)
{
    (void)win;
    if (!fake.kmsdrm)
        return SDL_FALSE;
    info->subsystem = SDL_SYSWM_KMSDRM;
    info->info.kmsdrm.drm_fd = 7;
    return SDL_TRUE;
}

int SDL_GL_SetAttribute(SDL_GLattr a, int v) { (void)a; (void)v; return 0; }
/* The context a GL call goes to. None at all is a painter bug too. */
static FakeContext *current(void)
{
    if (!fake.current)
        fake.wrong_context++;
    return fake.current;
}

/* A GL object used while a context other than its maker's is current. */
static void owned_by(FakeContext *owner)
{
    if (owner != fake.current)
        fake.wrong_context++;
}

/* As SDL does it: a new context is made current. */
SDL_GLContext SDL_GL_CreateContext(SDL_Window *w)
{
    FakeContext *c;
    if (fake.nctx == FAKE_CONTEXTS) {
        fprintf(stderr, "fake: more than %d contexts\n", FAKE_CONTEXTS);
        abort();
    }
    c = &fake.ctx[fake.nctx++];
    c->win = w;
    fake.current = c;
    return c;
}

int SDL_GL_MakeCurrent(SDL_Window *w, SDL_GLContext ctx)
{
    FakeContext *c = ctx;
    if (c && c->win != w)
        fake.wrong_context++;
    fake.current = c;
    return 0;
}

void SDL_GL_DeleteContext(SDL_GLContext c)
{
    if (fake.current == c)
        fake.current = NULL;
}

int SDL_GL_SetSwapInterval(int i) { (void)i; current(); return 0; }

/* GLES 3's pixel buffer calls, there only where the fake says ES 3. */
static void *fake_map(GLenum t, ptrdiff_t off, ptrdiff_t len, GLbitfield access)
{
    (void)access;
    current();
    if (t != GL_PIXEL_PACK_BUFFER || !pack_bound || !pack_store[pack_bound]
        || off < 0 || (size_t)(off + len) > pack_len[pack_bound])
        return NULL;
    fake.maps++;
    return pack_store[pack_bound] + off;
}

static GLboolean fake_unmap(GLenum t)
{
    (void)t;
    current();
    return GL_TRUE;
}

void *SDL_GL_GetProcAddress(const char *name)
{
    if (!fake.es3)
        return NULL;
    if (!strcmp(name, "glMapBufferRange"))
        return (void *)fake_map;
    if (!strcmp(name, "glUnmapBuffer"))
        return (void *)fake_unmap;
    return NULL;
}

/* A swap puts up the back buffer of the context current, which has to be
 * the one made for this window. */
void SDL_GL_SwapWindow(SDL_Window *w)
{
    FakeContext *c = current();
    if (c && c->win != w)
        fake.wrong_context++;
    fake.swaps++;
    if (!c || !c->cleared_since_swap)
        fake.swaps_uncleared++;
    if (c) {
        c->cleared_since_swap = 0;
        c->swaps++;
    }
    scan_out(w);
}

/* ------------------------------------------------------------ surfaces */

/* A surface and its format in one allocation, 32 bits a pixel. */
typedef struct {
    SDL_Surface s;
    SDL_PixelFormat f;
    SDL_BlendMode blend;    /* how it goes onto another when blitted */
} Owned;

SDL_Surface *SDL_CreateRGBSurfaceWithFormat(Uint32 flags, int w, int h, int d, Uint32 format)
{
    Owned *o = calloc(1, sizeof *o);
    (void)flags; (void)d;
    if (!o)
        return NULL;
    o->f.format = format;
    o->f.BitsPerPixel = 32;
    o->f.BytesPerPixel = 4;
    o->f.Amask = 0xff000000u;
    o->f.Ashift = 24;
    o->s.format = &o->f;
    o->s.w = w;
    o->s.h = h;
    o->s.pitch = w * 4;
    o->s.pixels = calloc((size_t)(w > 0 ? w : 1) * (size_t)(h > 0 ? h : 1), 4);
    return &o->s;
}

SDL_Surface *SDL_ConvertSurface(SDL_Surface *src, const SDL_PixelFormat *f, Uint32 x)
{
    SDL_Surface *s = SDL_CreateRGBSurfaceWithFormat(0, src->w, src->h, 32, f->format);
    (void)x;
    if (s)
        memcpy(s->pixels, src->pixels, (size_t)src->pitch * src->h);
    return s;
}

void SDL_FreeSurface(SDL_Surface *s)
{
    if (s) {
        free(s->pixels);
        free(s);
    }
}

int SDL_LockSurface(SDL_Surface *s) { (void)s; return 0; }
void SDL_UnlockSurface(SDL_Surface *s) { (void)s; }

/* A rectangle cut to the surface, as SDL clips; false if nothing is left. */
static int clip(const SDL_Surface *s, SDL_Rect *r)
{
    int x1 = r->x + r->w, y1 = r->y + r->h;
    if (r->x < 0) r->x = 0;
    if (r->y < 0) r->y = 0;
    if (x1 > s->w) x1 = s->w;
    if (y1 > s->h) y1 = s->h;
    r->w = x1 - r->x;
    r->h = y1 - r->y;
    return r->w > 0 && r->h > 0;
}

int SDL_FillRects(SDL_Surface *dst, const SDL_Rect *rects, int n, Uint32 c)
{
    int i, x, y;
    for (i = 0; i < n; i++) {
        SDL_Rect r = rects[i];
        if (!clip(dst, &r))
            continue;
        for (y = r.y; y < r.y + r.h; y++) {
            Uint32 *row = (Uint32 *)((uint8_t *)dst->pixels + (size_t)y * dst->pitch);
            for (x = r.x; x < r.x + r.w; x++)
                row[x] = c;
        }
    }
    return 0;
}

int SDL_FillRect(SDL_Surface *dst, const SDL_Rect *r, Uint32 c)
{
    SDL_Rect all = {0, 0, dst->w, dst->h};
    return SDL_FillRects(dst, r ? r : &all, 1, c);
}

Uint32 SDL_MapRGB(const SDL_PixelFormat *f, Uint8 r, Uint8 g, Uint8 b)
{
    (void)f;
    return 0xff000000u | (Uint32)r << 16 | (Uint32)g << 8 | b;
}

int SDL_BlitSurface(SDL_Surface *src, const SDL_Rect *sr, SDL_Surface *dst, SDL_Rect *dr)
{
    SDL_Rect from = sr ? *sr : (SDL_Rect){0, 0, src->w, src->h};
    SDL_Rect to = {dr ? dr->x : 0, dr ? dr->y : 0, from.w, from.h};
    int x, y;
    if (!clip(src, &from))
        return 0;
    to.w = from.w;
    to.h = from.h;
    for (y = 0; y < to.h; y++)
        for (x = 0; x < to.w; x++) {
            int dx = to.x + x, dy = to.y + y;
            if (dx < 0 || dy < 0 || dx >= dst->w || dy >= dst->h)
                continue;
        {
            Uint32 *d = &((Uint32 *)((uint8_t *)dst->pixels + (size_t)dy * dst->pitch))[dx];
            Uint32 c = ((Uint32 *)((uint8_t *)src->pixels + (size_t)(from.y + y) * src->pitch))[from.x + x];
            if (((Owned *)src)->blend == SDL_BLENDMODE_BLEND) {
                /* ARGB over ARGB by the source's alpha, as SDL blends */
                Uint32 a = c >> 24, out = 0xff000000u;
                int sh;
                for (sh = 0; sh < 24; sh += 8) {
                    Uint32 sc = c >> sh & 0xff, dc = *d >> sh & 0xff;
                    out |= ((sc * a + dc * (255 - a) + 127) / 255) << sh;
                }
                c = out;
            }
            *d = c;
        }
        }
    return 0;
}

int SDL_SetSurfaceBlendMode(SDL_Surface *s, SDL_BlendMode m) { ((Owned *)s)->blend = m; return 0; }
int SDL_SaveBMP(SDL_Surface *s, const char *p) { (void)s; (void)p; return 0; }

/* ------------------------------------------------------------- threads */

struct SDL_mutex { pthread_mutex_t m; };
struct SDL_cond { pthread_cond_t c; };
struct SDL_Thread { pthread_t t; SDL_ThreadFunction fn; void *data; int status; };

SDL_mutex *SDL_CreateMutex(void)
{
    SDL_mutex *m = calloc(1, sizeof *m);
    if (m)
        pthread_mutex_init(&m->m, NULL);
    return m;
}

void SDL_DestroyMutex(SDL_mutex *m)
{
    if (m) {
        pthread_mutex_destroy(&m->m);
        free(m);
    }
}

int SDL_LockMutex(SDL_mutex *m) { return pthread_mutex_lock(&m->m); }
int SDL_UnlockMutex(SDL_mutex *m) { return pthread_mutex_unlock(&m->m); }

SDL_cond *SDL_CreateCond(void)
{
    SDL_cond *c = calloc(1, sizeof *c);
    if (c)
        pthread_cond_init(&c->c, NULL);
    return c;
}

int SDL_CondSignal(SDL_cond *c) { return pthread_cond_signal(&c->c); }
int SDL_CondBroadcast(SDL_cond *c) { return pthread_cond_broadcast(&c->c); }
int SDL_CondWait(SDL_cond *c, SDL_mutex *m) { return pthread_cond_wait(&c->c, &m->m); }

int SDL_CondWaitTimeout(SDL_cond *c, SDL_mutex *m, Uint32 ms)
{
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_sec += ms / 1000;
    t.tv_nsec += (long)(ms % 1000) * 1000000L;
    if (t.tv_nsec >= 1000000000L) {
        t.tv_sec++;
        t.tv_nsec -= 1000000000L;
    }
    return pthread_cond_timedwait(&c->c, &m->m, &t) ? 1 : 0;
}

static void *thread_main(void *arg)
{
    SDL_Thread *t = arg;
    t->status = t->fn(t->data);
    return NULL;
}

SDL_Thread *SDL_CreateThread(SDL_ThreadFunction fn, const char *name, void *data)
{
    SDL_Thread *t = calloc(1, sizeof *t);
    (void)name;
    if (!t)
        return NULL;
    t->fn = fn;
    t->data = data;
    if (pthread_create(&t->t, NULL, thread_main, t) != 0) {
        free(t);
        return NULL;
    }
    fake.threads++;
    return t;
}

void SDL_WaitThread(SDL_Thread *t, int *status)
{
    if (!t)
        return;
    pthread_join(t->t, NULL);
    if (status)
        *status = t->status;
    free(t);
}

void SDL_DetachThread(SDL_Thread *t)
{
    if (t)
        pthread_detach(t->t);
}

/* ---------------------------------------------------------------- font */

struct TTF_Font { int size; };

static uint32_t first_cp(const char *u8)
{
    const uint8_t *p = (const uint8_t *)u8;
    if (p[0] < 0x80) return p[0];
    if (p[0] < 0xE0) return (uint32_t)(p[0] & 0x1F) << 6 | (p[1] & 0x3F);
    if (p[0] < 0xF0) return (uint32_t)(p[0] & 0x0F) << 12 | (uint32_t)(p[1] & 0x3F) << 6 | (p[2] & 0x3F);
    return (uint32_t)(p[0] & 0x07) << 18 | (uint32_t)(p[1] & 0x3F) << 12
         | (uint32_t)(p[2] & 0x3F) << 6 | (p[3] & 0x3F);
}

int TTF_Init(void) { return 0; }
void TTF_Quit(void) {}

TTF_Font *TTF_OpenFont(const char *path, int size)
{
    TTF_Font *f = calloc(1, sizeof *f);
    (void)path;
    if (f)
        f->size = size;
    return f;
}

void TTF_CloseFont(TTF_Font *f) { free(f); }

int TTF_SizeUTF8(TTF_Font *f, const char *text, int *w, int *h)
{
    *w = (f->size / 2 + 1) * (fake_wide(first_cp(text)) ? 2 : 1);
    *h = f->size + 2;
    return 0;
}

int TTF_FontLineSkip(const TTF_Font *f) { return f->size + 2; }

static void stamp(SDL_Surface *s, int x, uint32_t cp, int tail)
{
    Uint32 *row = s->pixels;
    uint8_t mark[3] = {(uint8_t)cp, (uint8_t)(cp >> 8),
                       (uint8_t)(FAKE_MARK | (tail ? FAKE_TAIL : 0) | (cp >> 16 & 0x1F))};
    int i;
    for (i = 0; i < 3 && x + i < s->w; i++)
        row[x + i] = (Uint32)mark[i] << 24;
}

SDL_Surface *TTF_RenderUTF8_Blended(TTF_Font *f, const char *text, SDL_Color c)
{
    int w, h;
    uint32_t cp = first_cp(text);
    SDL_Surface *s;
    (void)c;
    TTF_SizeUTF8(f, text, &w, &h);
    s = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, SDL_PIXELFORMAT_ARGB8888);
    if (!s)
        return NULL;
    stamp(s, 0, cp, 0);
    if (fake_wide(cp))
        stamp(s, f->size / 2 + 1, cp, 1);
    return s;
}

SDL_Surface *TTF_RenderUTF8_Shaded(TTF_Font *f, const char *text, SDL_Color fg, SDL_Color bg)
{
    (void)bg;
    return TTF_RenderUTF8_Blended(f, text, fg);
}

int TTF_GlyphIsProvided32(TTF_Font *f, Uint32 cp) { (void)f; (void)cp; return !fake.fontless; }

/* ------------------------------------------------------------------ GL */

void glGetIntegerv(GLenum what, GLint *out)
{
    if (what == GL_MAX_TEXTURE_SIZE)
        *out = fake.max_texture;
}

void glGetFloatv(GLenum what, GLfloat *out)
{
    if (what == GL_ALIASED_POINT_SIZE_RANGE) {
        out[0] = 1;
        out[1] = fake.max_point;
    }
}

/* The texture bound in the current context, which a texture call acts on. */
static FakeTexture *bound(void)
{
    FakeContext *c = current();
    FakeTexture *t = &fake.tex[c ? c->texture : 0];
    owned_by(t->owner);
    return t;
}

void glTexImage2D(GLenum target, GLint level, GLint ifmt, GLsizei w, GLsizei h,
                  GLint border, GLenum fmt, GLenum type, const void *data)
{
    FakeTexture *t = bound();
    size_t bpp = fmt == GL_RGBA ? 4 : 1;    /* a picture, or a mask */
    (void)target; (void)level; (void)ifmt; (void)border; (void)type;
    free(t->pixels);
    t->pixels = calloc((size_t)(w > 0 ? w : 1) * (size_t)(h > 0 ? h : 1), bpp);
    t->w = w;
    t->h = h;
    t->bpp = (int)bpp;
    if (fmt == GL_ALPHA) {     /* a plane of a film is not the atlas */
        fake.atlas = t->pixels;
        fake.atlas_w = w;
        fake.atlas_h = h;
        fake.atlas_made++;
    }
    if (data && t->pixels)
        memcpy(t->pixels, data, (size_t)w * h * bpp);
}

/* A mask written outside the texture is a painter bug, and loud. */
void glTexSubImage2D(GLenum target, GLint level, GLint x, GLint y, GLsizei w,
                     GLsizei h, GLenum fmt, GLenum type, const void *data)
{
    const uint8_t *src = data;
    FakeTexture *t = bound();
    int r;
    (void)target; (void)level; (void)fmt; (void)type;
    if (!t->pixels || x < 0 || y < 0 || x + w > t->w || y + h > t->h) {
        fprintf(stderr, "fake: mask %dx%d at %d,%d is outside the %dx%d atlas\n",
                w, h, x, y, t->w, t->h);
        abort();
    }
    if (fake.current)
        fake.current->uploads++;
    for (r = 0; r < h; r++)
        memcpy(t->pixels + ((size_t)(y + r) * t->w + x) * t->bpp,
               src + (size_t)r * w * t->bpp, (size_t)w * t->bpp);
}

void glGetShaderiv(GLuint s, GLenum what, GLint *out) { (void)s; (void)what; *out = 1; }
void glGetProgramiv(GLuint p, GLenum what, GLint *out) { (void)p; (void)what; *out = 1; }
GLuint glCreateShader(GLenum kind) { (void)kind; return 1; }
GLuint glCreateProgram(void)
{
    GLuint p = (GLuint)(fake.nprograms < FAKE_PROGRAMS - 1 ? ++fake.nprograms : FAKE_PROGRAMS - 1);
    fake.program_owner[p] = current();
    return p;
}
GLenum glGetError(void) { return GL_NO_ERROR; }
const GLubyte *glGetString(GLenum name)
{
    current();
    if (name != GL_VERSION)
        return (const GLubyte *)"";
    return (const GLubyte *)(fake.es3 ? "OpenGL ES 3.1 fake" : "OpenGL ES 2.0 fake");
}
GLint glGetAttribLocation(GLuint p, const GLchar *n) { (void)p; (void)n; return 0; }
GLint glGetUniformLocation(GLuint p, const GLchar *n) { (void)p; (void)n; return 0; }

void glGenBuffers(GLsizei n, GLuint *out)
{
    FakeContext *c = current();
    while (n-- > 0) {
        out[n] = (GLuint)(fake.nbuffers < FAKE_BUFFERS - 1 ? ++fake.nbuffers : FAKE_BUFFERS - 1);
        fake.buffer_owner[out[n]] = c;
    }
}

void glGenTextures(GLsizei n, GLuint *out)
{
    FakeContext *c = current();
    while (n-- > 0) {
        out[n] = (GLuint)(fake.ntex < FAKE_TEXTURES - 1 ? ++fake.ntex : FAKE_TEXTURES - 1);
        fake.tex[out[n]].owner = c;
        fake.tex[out[n]].local = c ? ++c->ntex : 0;
    }
}

/* A texture let go: its pixels freed and its name never handed out again,
 * so a name used after it is gone reads as empty. */
void glDeleteTextures(GLsizei n, const GLuint *names)
{
    GLsizei i;
    current();
    for (i = 0; i < n; i++) {
        FakeTexture *t;
        if (!names[i] || names[i] >= FAKE_TEXTURES)
            continue;
        t = &fake.tex[names[i]];
        owned_by(t->owner);
        free(t->pixels);
        t->pixels = NULL;
        t->w = t->h = 0;
        fake.tex_deleted++;
    }
}

/* What texture name `x` means in the current context: itself if this
 * context made it, and otherwise this context's own texture with the
 * same local number, made on the spot if there is none, as binding an
 * unknown name does in GLES2. */
static unsigned resolve(unsigned x)
{
    unsigned i, local;
    if (!x || x >= FAKE_TEXTURES || fake.tex[x].owner == fake.current)
        return x < FAKE_TEXTURES ? x : 0;
    fake.wrong_context++;
    local = fake.tex[x].local;
    for (i = 1; i <= (unsigned)fake.ntex; i++)
        if (fake.tex[i].owner == fake.current && fake.tex[i].local == local)
            return i;
    if (fake.ntex == FAKE_TEXTURES - 1)
        return 0;
    i = (unsigned)++fake.ntex;
    fake.tex[i].owner = fake.current;
    fake.tex[i].local = local;
    return i;
}

void glActiveTexture(GLenum t) { (void)t; }

/* Framebuffers: made, bound and always complete, drawing nowhere. */
void glGenFramebuffers(GLsizei n, GLuint *out)
{
    static GLuint next = 1;
    GLsizei i;
    current();
    for (i = 0; i < n; i++)
        out[i] = next++;
    fake.framebuffers += n;
}
void glDeleteFramebuffers(GLsizei n, const GLuint *f)
{
    (void)f;
    current();
    fake.framebuffers -= n;
}
void glBindFramebuffer(GLenum t, GLuint f) { (void)t; current(); fake.framebuffer = f; }
void glFramebufferTexture2D(GLenum t, GLenum a, GLenum tt, GLuint x, GLint l)
{
    (void)t; (void)a; (void)tt; (void)x; (void)l;
    current();
}
GLenum glCheckFramebufferStatus(GLenum t) { (void)t; return GL_FRAMEBUFFER_COMPLETE; }
void glAttachShader(GLuint p, GLuint s) { (void)p; (void)s; }
void glBindBuffer(GLenum t, GLuint b)
{
    current();
    if (t == GL_PIXEL_PACK_BUFFER)
        pack_bound = b < FAKE_BUFFERS ? b : 0;
    if (b)
        owned_by(fake.buffer_owner[b < FAKE_BUFFERS ? b : 0]);
}

void glBindTexture(GLenum t, GLuint x)
{
    FakeContext *c = current();
    (void)t;
    if (c)
        c->texture = resolve(x);
}
void glBufferData(GLenum t, GLsizeiptr n, const void *d, GLenum u)
{
    (void)d; (void)u;
    current();
    if (t == GL_PIXEL_PACK_BUFFER && pack_bound) {
        free(pack_store[pack_bound]);
        pack_store[pack_bound] = calloc((size_t)(n > 0 ? n : 1), 1);
        pack_len[pack_bound] = pack_store[pack_bound] ? (size_t)n : 0;
    }
}
void glClear(GLbitfield m)
{
    FakeContext *c = current();
    (void)m;
    fake.clears++;
    if (c) {
        c->clears++;
        c->cleared_since_swap = 1;
    }
}
void glClearColor(GLfloat r, GLfloat g, GLfloat b, GLfloat a) { (void)r; (void)g; (void)b; (void)a; current(); }
void glCompileShader(GLuint s) { (void)s; }
void glDeleteProgram(GLuint p) { (void)p; }
void glDeleteShader(GLuint s) { (void)s; }
void glDisable(GLenum c)
{
    FakeContext *x = current();
    if (x && c == GL_BLEND)
        x->blend = 0;
}
void glEnable(GLenum c)
{
    FakeContext *x = current();
    if (x && c == GL_BLEND)
        x->blend = 1;
}
void glBlendFunc(GLenum s, GLenum d) { (void)s; (void)d; current(); }
/* A draw samples the texture bound, which has to be this context's. */
static void draw(void)
{
    FakeContext *c = current();
    if (c) {
        c->draws++;
        c->blended += c->blend;
        owned_by(fake.tex[c->texture].owner);
    }
    if (fake.framebuffer)
        fake.fb_draws++;
    else
        fake.window_draws++;
}

void glDrawArrays(GLenum m, GLint f, GLsizei n) { (void)m; (void)f; (void)n; draw(); }
void glDrawElements(GLenum m, GLsizei n, GLenum t, const void *i) { (void)m; (void)n; (void)t; (void)i; draw(); }
void glEnableVertexAttribArray(GLuint a) { (void)a; }
void glDisableVertexAttribArray(GLuint a) { (void)a; }
void glDeleteBuffers(GLsizei n, const GLuint *b)
{
    GLsizei i;
    for (i = 0; i < n; i++)
        if (b[i] && b[i] < FAKE_BUFFERS) {
            free(pack_store[b[i]]);
            pack_store[b[i]] = NULL;
            pack_len[b[i]] = 0;
        }
}
void glGetProgramInfoLog(GLuint p, GLsizei m, GLsizei *n, GLchar *l) { (void)p; (void)m; (void)n; l[0] = 0; }
void glGetShaderInfoLog(GLuint s, GLsizei m, GLsizei *n, GLchar *l) { (void)s; (void)m; (void)n; l[0] = 0; }
void glLinkProgram(GLuint p) { (void)p; }
void glPixelStorei(GLenum w, GLint v) { (void)w; (void)v; }
/* Black, or under read_pattern a value from each byte's place; into the
 * pixel buffer bound, at the offset given, where one is. */
void glReadPixels(GLint x, GLint y, GLsizei w, GLsizei h, GLenum f, GLenum t, void *o)
{
    uint8_t *to = o;
    size_t n = (size_t)w * h * 4, i;
    (void)x; (void)y; (void)f; (void)t;
    current();
    fake.reads++;
    fake.read_bytes += (long)n;
    if (pack_bound) {
        size_t off = (size_t)(uintptr_t)o;
        if (!pack_store[pack_bound] || off + n > pack_len[pack_bound]) {
            fprintf(stderr, "fake: a read of %zu bytes past its pixel buffer\n", n);
            abort();
        }
        to = pack_store[pack_bound] + off;
        fake.read_into_buffer++;
    }
    if (!fake.read_pattern) {
        memset(to, 0, n);
        return;
    }
    for (i = 0; i < n; i++)
        to[i] = read_byte(i % ((size_t)w * 4), i / ((size_t)w * 4));
}
void glShaderSource(GLuint s, GLsizei n, const GLchar *const *t, const GLint *l) { (void)s; (void)n; (void)t; (void)l; }
void glTexParameteri(GLenum t, GLenum w, GLint v) { (void)t; (void)w; (void)v; }
void glUniform1f(GLint a, GLfloat v) { (void)a; (void)v; }
void glUniform1i(GLint a, GLint v) { (void)a; (void)v; }
void glUniform2f(GLint a, GLfloat x, GLfloat y) { (void)a; (void)x; (void)y; }
void glUniform4f(GLint a, GLfloat x, GLfloat y, GLfloat z, GLfloat w)
{
    (void)a; (void)x; (void)y; (void)z; (void)w;
}
void glUseProgram(GLuint p)
{
    current();
    if (p)
        owned_by(fake.program_owner[p < FAKE_PROGRAMS ? p : 0]);
}
void glVertexAttribPointer(GLuint a, GLint n, GLenum t, GLboolean m, GLsizei s, const void *p)
{
    (void)a; (void)n; (void)t; (void)m; (void)s; (void)p;
}
void glViewport(GLint x, GLint y, GLsizei w, GLsizei h) { (void)x; (void)y; (void)w; (void)h; current(); }

/* ----------------------------------------------------------------- DRM */

static drmModeRes res;
static uint32_t ids[4];
static drmModeConnector conns[4];
static drmModeEncoder encs[4];
static uint32_t props[4][2];
static drmModePropertyRes prop;

drmModeRes *drmModeGetResources(int fd)
{
    int i;
    (void)fd;
    if (!fake.nconnectors)
        return NULL;
    for (i = 0; i < fake.nconnectors; i++)
        ids[i] = fake.conn[i].id;
    res.count_connectors = fake.nconnectors;
    res.connectors = ids;
    return &res;
}

void drmModeFreeResources(drmModeRes *r) { (void)r; }

drmModeConnector *drmModeGetConnector(int fd, uint32_t id)
{
    int i;
    (void)fd;
    for (i = 0; i < fake.nconnectors; i++) {
        const FakeConnector *f = &fake.conn[i];
        drmModeConnector *c = &conns[i];
        if (f->id != id)
            continue;
        c->connector_id = f->id;
        c->encoder_id = f->encoder;
        c->connector_type = DRM_MODE_CONNECTOR_HDMIA;
        c->connector_type_id = f->type_id ? f->type_id : (uint32_t)i + 1;
        c->connection = f->connected ? DRM_MODE_CONNECTED : DRM_MODE_DISCONNECTED;
        props[i][0] = 1;                    /* EDID */
        props[i][1] = 100 + (uint32_t)i;    /* DPMS, when it has one */
        c->props = props[i];
        c->count_props = f->dpms ? 2 : 1;
        return c;
    }
    return NULL;
}

void drmModeFreeConnector(drmModeConnector *c) { (void)c; }

drmModeEncoder *drmModeGetEncoder(int fd, uint32_t id)
{
    int i;
    (void)fd;
    for (i = 0; i < fake.nconnectors; i++)
        if (id && fake.conn[i].encoder == id) {
            encs[i].encoder_id = id;
            encs[i].crtc_id = fake.conn[i].crtc;
            return &encs[i];
        }
    return NULL;
}

void drmModeFreeEncoder(drmModeEncoder *e) { (void)e; }

drmModeCrtc *drmModeGetCrtc(int fd, uint32_t id)
{
    static drmModeCrtc crtc;
    (void)fd;
    if (!id)
        return NULL;
    crtc.crtc_id = id;
    crtc.buffer_id = fake.fb[id & 15];
    return &crtc;
}

void drmModeFreeCrtc(drmModeCrtc *c) { (void)c; }

drmModePropertyRes *drmModeGetProperty(int fd, uint32_t id)
{
    (void)fd;
    prop.prop_id = id;
    snprintf(prop.name, sizeof prop.name, "%s", id >= 100 ? "DPMS" : "EDID");
    return &prop;
}

void drmModeFreeProperty(drmModePropertyRes *p) { (void)p; }

int drmModeConnectorSetProperty(int fd, uint32_t connector, uint32_t p, uint64_t value)
{
    int i;
    (void)fd; (void)p;
    for (i = 0; i < fake.nconnectors; i++)
        if (fake.conn[i].id == connector) {
            fake.conn[i].sets++;
            fake.conn[i].value = value;
        }
    fake.set_calls++;
    fake.set_connector = connector;
    fake.set_value = value;
    return fake.set_result;
}

/* The machine is a Pi 3's: four cores and 1 GB. */
int SDL_GetCPUCount(void) { return 4; }
int SDL_GetSystemRAM(void) { return 1024; }
