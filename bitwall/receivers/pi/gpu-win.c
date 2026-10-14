/*
 * The GPU context on Windows: OpenGL 2.1 compatibility through SDL's WGL
 * context, which takes the painter's GLES 2 shaders once their precision
 * words are defined away, as on a Mac (gpu-gl.c). In a window it is the
 * window's context; with no screen it is the context of a hidden window
 * of its own, and the glass is a framebuffer of the context's.
 *
 * opengl32.dll exports only GL 1.1, so every later function the painter
 * calls is defined here as a call through the pointer the driver gives
 * for it, looked up the first time it is called. Part of
 * bitwall-paint.c, which includes it (platform.h), after compat-win.h has
 * brought in windows.h.
 */

#define GL_PROGRAM_POINT_SIZE_ 0x8642
#define GL_POINT_SPRITE_ 0x8861
#define GL_READ_ONLY_ 0x88B8

static void *gpu_proc(const char *name)
{
    static HMODULE gl;
    void *p = (void *)wglGetProcAddress(name);
    if (p && p != (void *)1 && p != (void *)2 && p != (void *)3 && p != (void *)-1)
        return p;
    if (!gl)
        gl = LoadLibraryA("opengl32.dll");
    return gl ? (void *)GetProcAddress(gl, name) : NULL;
}

/* A function past GL 1.1, called through the driver's pointer. */
#define GLFN(ret, name, params, args)                                         \
    GL_APICALL ret GL_APIENTRY name params                                    \
    {                                                                         \
        static ret (GL_APIENTRY *p) params;                                   \
        if (!p)                                                               \
            p = (ret (GL_APIENTRY *) params)gpu_proc(#name);                  \
        return p args;                                                        \
    }
#define GLFV(name, params, args)                                              \
    GL_APICALL void GL_APIENTRY name params                                   \
    {                                                                         \
        static void (GL_APIENTRY *p) params;                                  \
        if (!p)                                                               \
            p = (void (GL_APIENTRY *) params)gpu_proc(#name);                 \
        p args;                                                               \
    }

GLFV(glActiveTexture, (GLenum t), (t))
GLFV(glAttachShader, (GLuint p_, GLuint s), (p_, s))
GLFV(glBindBuffer, (GLenum t, GLuint b), (t, b))
GLFV(glBindFramebuffer, (GLenum t, GLuint f), (t, f))
GLFV(glBufferData, (GLenum t, GLsizeiptr n, const void *d, GLenum u), (t, n, d, u))
GLFV(glBufferSubData, (GLenum t, GLintptr o, GLsizeiptr n, const void *d), (t, o, n, d))
GLFN(GLenum, glCheckFramebufferStatus, (GLenum t), (t))
GLFV(glCompileShader, (GLuint s), (s))
GLFN(GLuint, glCreateProgram, (void), ())
GLFN(GLuint, glCreateShader, (GLenum t), (t))
GLFV(glDeleteBuffers, (GLsizei n, const GLuint *b), (n, b))
GLFV(glDeleteFramebuffers, (GLsizei n, const GLuint *f), (n, f))
GLFV(glDeleteProgram, (GLuint p_), (p_))
GLFV(glDeleteShader, (GLuint s), (s))
GLFV(glDisableVertexAttribArray, (GLuint i), (i))
GLFV(glEnableVertexAttribArray, (GLuint i), (i))
GLFV(glFramebufferTexture2D, (GLenum t, GLenum a, GLenum tt, GLuint tex, GLint l),
     (t, a, tt, tex, l))
GLFV(glGenBuffers, (GLsizei n, GLuint *b), (n, b))
GLFV(glGenFramebuffers, (GLsizei n, GLuint *f), (n, f))
GLFN(GLint, glGetAttribLocation, (GLuint p_, const GLchar *n), (p_, n))
GLFV(glGetProgramInfoLog, (GLuint p_, GLsizei m, GLsizei *l, GLchar *s), (p_, m, l, s))
GLFV(glGetProgramiv, (GLuint p_, GLenum e, GLint *v), (p_, e, v))
GLFV(glGetShaderInfoLog, (GLuint p_, GLsizei m, GLsizei *l, GLchar *s), (p_, m, l, s))
GLFV(glGetShaderiv, (GLuint p_, GLenum e, GLint *v), (p_, e, v))
GLFN(GLint, glGetUniformLocation, (GLuint p_, const GLchar *n), (p_, n))
GLFV(glLinkProgram, (GLuint p_), (p_))
GLFV(glShaderSource, (GLuint s, GLsizei n, const GLchar *const *t, const GLint *l),
     (s, n, t, l))
GLFV(glUniform1f, (GLint l, GLfloat a), (l, a))
GLFV(glUniform1i, (GLint l, GLint a), (l, a))
GLFV(glUniform2f, (GLint l, GLfloat a, GLfloat b), (l, a, b))
GLFV(glUniform4f, (GLint l, GLfloat a, GLfloat b, GLfloat c, GLfloat d), (l, a, b, c, d))
GLFV(glUseProgram, (GLuint p_), (p_))
GLFV(glVertexAttribPointer, (GLuint i, GLint n, GLenum t, GLboolean nm, GLsizei st,
                             const void *o), (i, n, t, nm, st, o))

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

static const char *gpu_preamble(void)
{
    return "#version 120\n#define precision\n#define lowp\n#define mediump\n#define highp\n";
}

/* With no screen each context has a hidden window of its own to be
 * current on, which nothing shows. */
#define HIDDEN 8
static struct {
    SDL_GLContext ctx;
    SDL_Window *win;
} hidden[HIDDEN];

static SDL_Window *hidden_window(SDL_GLContext ctx)
{
    int i;
    for (i = 0; i < HIDDEN; i++)
        if (hidden[i].ctx == ctx)
            return hidden[i].win;
    return NULL;
}

static int gpu_context_none(Screen *s)
{
    SDL_Window *win;
    int i;
    if (!SDL_WasInit(SDL_INIT_VIDEO) && SDL_InitSubSystem(SDL_INIT_VIDEO) < 0) {
        fprintf(stderr, "bitwall-paint: SDL video: %s\n", SDL_GetError());
        return -1;
    }
    gpu_attributes();
    win = SDL_CreateWindow("bitwall", 0, 0, 16, 16, SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
    if (!win) {
        fprintf(stderr, "bitwall-paint: hidden window: %s\n", SDL_GetError());
        return -1;
    }
    s->gl.ctx = SDL_GL_CreateContext(win);
    if (!s->gl.ctx) {
        fprintf(stderr, "bitwall-paint: GL context: %s\n", SDL_GetError());
        SDL_DestroyWindow(win);
        return -1;
    }
    for (i = 0; i < HIDDEN && hidden[i].ctx; i++)
        ;
    if (i == HIDDEN)
        return -1;
    hidden[i].ctx = s->gl.ctx;
    hidden[i].win = win;
    SDL_GL_MakeCurrent(win, s->gl.ctx);
    return 0;
}

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
    fprintf(stderr, "bitwall-paint: GL %s, %s, %s; GLSL %s\n",
            (const char *)glGetString(GL_VERSION), (const char *)glGetString(GL_RENDERER),
            (const char *)glGetString(GL_VENDOR),
            (const char *)glGetString(GL_SHADING_LANGUAGE_VERSION));
    glEnable(GL_PROGRAM_POINT_SIZE_);
    glEnable(GL_POINT_SPRITE_);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    return 0;
}

static void gl_use(Screen *s)
{
    if (!s->gl.ctx)
        return;
    if (SDL_GL_GetCurrentContext() == s->gl.ctx)
        return;
    SDL_GL_MakeCurrent(s->win ? s->win : hidden_window(s->gl.ctx), s->gl.ctx);
}

static GLuint gpu_glass_fb(Screen *s)
{
    return s->gl.glass_fb;
}

/* In a buffer a power of two bytes, as on a Mac (gpu-gl.c), so a grid
 * that changes size with the glass leaves a handful of sizes behind. */
static void gpu_vertices(const void *verts, size_t bytes)
{
    size_t room = 4096;
    while (room < bytes)
        room <<= 1;
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)room, NULL, GL_STREAM_DRAW);
    glBufferSubData(GL_ARRAY_BUFFER, 0, (GLsizeiptr)bytes, verts);
}

/* With no screen the glass's texture is given its new size in place; a
 * window's framebuffer follows its window. */
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
    SDL_Window *win;
    int i;
    if (s->win) {
        SDL_GL_DeleteContext(s->gl.ctx);
        return;
    }
    gl_use(s);
    glDeleteFramebuffers(1, &s->gl.glass_fb);
    glDeleteTextures(1, &s->gl.glass_tex);
    win = hidden_window(s->gl.ctx);
    SDL_GL_MakeCurrent(NULL, NULL);
    SDL_GL_DeleteContext(s->gl.ctx);
    if (win)
        SDL_DestroyWindow(win);
    for (i = 0; i < HIDDEN; i++)
        if (hidden[i].ctx == s->gl.ctx)
            hidden[i].ctx = NULL;
}

static void *(GL_APIENTRY *map_buffer)(GLenum, GLenum);

static void *map_read(GLenum target, ptrdiff_t at, ptrdiff_t len, GLbitfield access)
{
    (void)at;
    (void)len;
    (void)access;
    return map_buffer(target, GL_READ_ONLY_);
}

static int gpu_read_later(void *(**map)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield),
                          GLboolean (**unmap)(GLenum))
{
    map_buffer = (void *(GL_APIENTRY *)(GLenum, GLenum))gpu_proc("glMapBuffer");
    *unmap = (GLboolean (*)(GLenum))gpu_proc("glUnmapBuffer");
    if (!map_buffer || !*unmap)
        return 0;
    *map = map_read;
    return 1;
}
