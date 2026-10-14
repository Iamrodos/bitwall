/*
 * The GPU context on a desktop: OpenGL 2.1, the legacy context, which
 * takes the painter's GLES 2 shaders unchanged once their precision words
 * are defined away, and draws them to the same pixels. On a window it is
 * SDL's context; with no screen it is a CGL context of its own, which
 * wants no window and no application, and the glass is a framebuffer of
 * the context's. Part of bitwall-paint.c, which includes it (platform.h).
 */
#define GL_SILENCE_DEPRECATION
#include <dlfcn.h>
#include <OpenGL/OpenGL.h>

/* What desktop GL has and GLES 2's header does not name. */
#define GL_PROGRAM_POINT_SIZE_ 0x8642
#define GL_POINT_SPRITE_ 0x8861
#define GL_READ_ONLY_ 0x88B8

static const char *gpu_name(void)
{
    return "gl2.1";
}

static void gpu_attributes(void)
{
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_COMPATIBILITY);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 1);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
}

/* GLSL 1.20 has no precision statement and refuses the word, so it and
 * the qualifiers are defined away; `precision mediump float;` is left as
 * `float;`, an empty declaration, which is legal. */
static const char *gpu_preamble(void)
{
    return "#version 120\n#define precision\n#define lowp\n#define mediump\n#define highp\n";
}

/* A CGL context on the Mac's own GPU, with no window and no drawable. */
static int gpu_context_none(Screen *s)
{
    CGLPixelFormatAttribute at[] = {
        kCGLPFAOpenGLProfile, (CGLPixelFormatAttribute)kCGLOGLPVersion_Legacy,
        kCGLPFAAccelerated, kCGLPFAColorSize, (CGLPixelFormatAttribute)24,
        kCGLPFAAlphaSize, (CGLPixelFormatAttribute)8, (CGLPixelFormatAttribute)0};
    CGLPixelFormatObj pf = NULL;
    CGLContextObj ctx = NULL;
    GLint n = 0;
    CGLError e = CGLChoosePixelFormat(at, &pf, &n);
    if (e || !pf) {
        fprintf(stderr, "bitwall-paint: CGL: %s\n", CGLErrorString(e));
        return -1;
    }
    e = CGLCreateContext(pf, NULL, &ctx);
    CGLDestroyPixelFormat(pf);
    if (e || !ctx) {
        fprintf(stderr, "bitwall-paint: CGL: %s\n", CGLErrorString(e));
        return -1;
    }
    s->gl.ctx = (SDL_GLContext)ctx;
    CGLSetCurrentContext(ctx);
    return 0;
}

/* The glass where there is no window: a framebuffer of the context's own,
 * the glass's size, which every pass that would draw on a screen draws
 * into instead. */
static int glass_fb_make(Screen *s)
{
    Gl *g = &s->gl;
    glGenTextures(1, &g->glass_tex);
    glGenFramebuffers(1, &g->glass_fb);
    glBindTexture(GL_TEXTURE_2D, g->glass_tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, s->phys_w, s->phys_h, 0, GL_RGBA,
                 GL_UNSIGNED_BYTE, NULL);
    glBindFramebuffer(GL_FRAMEBUFFER, g->glass_fb);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           g->glass_tex, 0);
    glBindTexture(GL_TEXTURE_2D, 0);
    return glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE ? 0 : -1;
}

/* What a desktop context needs, once it is current, to behave as GLES 2
 * does: points sized by the shader, with gl_PointCoord across them, and
 * rows read back packed whatever their width. */
static int gpu_context(Screen *s)
{
    if (s->win) {
        s->gl.ctx = SDL_GL_CreateContext(s->win);
        if (!s->gl.ctx)
            return -1;
        gl_use(s);
        SDL_GL_SetSwapInterval(0);
    } else if (gpu_context_none(s) < 0 || glass_fb_make(s) < 0) {
        return -1;
    }
    glEnable(GL_PROGRAM_POINT_SIZE_);
    glEnable(GL_POINT_SPRITE_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    return 0;
}

static void gl_use(Screen *s)
{
    if (!s->gl.ctx)
        return;
    if (s->win)
        SDL_GL_MakeCurrent(s->win, s->gl.ctx);
    else if (CGLGetCurrentContext() != (CGLContextObj)s->gl.ctx)
        CGLSetCurrentContext((CGLContextObj)s->gl.ctx);
}

static GLuint gpu_glass_fb(Screen *s)
{
    return s->gl.glass_fb;
}

/* The buffer is made a power of two bytes and the vertices put in the
 * front of it. A Mac's GL keeps storage for every size a buffer has been
 * given, so a grid given its own size each time grew the painter as the
 * glass changed size, 40 MB over 200 sizes; in powers of two there are a
 * handful of sizes, and it grows no more once it has drawn the largest. */
static void gpu_vertices(const void *verts, size_t bytes)
{
    size_t room = 4096;
    while (room < bytes)
        room <<= 1;
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)room, NULL, GL_STREAM_DRAW);
    glBufferSubData(GL_ARRAY_BUFFER, 0, (GLsizeiptr)bytes, verts);
}

/* With no screen the glass's texture is given its new size in place: the
 * same texture and framebuffer, so nothing is made that is not let go. */
static int gpu_resized(Screen *s)
{
    if (s->win)
        return 0;
    gl_use(s);
    glBindTexture(GL_TEXTURE_2D, s->gl.glass_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, s->phys_w, s->phys_h, 0, GL_RGBA,
                 GL_UNSIGNED_BYTE, NULL);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    glBindFramebuffer(GL_FRAMEBUFFER, s->gl.glass_fb);
    return glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE ? 0 : -1;
}

static void gpu_swap(Screen *s)
{
    if (s->win)
        SDL_GL_SwapWindow(s->win);
    else
        glFlush();
}

static void gpu_drop(Screen *s)
{
    if (s->win) {
        SDL_GL_DeleteContext(s->gl.ctx);
        return;
    }
    gl_use(s);
    glDeleteFramebuffers(1, &s->gl.glass_fb);
    glDeleteTextures(1, &s->gl.glass_tex);
    CGLSetCurrentContext(NULL);
    CGLDestroyContext((CGLContextObj)s->gl.ctx);
}

/* OpenGL.framework exports every entry point it has, so they are looked
 * up in the process, which needs no SDL video and so works with no
 * screen. */
static void *gpu_proc(const char *name)
{
    return dlsym(RTLD_DEFAULT, name);
}

static void *(*map_buffer)(GLenum, GLenum);

/* Legacy GL maps a whole buffer; the painter maps the whole of one. */
static void *map_read(GLenum target, ptrdiff_t at, ptrdiff_t len, GLbitfield access)
{
    (void)at;
    (void)len;
    (void)access;
    return map_buffer(target, GL_READ_ONLY_);
}

/* Pixel buffers are GL 2.1's own: a frame is read into one and collected
 * a frame later, mapped whole, so the draw never waits for the read. */
static int gpu_read_later(void *(**map)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield),
                          GLboolean (**unmap)(GLenum))
{
    map_buffer = (void *(*)(GLenum, GLenum))gpu_proc("glMapBuffer");
    *unmap = (GLboolean (*)(GLenum))gpu_proc("glUnmapBuffer");
    if (!map_buffer || !*unmap)
        return 0;
    *map = map_read;
    return 1;
}
