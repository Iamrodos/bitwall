/*
 * The GPU context on a Pi: GLES 2 through SDL, which on KMS is EGL on a
 * GBM surface of the output's. GLES 2 is what the Pi's GPU speaks
 * natively, and what the shaders are written in. Part of
 * bitwall-paint.c, which includes it (platform.h).
 */

static const char *gpu_name(void)
{
    return "gles2";
}

/* Every window gets a context of its own, made without sharing, so that
 * one wall's atlas can never be another's. */
static void gpu_attributes(void)
{
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
}

static int gpu_context(Screen *s)
{
    s->gl.ctx = SDL_GL_CreateContext(s->win);
    if (!s->gl.ctx)
        return -1;
    gl_use(s);
    SDL_GL_SetSwapInterval(0);
    return 0;
}

/* Make this screen's GL context the current one. GL has no notion of
 * which screen a call is for: it goes to whichever context is current,
 * so a call made for one wall with another's current draws on the other
 * wall's glass, or into its atlas, and says nothing. Every function that
 * calls GL for a screen does this first. SDL returns at once when the
 * context is already current, so asking often costs nothing. */
static void gl_use(Screen *s)
{
    if (s->gl.ctx)
        SDL_GL_MakeCurrent(s->win, s->gl.ctx);
}

static void gpu_swap(Screen *s)
{
    SDL_GL_SwapWindow(s->win);
}

static void gpu_drop(Screen *s)
{
    SDL_GL_DeleteContext(s->gl.ctx);
}

static void *gpu_proc(const char *name)
{
    return SDL_GL_GetProcAddress(name);
}

static const char *gpu_preamble(void)
{
    return "";
}

static GLuint gpu_glass_fb(Screen *s)
{
    (void)s;
    return 0;
}

static void gpu_vertices(const void *verts, size_t bytes)
{
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)bytes, verts, GL_STREAM_DRAW);
}

/* The window's framebuffer follows the window. */
static int gpu_resized(Screen *s)
{
    (void)s;
    return 0;
}

/* Where GLES 3 is there, a pixel buffer is mapped for reading by range. */
static int gpu_read_later(void *(**map)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield),
                          GLboolean (**unmap)(GLenum))
{
    const char *v = (const char *)glGetString(GL_VERSION);
    if (!v || strncmp(v, "OpenGL ES ", 10) || atoi(v + 10) < 3)
        return 0;
    *map = (void *(*)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield))gpu_proc("glMapBufferRange");
    *unmap = (GLboolean (*)(GLenum))gpu_proc("glUnmapBuffer");
    return *map && *unmap;
}
