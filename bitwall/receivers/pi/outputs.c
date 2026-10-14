/* The wall's outputs: what is made of the glass once it is composed.
 *
 * A wall composes once, and everything that shows it is a view of that
 * one composition: the screen (a Pi's KMS, a window, or none:
 * display-*.c), the snapshots (`picture` and the QOI `mirror`, which draw
 * the glass when they look: glass_rgba), and here, the glass streamed
 * raw, as many streams as are asked for, each at a size, a rate and with
 * or without the painter's overlay of its own. No output differs from
 * another in what is composed: a stream is the glass, scaled.
 *
 * The streams are owned by `Outputs`, which exists only while one runs:
 * with none, nothing of this is made and the screen is drawn as it
 * always was. While any runs, the glass is drawn once, into `scene`,
 * the glass's size and upright, and copied to the screen in one quad;
 * each stream then takes its frames from that one scene, on its own
 * clock. A stream that carries the overlay takes them from `lit`, the
 * scene with the overlay drawn over it, made at most once a tick for
 * every stream that wants it.
 *
 * A stream's frame is I420 at exactly its size, made from the scene in
 * one pass: shrunk to the box it fills there by where its looks fall,
 * which is the area each pixel covers at a half or a quarter of the
 * glass and near it between (FS_I420), and turned into the planes, four
 * samples of a plane to a texel; then read back, a byte and a half a
 * pixel. Where GLES 3 is there the read goes into a pixel buffer and is
 * collected at the next frame, so the draw never waits for it. The frame goes into one of two
 * buffers and the stream's writer thread puts it on its fifo, so a
 * reader that is slow, or not there, costs the loop nothing: a frame it
 * is not ready for is let go, the newest kept.
 *
 * Included by bitwall-paint.c, after the sessions it serves, as the
 * seams are, so the painter stays one translation unit. */

#define RAW_HEADER 32
/* Streams a wall runs at once, every session's together: each costs a
 * pass over the glass and a read of it every frame. Said in `caps`. */
#define STREAMS 8
/* The longest name a stream is asked by: its fifo is `glass-<name>`. */
#define STREAM_NAME 24

typedef struct Raw {
    struct Session *owner;      /* the controller that asked */
    char name[STREAM_NAME + 1]; /* as asked; empty for the stream on `glass` */
    char path[700];             /* s/<session>/glass, or glass-<name> */
    int w, h, fps;              /* the frame's size, and how often */
    int overlay;                /* drawn with the painter's own overlay */
    int bx, by, bw, bh;         /* where the glass goes in it */
    double every, due;          /* ms between frames, and when the next is */
    uint64_t number;            /* frames drawn so far */
    size_t len;                 /* a frame's bytes, the header with them */

    /* the GPU's part */
    GLuint yuv_fb, yuv_tex;     /* the planes, packed */
    int tw, th;                 /* its texels: a Y row, then U and V side
                                 * by side, four samples to a texel */
    uint8_t *packed;            /* the planes as read, tw * 4 by th */
    int es3;                    /* reading through `pbo`, a frame later */
    GLuint pbo;
    int pending;                /* `pbo` holds a frame not yet collected */
    uint64_t pending_n, pending_us;
    void *(*map)(GLenum, ptrdiff_t, ptrdiff_t, GLbitfield);
    GLboolean (*unmap)(GLenum);

    /* the writer's part, under `mu` */
    SDL_Thread *writer;
    SDL_mutex *mu;
    int stop;
    int wake[2];                /* rung with `stop`, so a writer waiting on
                                 * a full pipe sees it at once */
    uint8_t *buf[2];
    int next;                   /* the buffer holding a frame to write, -1 none */
    int writing;                /* the one the writer has, -1 none */
    unsigned long streamed;     /* frames written whole */
    unsigned long dropped;      /* frames drawn and never written */
} Raw;

typedef struct Outputs {
    Raw *streams[STREAMS];      /* in the order they were asked for */
    int n;
    int twice;                  /* BITWALL_STREAM_DRAW_TWICE: the glass drawn
                                 * again for each frame and turned to I420
                                 * here, as a picture is; no overlay */

    /* what every stream is drawn from, on the GPU */
    GLuint copy_prog, yuv_prog, vbo;
    GLuint scene_fb, scene_tex; /* the glass, drawn once a frame */
    int scene_w, scene_h;
    int fresh;                  /* it holds what the glass shows */
    GLuint lit_fb, lit_tex;     /* the scene with the overlay over it */
    int lit_w, lit_h;
    int lit_fresh;
} Outputs;

/* ---- the scene, and what is drawn from it */


/* A quad given in clip space and turned as the glass is: the scene copied
 * to the screen, and the planes drawn from the scene, unturned. */
static const char *VS_GLASS =
    "attribute vec2 a_pos; attribute vec2 a_uv; uniform vec2 u_turn; varying vec2 v_uv;\n"
    "void main() {\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x) * a_pos, 0.0, 1.0);\n"
    "  v_uv = a_uv;\n"
    "}\n";

/* The scene as it was drawn, pixel for pixel. */
static const char *FS_GLASS =
    "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\n"
    "precision mediump float;\n#endif\n"
    "uniform sampler2D u_src;\n"
    "varying vec2 v_uv;\n"
    "void main() { gl_FragColor = texture2D(u_src, v_uv); }\n";

/* The planes of a frame, I420 in BT.709 at studio range as yuv_argb reads
 * it, four samples to a texel, so that what is read back is the planes
 * themselves: the frame's rows of Y in the first `u_size.y` rows of
 * texels, and below them, for each two rows of the frame, a row of U and
 * a row of V side by side, V's from texel `u_half`. `u_map` takes a
 * frame's pixel to the scene's texture across the box the glass fills,
 * upright; black where the glass does not reach.
 *
 * A frame smaller than the glass is shrunk here, in the one pass, by
 * where the looks fall rather than by more of them, since on a Pi's GPU
 * every look costs: a look at the middle of a pixel is the mean of the
 * texels round it, which down to half the glass's size is the area the
 * pixel covers, exactly at a half and near it between. So a Y sample is
 * one look, as at full size, and four (`u_tap` a quarter of a pixel
 * apart) only where the frame is under half the glass, where one would
 * miss texels. A chroma sample covers four pixels: one look at their
 * middle at full size, which is their mean; two, at the middles of two
 * opposite ones, while shrinking by less than half; and four, one in the
 * middle of each, from half down (`u_ctap` 0, 1, 2). A pass of its own
 * to shrink first, sixteen looks a pixel, cost a Pi 5's GPU 40% more
 * than the stream without it; this, 3%. */
static const char *FS_I420 =
    "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\n"
    "precision mediump float;\n#endif\n"
    "uniform sampler2D u_src; uniform vec2 u_size; uniform float u_half;\n"
    "uniform vec4 u_map; uniform vec2 u_tap; uniform float u_ctap;\n"
    "vec3 at(vec2 uv) { return texture2D(u_src, uv).rgb; }\n"
    "bool outside(vec2 uv) {\n"
    "  return uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0;\n"
    "}\n"
    "vec3 look(vec2 p) {\n"
    "  vec2 uv = p * u_map.xy + u_map.zw;\n"
    "  if (outside(uv)) return vec3(0.0);\n"
    "  if (u_tap.x == 0.0) return at(uv);\n"
    "  return 0.25 * (at(uv - u_tap) + at(uv + u_tap)\n"
    "               + at(uv + vec2(u_tap.x, -u_tap.y)) + at(uv + vec2(-u_tap.x, u_tap.y)));\n"
    "}\n"
    "vec3 block(vec2 p) {\n"
    "  vec2 uv = p * u_map.xy + u_map.zw, d = 0.5 * u_map.xy;\n"
    "  if (outside(uv)) return vec3(0.0);\n"
    "  if (u_ctap == 0.0) return at(uv);\n"
    "  if (u_ctap == 1.0) return 0.5 * (at(uv - d) + at(uv + d));\n"
    "  return 0.25 * (at(uv - d) + at(uv + d) + at(uv + vec2(d.x, -d.y)) + at(uv + vec2(-d.x, d.y)));\n"
    "}\n"
    "float luma(vec2 p) {\n"
    "  return 0.0627451 + 0.8588235 * dot(look(p), vec3(0.2126, 0.7152, 0.0722));\n"
    "}\n"
    "void main() {\n"
    "  vec2 t = floor(gl_FragCoord.xy);\n"
    "  if (t.y < u_size.y) {\n"
    "    float x = t.x * 4.0 + 0.5, y = t.y + 0.5;\n"
    "    gl_FragColor = vec4(luma(vec2(x, y)), luma(vec2(x + 1.0, y)),\n"
    "                        luma(vec2(x + 2.0, y)), luma(vec2(x + 3.0, y)));\n"
    "  } else {\n"
    "    float v = step(u_half, t.x);\n"
    "    float x = (t.x - v * u_half) * 8.0 + 1.0, y = (t.y - u_size.y) * 2.0 + 1.0;\n"
    "    vec3 k = v > 0.5 ? vec3(0.5, -0.4542, -0.0458) : vec3(-0.1146, -0.3854, 0.5);\n"
    "    gl_FragColor = 0.5019608 + 0.8784314 * vec4(dot(block(vec2(x, y)), k),\n"
    "        dot(block(vec2(x + 2.0, y)), k), dot(block(vec2(x + 4.0, y)), k),\n"
    "        dot(block(vec2(x + 6.0, y)), k));\n"
    "  }\n"
    "}\n";

/* A framebuffer drawing into a texture of its own, w by h, made or made
 * again at that size. -1 when GL will not draw into it. */
static int raw_target(Screen *s, GLuint *fb, GLuint *tex, int w, int h)
{
    int ok;
    if (!*tex) {
        glGenTextures(1, tex);
        glGenFramebuffers(1, fb);
    }
    glBindTexture(GL_TEXTURE_2D, *tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glBindFramebuffer(GL_FRAMEBUFFER, *fb);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, *tex, 0);
    ok = glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE;
    glBindFramebuffer(GL_FRAMEBUFFER, gpu_glass_fb(s));
    return ok ? 0 : -1;
}

/* The quad every pass of the outputs' draws, from `vbo`, by program `p`. */
static void raw_quad(Screen *s, GLuint p)
{
    GLint a_pos = glGetAttribLocation(p, "a_pos"), a_uv = glGetAttribLocation(p, "a_uv");
    glBindBuffer(GL_ARRAY_BUFFER, s->outs->vbo);
    glEnableVertexAttribArray(a_pos);
    glEnableVertexAttribArray(a_uv);
    glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat), (const void *)0);
    glVertexAttribPointer(a_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                          (const void *)(uintptr_t)(2 * sizeof(GLfloat)));
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s->gl.ibo);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, 0);
}

/* Texture `tex` drawn whole over the target bound, w by h, by the copy
 * program, unturned: the scene as it was drawn, pixel for pixel. */
static void raw_blit(Screen *s, GLuint tex, int w, int h)
{
    GLuint p = s->outs->copy_prog;
    glViewport(0, 0, w, h);
    glDisable(GL_BLEND);
    glUseProgram(p);
    glUniform2f(glGetUniformLocation(p, "u_turn"), 1, 0);
    glUniform1i(glGetUniformLocation(p, "u_src"), 0);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    raw_quad(s, p);
}

/* The glass drawn into the scene, upright and the overlay left out, as a
 * picture of it is drawn. -1 when it cannot be. */
static int raw_scene(Screen *s)
{
    Outputs *o = s->outs;
    o->fresh = o->lit_fresh = 0;
    if (s->width > s->gl.max_texture || s->height > s->gl.max_texture)
        return -1;
    gl_use(s);
    if (o->scene_w != s->width || o->scene_h != s->height) {
        o->scene_w = o->scene_h = 0;
        if (raw_target(s, &o->scene_fb, &o->scene_tex, s->width, s->height) < 0)
            return -1;
        o->scene_w = s->width;
        o->scene_h = s->height;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, o->scene_fb);
    s->capturing = 1;
    gl_draw(s);
    pics_gl(s);
    s->capturing = 0;
    glBindFramebuffer(GL_FRAMEBUFFER, gpu_glass_fb(s));
    o->fresh = 1;
    return 0;
}

/* The scene with the overlay over it, as the screen shows it but
 * upright, made from the scene as it stands. -1 when it cannot be. */
static int raw_lit(Screen *s)
{
    Outputs *o = s->outs;
    if (o->lit_fresh && o->lit_w == o->scene_w && o->lit_h == o->scene_h)
        return 0;
    if (o->lit_w != o->scene_w || o->lit_h != o->scene_h) {
        o->lit_w = o->lit_h = 0;
        if (raw_target(s, &o->lit_fb, &o->lit_tex, o->scene_w, o->scene_h) < 0)
            return -1;
        o->lit_w = o->scene_w;
        o->lit_h = o->scene_h;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, o->lit_fb);
    raw_blit(s, o->scene_tex, o->lit_w, o->lit_h);
    s->capturing = 1;
    stats_gl(s);
    s->capturing = 0;
    glBindFramebuffer(GL_FRAMEBUFFER, gpu_glass_fb(s));
    o->lit_fresh = 1;
    return 0;
}

/* The scene onto the screen, turned as the glass hangs: one quad, each
 * pixel the scene's own. */
static void raw_copy(Screen *s)
{
    Outputs *o = s->outs;
    glViewport(0, 0, s->phys_w, s->phys_h);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glDisable(GL_BLEND);
    glUseProgram(o->copy_prog);
    gl_turn(s, o->copy_prog);
    glUniform1i(glGetUniformLocation(o->copy_prog, "u_src"), 0);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, o->scene_tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    raw_quad(s, o->copy_prog);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
}

/* The glass into the back buffer: while a stream is taken from the scene,
 * drawn once into it and copied; otherwise drawn straight in. */
static void gl_glass(Screen *s)
{
    if (s->outs && !s->outs->twice && s->outs->copy_prog && raw_scene(s) == 0) {
        raw_copy(s);
        return;
    }
    gl_draw(s);
    pics_gl(s);
}

/* The glass has changed and is not on any screen: the scene is drawn
 * again when a stream next looks. */
static void outputs_stale(Screen *s)
{
    if (s->outs)
        s->outs->fresh = s->outs->lit_fresh = 0;
}

/* ---- the streams */

#ifndef GL_VERSION
#define GL_VERSION 0x1F02
#endif
#ifndef GL_PIXEL_PACK_BUFFER
#define GL_PIXEL_PACK_BUFFER 0x88EB
#endif
#ifndef GL_STREAM_READ
#define GL_STREAM_READ 0x88E1
#endif
#ifndef GL_MAP_READ_BIT
#define GL_MAP_READ_BIT 0x0001
#endif

static uint64_t unix_us(void)
{
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return (uint64_t)t.tv_sec * 1000000u + (uint64_t)(t.tv_nsec / 1000);
}

/* Where the glass goes in the frame: as large as fits with its shape
 * kept, in the middle, black round it. Worked out for each frame, since
 * the glass may change size under a stream. */
static void raw_fit(Raw *r, int gw, int gh)
{
    if ((long)gw * r->h <= (long)gh * r->w) {
        r->bh = r->h;
        r->bw = (int)(((long)gw * r->h + gh / 2) / gh);
    } else {
        r->bw = r->w;
        r->bh = (int)(((long)gh * r->w + gw / 2) / gw);
    }
    if (r->bw < 1)
        r->bw = 1;
    if (r->bh < 1)
        r->bh = 1;
    r->bx = (r->w - r->bw) / 2;
    r->by = (r->h - r->bh) / 2;
}

/* A frame's pixel from the glass, RGBA bw by bh at bx,by; null where the
 * glass does not reach, which is black. */
static const uint8_t *raw_px(const uint8_t *px, const Raw *r, int x, int y)
{
    x -= r->bx;
    y -= r->by;
    if (x < 0 || y < 0 || x >= r->bw || y >= r->bh)
        return NULL;
    return px + ((size_t)y * r->bw + x) * 4;
}

/* The frame's planes worked out here from the glass as RGBA, bw by bh, as
 * the GPU's pass makes them: BT.709 at studio range, black round the
 * glass, each chroma sample the mean of its four pixels. */
static void i420_of_rgba(const uint8_t *px, const Raw *r, uint8_t *out)
{
    int cw = r->w / 2, ch = r->h / 2, x, y, i;
    uint8_t *Y = out, *U = out + (size_t)r->w * r->h, *V = U + (size_t)cw * ch;
    for (y = 0; y < r->h; y++)
        for (x = 0; x < r->w; x++) {
            const uint8_t *c = raw_px(px, r, x, y);
            Y[(size_t)y * r->w + x] = (uint8_t)(c ? 16 + ((47 * c[0] + 157 * c[1] + 16 * c[2]
                                                          + 128) >> 8) : 16);
        }
    for (y = 0; y < ch; y++)
        for (x = 0; x < cw; x++) {
            int sum[3] = {0, 0, 0};
            for (i = 0; i < 4; i++) {
                const uint8_t *c = raw_px(px, r, x * 2 + (i & 1), y * 2 + (i >> 1));
                if (c) {
                    sum[0] += c[0];
                    sum[1] += c[1];
                    sum[2] += c[2];
                }
            }
            /* In 1024ths: four pixels' sum, and 256ths of a level. */
            U[(size_t)y * cw + x] = (uint8_t)((-26 * sum[0] - 87 * sum[1] + 112 * sum[2]
                                               + (128 << 10) + 512) >> 10);
            V[(size_t)y * cw + x] = (uint8_t)((112 * sum[0] - 102 * sum[1] - 10 * sum[2]
                                               + (128 << 10) + 512) >> 10);
        }
}

/* The planes out of what the GPU's pass left, `tw` texels a row: each row
 * of Y at the start of its own, and each row of U and of V side by side
 * below them. */
static void raw_unpack(const Raw *r, const uint8_t *p, uint8_t *out)
{
    size_t row = (size_t)r->tw * 4, cw = (size_t)r->w / 2;
    uint8_t *U = out + (size_t)r->w * r->h, *V = U + cw * (size_t)(r->h / 2);
    int y;
    for (y = 0; y < r->h; y++)
        memcpy(out + (size_t)y * r->w, p + y * row, (size_t)r->w);
    for (y = 0; y < r->h / 2; y++) {
        const uint8_t *q = p + (size_t)(r->h + y) * row;
        memcpy(U + y * cw, q, cw);
        memcpy(V + y * cw, q + row / 2, cw);
    }
}

static void put_le(uint8_t *p, uint64_t v, int n)
{
    int i;
    for (i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * i));
}

/* A frame's header: "BWG1", its width and height, its number, when it was
 * drawn in microseconds of the wall's clock, and the bytes after it. */
static void raw_header(uint8_t *p, const Raw *r, uint64_t n, uint64_t us)
{
    memcpy(p, "BWG1", 4);
    put_le(p + 4, (uint64_t)r->w, 4);
    put_le(p + 8, (uint64_t)r->h, 4);
    put_le(p + 12, n, 8);
    put_le(p + 20, us, 8);
    put_le(p + 28, (uint64_t)(r->len - RAW_HEADER), 4);
}

static int raw_stopping(Raw *r)
{
    int stop;
    SDL_LockMutex(r->mu);
    stop = r->stop;
    SDL_UnlockMutex(r->mu);
    return stop;
}

/* One frame onto the fifo, whole: 1 when it went. The fifo is opened
 * without waiting, so with nobody reading the frame is let go; and kept
 * open while a reader stays, each frame after the last, so a reader that
 * comes is given whole frames from its first byte. A reader that goes
 * mid-frame has the rest let go with it, and the next is given whole ones
 * again. */
static int raw_put(Raw *r, int *fd, const uint8_t *p, size_t n)
{
    if (*fd < 0) {
        *fd = ipc_glass_open(r->path);
        if (*fd < 0)
            return 0;
    }
    while (n) {
        ssize_t k = ipc_write(*fd, p, n);
        if (k > 0) {
            p += k;
            n -= (size_t)k;
        } else if (k < 0 && (errno == EAGAIN || errno == EINTR)) {
            if (raw_stopping(r))
                return 0;
            if (wait_writable(*fd, r->wake[0], 1000) == 0)
                waker_drain(r->wake);   /* a frame came, or the stream is ending */
        } else {
            break;
        }
    }
    if (n) {
        ipc_close(*fd);
        *fd = -1;
        return 0;
    }
    return 1;
}

/* The writer: each frame the loop hands over, onto the fifo. */
static int raw_writer(void *arg)
{
    Raw *r = arg;
    int fd = -1;
    for (;;) {
        int k, ok;
        SDL_LockMutex(r->mu);
        if (r->stop) {
            SDL_UnlockMutex(r->mu);
            break;
        }
        k = r->next;
        if (k >= 0) {
            r->next = -1;
            r->writing = k;
        }
        SDL_UnlockMutex(r->mu);
        if (k < 0) {
            wait_readable(-1, r->wake[0], 1000);
            waker_drain(r->wake);
            continue;
        }
        ok = raw_put(r, &fd, r->buf[k], r->len);
        SDL_LockMutex(r->mu);
        r->writing = -1;
        if (ok)
            r->streamed++;
        else
            r->dropped++;
        SDL_UnlockMutex(r->mu);
    }
    if (fd >= 0)
        ipc_close(fd);
    return 0;
}

/* The buffer the next frame goes in: not the one being written. A frame
 * still waiting in it is let go, for this newer one. */
static uint8_t *raw_buffer(Raw *r, int *k)
{
    SDL_LockMutex(r->mu);
    *k = r->writing == 0 ? 1 : 0;
    if (r->next == *k) {
        r->next = -1;
        r->dropped++;
    }
    SDL_UnlockMutex(r->mu);
    return r->buf[*k];
}

/* Buffer `k` filled, handed to the writer; a frame waiting before it is
 * let go. */
static void raw_send(Raw *r, int k)
{
    SDL_LockMutex(r->mu);
    if (r->next >= 0 && r->next != k)
        r->dropped++;
    r->next = k;
    SDL_UnlockMutex(r->mu);
    waker_ring(r->wake);
}

static void raw_drop(Raw *r)
{
    SDL_LockMutex(r->mu);
    r->dropped++;
    SDL_UnlockMutex(r->mu);
}


/* What every stream shares made: the programs and the quad. -1 when they
 * are not. */
static int outputs_gl_up(Screen *s)
{
    static const GLfloat quad[16] = {-1, -1, 0, 0, 1, -1, 1, 0, 1, 1, 1, 1, -1, 1, 0, 1};
    Outputs *o = s->outs;
    gl_use(s);
    o->copy_prog = gl_program(VS_GLASS, FS_GLASS);
    o->yuv_prog = gl_program(VS_GLASS, FS_I420);
    if (!o->copy_prog || !o->yuv_prog)
        return -1;
    glGenBuffers(1, &o->vbo);
    glBindBuffer(GL_ARRAY_BUFFER, o->vbo);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof quad, quad, GL_STATIC_DRAW);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    return 0;
}

static void gl_free_target(GLuint *fb, GLuint *tex)
{
    if (*tex)
        glDeleteTextures(1, tex);
    if (*fb)
        glDeleteFramebuffers(1, fb);
    *tex = *fb = 0;
}

/* The outputs let go once the last stream has: every target, program and
 * buffer of theirs, so that the screen is drawn as it always was. */
static void outputs_down(Screen *s)
{
    Outputs *o = s->outs;
    GLuint progs[2];
    int i;
    if (!o || o->n)
        return;
    if (s->gpu) {
        gl_use(s);
        gl_free_target(&o->scene_fb, &o->scene_tex);
        gl_free_target(&o->lit_fb, &o->lit_tex);
        if (o->vbo)
            glDeleteBuffers(1, &o->vbo);
        progs[0] = o->copy_prog;
        progs[1] = o->yuv_prog;
        for (i = 0; i < 2; i++)
            if (progs[i])
                glDeleteProgram(progs[i]);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    }
    free(o);
    s->outs = NULL;
}

/* The outputs made, where no stream has made them yet. Null when they
 * cannot be. */
static Outputs *outputs_up(Screen *s)
{
    const char *twice = getenv("BITWALL_STREAM_DRAW_TWICE");
    if (s->outs)
        return s->outs;
    s->outs = calloc(1, sizeof *s->outs);
    if (!s->outs)
        return NULL;
    s->outs->twice = twice && *twice && strcmp(twice, "0");
    if (!s->outs->twice && outputs_gl_up(s) < 0) {
        outputs_down(s);
        return NULL;
    }
    return s->outs;
}

/* A stream's own part on the GPU: the packed target, and where GLES 3 is
 * there, the pixel buffer it is read into. -1 when any of it is not. */
static int raw_gl_up(Screen *s, Raw *r)
{
    gl_use(s);
    if (raw_target(s, &r->yuv_fb, &r->yuv_tex, r->tw, r->th) < 0)
        return -1;
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    if (!getenv("BITWALL_STREAM_SYNC") && gpu_read_later(&r->map, &r->unmap)) {
        glGenBuffers(1, &r->pbo);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, r->pbo);
        glBufferData(GL_PIXEL_PACK_BUFFER, (GLsizeiptr)r->tw * 4 * r->th, NULL, GL_STREAM_READ);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        r->es3 = 1;
    } else {
        r->packed = malloc((size_t)r->tw * 4 * r->th);
        if (!r->packed)
            return -1;
    }
    return 0;
}

/* A stream ended: its writer waited for, its fifo removed, every buffer
 * and texture of its own let go, and taken off the list; the outputs
 * with it, where it was the last. */
static void raw_free(Screen *s, Raw *r)
{
    Outputs *o = s->outs;
    int i;
    if (!r)
        return;
    if (r->writer) {
        SDL_LockMutex(r->mu);
        r->stop = 1;
        SDL_UnlockMutex(r->mu);
        waker_ring(r->wake);
        SDL_WaitThread(r->writer, NULL);
    }
    waker_close(r->wake);
    if (r->mu)
        SDL_DestroyMutex(r->mu);
    if (r->path[0])
        ipc_glass_remove(r->path);
    if (s->gpu) {
        gl_use(s);
        gl_free_target(&r->yuv_fb, &r->yuv_tex);
        if (r->pbo)
            glDeleteBuffers(1, &r->pbo);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    }
    free(r->packed);
    free(r->buf[0]);
    free(r->buf[1]);
    for (i = 0; o && i < o->n; i++)
        if (o->streams[i] == r) {
            memmove(o->streams + i, o->streams + i + 1, (size_t)(o->n - i - 1) * sizeof r);
            o->n--;
            break;
        }
    free(r);
    outputs_down(s);
}

/* A stream set going on this screen, `r` its request: the outputs made
 * where it is the first, its fifo, its buffers, its part on the GPU and
 * its writer, and put on the list. -1, and nothing left of it, when any
 * of that cannot be. */
static int raw_up(Screen *s, Raw *r)
{
    Outputs *o = outputs_up(s);
    r->next = r->writing = -1;
    r->wake[0] = r->wake[1] = -1;
    if (!o || o->n >= STREAMS) {
        free(r);
        return -1;
    }
    o->streams[o->n++] = r;
    r->len = RAW_HEADER + (size_t)r->w * r->h + 2 * (size_t)(r->w / 2) * (r->h / 2);
    r->tw = (r->w / 2 + 3) / 4 * 2;
    r->th = r->h + r->h / 2;
    if (ipc_glass_make(r->path) < 0) {
        r->path[0] = 0;
        raw_free(s, r);
        return -1;
    }
    r->buf[0] = malloc(r->len);
    r->buf[1] = malloc(r->len);
    r->mu = SDL_CreateMutex();
    if (!r->buf[0] || !r->buf[1] || !r->mu || waker_open(r->wake) < 0
        || (!o->twice && raw_gl_up(s, r) < 0)
        || !(r->writer = SDL_CreateThread(raw_writer, "glass", r))) {
        raw_free(s, r);
        return -1;
    }
    return 0;
}

/* `src`, the glass, into the packed target as the frame's planes, shrunk
 * on the way where the frame's box is smaller. */
static void raw_pass(Screen *s, Raw *r, GLuint src)
{
    GLuint p = s->outs->yuv_prog;
    int shrunk = r->bw < s->outs->scene_w || r->bh < s->outs->scene_h;
    int far = r->bw * 2 < s->outs->scene_w || r->bh * 2 < s->outs->scene_h;
    int half = r->bw * 2 <= s->outs->scene_w || r->bh * 2 <= s->outs->scene_h;
    glBindFramebuffer(GL_FRAMEBUFFER, r->yuv_fb);
    glViewport(0, 0, r->tw, r->th);
    glDisable(GL_BLEND);
    glUseProgram(p);
    glUniform2f(glGetUniformLocation(p, "u_turn"), 1, 0);
    glUniform1i(glGetUniformLocation(p, "u_src"), 0);
    glUniform2f(glGetUniformLocation(p, "u_size"), (float)r->w, (float)r->h);
    /* A frame's pixel p to the scene's texture: across the box, upright. */
    glUniform4f(glGetUniformLocation(p, "u_map"), 1.0f / r->bw, -1.0f / r->bh,
                (float)-r->bx / r->bw, 1.0f + (float)r->by / r->bh);
    glUniform1f(glGetUniformLocation(p, "u_half"), (float)(r->tw / 2));
    glUniform2f(glGetUniformLocation(p, "u_tap"), far ? 0.25f / r->bw : 0,
                far ? -0.25f / r->bh : 0);
    glUniform1f(glGetUniformLocation(p, "u_ctap"), !shrunk ? 0.0f : half ? 2.0f : 1.0f);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, src);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    raw_quad(s, p);
}

/* A due frame of stream `r`: drawn, read, and handed to its writer. On
 * the GPU it is drawn from the scene as the screen was last drawn from
 * it, unless the glass is dark, has changed on no screen, or has not been
 * drawn since the streams began, when it is drawn now, once for every
 * stream due. With a pixel buffer the frame read is the last one's,
 * collected now that the GPU has long finished it, and this one is
 * collected at the next. */
static void raw_frame(Screen *s, Raw *r)
{
    Outputs *o = s->outs;
    uint64_t us = unix_us(), n = r->number++;
    uint8_t *b;
    GLuint src;
    int k;
    raw_fit(r, s->width, s->height);
    if (o->twice) {
        uint8_t *px = glass_rgba(s, r->bw, r->bh);
        if (!px) {
            raw_drop(r);
            return;
        }
        b = raw_buffer(r, &k);
        i420_of_rgba(px, r, b + RAW_HEADER);
        free(px);
        raw_header(b, r, n, us);
        raw_send(r, k);
        return;
    }
    gl_use(s);
    if (r->pending) {
        const uint8_t *p;
        r->pending = 0;
        glBindBuffer(GL_PIXEL_PACK_BUFFER, r->pbo);
        p = r->map(GL_PIXEL_PACK_BUFFER, 0, (ptrdiff_t)r->tw * 4 * r->th, GL_MAP_READ_BIT);
        if (p) {
            b = raw_buffer(r, &k);
            raw_unpack(r, p, b + RAW_HEADER);
            raw_header(b, r, r->pending_n, r->pending_us);
            r->unmap(GL_PIXEL_PACK_BUFFER);
            raw_send(r, k);
        } else {
            raw_drop(r);
        }
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    }
    if ((!o->fresh || o->scene_w != s->width || o->scene_h != s->height) && raw_scene(s) < 0) {
        raw_drop(r);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
        return;
    }
    src = o->scene_tex;
    if (r->overlay && stats_shown(s)) {
        if (raw_lit(s) < 0) {
            raw_drop(r);
            glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
            return;
        }
        src = o->lit_tex;
    }
    raw_pass(s, r, src);
    if (r->es3) {
        glBindBuffer(GL_PIXEL_PACK_BUFFER, r->pbo);
        glReadPixels(0, 0, r->tw, r->th, GL_RGBA, GL_UNSIGNED_BYTE, (void *)0);
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        r->pending = 1;
        r->pending_n = n;
        r->pending_us = us;
    } else {
        glReadPixels(0, 0, r->tw, r->th, GL_RGBA, GL_UNSIGNED_BYTE, r->packed);
        b = raw_buffer(r, &k);
        raw_unpack(r, r->packed, b + RAW_HEADER);
        raw_header(b, r, n, us);
        raw_send(r, k);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, gpu_glass_fb(s));
    glViewport(0, 0, s->phys_w, s->phys_h);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
}

/* Every stream whose frame is due, each on its own clock. A dark glass is
 * not drawn on the screen, so the scene is drawn afresh for the streams
 * due, once for all of them. */
static void outputs_tick(Screen *s)
{
    Outputs *o = s->outs;
    double now = now_ms();
    int i, due = 0;
    if (!o)
        return;
    for (i = 0; i < o->n && !due; i++)
        due = now >= o->streams[i]->due;
    if (!due)
        return;
    if (s->blanked)
        outputs_stale(s);
    for (i = 0; i < o->n; i++) {
        Raw *r = o->streams[i];
        if (now < r->due)
            continue;
        r->due = now - r->due < r->every ? r->due + r->every : now + r->every;
        raw_frame(s, r);
    }
}

/* How long the loop may wait before a stream's frame is due. */
static int outputs_wait(Screen *s, int wait)
{
    double now = now_ms();
    int i;
    for (i = 0; s->outs && i < s->outs->n; i++) {
        double left = s->outs->streams[i]->due - now;
        if (left < wait)
            wait = left < 1 ? 1 : (int)left + 1;
    }
    return wait;
}

/* Session `x`'s streams, as its measure line counts them: the frames
 * written whole and those let go, every stream of its together. Empty
 * where it has none. */
static void outputs_measure(Screen *s, struct Session *x, char *out, size_t max)
{
    unsigned long streamed = 0, dropped = 0;
    int i, any = 0;
    *out = 0;
    for (i = 0; s->outs && i < s->outs->n; i++) {
        Raw *r = s->outs->streams[i];
        if (r->owner != x)
            continue;
        SDL_LockMutex(r->mu);
        streamed += r->streamed;
        dropped += r->dropped;
        SDL_UnlockMutex(r->mu);
        any = 1;
    }
    if (any)
        snprintf(out, max, " streamed=%lu stream_dropped=%lu", streamed, dropped);
}

/* Session `x`'s streams ended: the one called `name`, or every one of
 * them where `name` is null. How many. */
static int outputs_stop(Screen *s, struct Session *x, const char *name)
{
    int i, gone = 0;
    for (i = 0; s->outs && i < s->outs->n;) {
        Raw *r = s->outs->streams[i];
        if (r->owner == x && (!name || !strcmp(r->name, name))) {
            raw_free(s, r);         /* which takes it off the list */
            gone++;
            continue;
        }
        i++;
    }
    return gone;
}

/* Every stream ended, as the wall goes. */
static void outputs_free(Screen *s)
{
    while (s->outs && s->outs->n)
        raw_free(s, s->outs->streams[0]);
    outputs_down(s);
}

static int side_pair(char *word, int *w, int *h);

/* A stream's name as asked: letters, digits, `-` and `_`. */
static int stream_name_ok(const char *v)
{
    size_t i, n = strlen(v);
    if (!n || n > STREAM_NAME)
        return 0;
    for (i = 0; i < n; i++)
        if (!((v[i] >= 'a' && v[i] <= 'z') || (v[i] >= 'A' && v[i] <= 'Z')
              || (v[i] >= '0' && v[i] <= '9') || v[i] == '-' || v[i] == '_'))
            return 0;
    return 1;
}

/* `mirror glass stream=i420 size=<w>x<h> fps=<n> [name=<n>] [overlay=1]`:
 * the glass as a raw stream on `s/<session>/glass`, or `glass-<name>`
 * where it is named, for a controller. A session may hold several, by
 * name, and a wall STREAMS of all of them. The frame's sides are even, as
 * 4:2:0 wants them, and no larger than the GPU draws. */
static void cmd_stream(Wall *w, Session *from, char **words, int n)
{
    char said[48], said2[48], name[STREAM_NAME + 1] = "";
    Screen *s = w->s;
    int i, fw = 0, fh = 0, fps = 0, overlay = 0;
    Raw *r;
    if (from->role != ROLE_CONTROLLER) {
        wsay(from, "error mirror not-for-role");
        return;
    }
    for (i = 2; i < n; i++)
        if (!strncmp(words[i], "stream=", 7))
            break;
    if (strcmp(words[1], "glass")) {
        wsay(from, "error mirror %s %s: only for the glass", plain(words[1], said, sizeof said),
             plain(words[i], said2, sizeof said2));
        return;
    }
    for (i = 2; i < n; i++) {
        char *v = words[i];
        if (!strncmp(v, "stream=", 7)) {
            if (strcmp(v + 7, "i420")) {
                wsay(from, "error mirror %s: not i420", plain(v, said, sizeof said));
                return;
            }
        } else if (!strncmp(v, "size=", 5)) {
            if (!side_pair(v + 5, &fw, &fh) || fw < 16 || fh < 16 || fw % 2 || fh % 2
                || (s->gpu && ((fw / 2 + 3) / 4 * 2 > s->gl.max_texture
                               || fh + fh / 2 > s->gl.max_texture))) {
                wsay(from, "error mirror %s: bad size", plain(v, said, sizeof said));
                return;
            }
        } else if (!strncmp(v, "fps=", 4)) {
            if (!digits(v + 4, 9)) {
                wsay(from, "error mirror %s: not a number", plain(v, said, sizeof said));
                return;
            }
            fps = atoi(v + 4);
            if (fps < 1 || fps > 60) {
                wsay(from, "error mirror %s: not 1 to 60", plain(v, said, sizeof said));
                return;
            }
        } else if (!strncmp(v, "name=", 5)) {
            if (!stream_name_ok(v + 5)) {
                wsay(from, "error mirror %s: bad name", plain(v, said, sizeof said));
                return;
            }
            snprintf(name, sizeof name, "%s", v + 5);
        } else if (!strncmp(v, "overlay=", 8)) {
            if (strcmp(v + 8, "0") && strcmp(v + 8, "1")) {
                wsay(from, "error mirror %s: not 0 or 1", plain(v, said, sizeof said));
                return;
            }
            overlay = v[8] == '1';
        } else {
            wsay(from, "error mirror %s: unknown option", plain(v, said, sizeof said));
            return;
        }
    }
    if (!fw) {
        wsay(from, "error mirror stream=i420: no size");
        return;
    }
    if (!fps) {
        wsay(from, "error mirror stream=i420: no fps");
        return;
    }
    if (!s->gpu || s->width > s->gl.max_texture || s->height > s->gl.max_texture) {
        wsay(from, "error mirror glass stream=i420: not offered");
        return;
    }
    for (i = 0; s->outs && i < s->outs->n; i++)
        if (s->outs->streams[i]->owner == from && !strcmp(s->outs->streams[i]->name, name)) {
            wsay(from, "error mirror busy");
            return;
        }
    if (s->outs && s->outs->n >= STREAMS) {
        wsay(from, "error mirror glass: %d streams already", STREAMS);
        return;
    }
    r = calloc(1, sizeof *r);
    if (!r) {
        wsay(from, "error mirror glass stream=i420: not offered");
        return;
    }
    r->owner = from;
    snprintf(r->name, sizeof r->name, "%s", name);
    r->w = fw;
    r->h = fh;
    r->fps = fps;
    r->overlay = overlay;
    r->every = 1000.0 / fps;
    r->due = now_ms();
    snprintf(r->path, sizeof r->path, "%s/glass%s%s", from->dir, name[0] ? "-" : "", name);
    if (raw_up(s, r) < 0)
        wsay(from, "error mirror glass stream=i420: not offered");
}

/* `mirror glass off name=<n>`: that one of the session's streams ended,
 * and nothing else. 0 where the words are not that. */
static int cmd_stream_off(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    if (n != 4 || strcmp(words[1], "glass") || strcmp(words[2], "off")
        || strncmp(words[3], "name=", 5))
        return 0;
    if (!outputs_stop(w->s, from, words[3] + 5))
        wsay(from, "error mirror %s: no such stream", plain(words[3], said, sizeof said));
    return 1;
}
