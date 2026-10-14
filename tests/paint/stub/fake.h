/*
 * The knobs and the evidence of the fakes: what the GPU and the card
 * claim to be, and what the painter did to them. Everything is reset by
 * fake_reset(), which each test gets before it runs.
 */
#ifndef FAKE_H
#define FAKE_H

#include <stdint.h>

/* A glyph the fake font draws carries its code point in the alpha of its
 * first three pixels, so a test can read back what a cell shows. The
 * second half of a wide glyph carries it too, marked as the tail. */
#define FAKE_MARK 0x80
#define FAKE_TAIL 0x40

/* How many of each the fakes keep. A test that makes more is told. */
#define FAKE_CONTEXTS 64
#define FAKE_TEXTURES 64
#define FAKE_BUFFERS 256
#define FAKE_PROGRAMS 128

typedef struct {
    uint32_t id, encoder, crtc;     /* crtc zero: the output is not driven */
    int connected, dpms;            /* dpms: it has the property */
    uint32_t type_id;               /* its HDMI-A number; zero: its place + 1 */
    int sets;                       /* DPMS set on this connector: how often */
    uint64_t value;                 /* and what it was last set to */
} FakeConnector;

/* One display as SDL lists it. */
typedef struct {
    int w, h;                       /* zero: win_w and win_h */
    int on;                         /* 1 + the conn[] it is on; zero: none */
} FakeDisplay;

/* One GL context, and what was done with it current. */
typedef struct {
    void *win;                      /* the window it was made for */
    int clears, draws, swaps, uploads;
    int cleared_since_swap;
    int blend;                      /* GL_BLEND is on */
    int blended;                    /* draws made with it on */
    unsigned texture;               /* bound just now */
    unsigned ntex;                  /* textures it has made */
} FakeContext;

/* A texture's storage, and the context that made it. GL numbers textures
 * per context, so each context's first is 1 whatever the others have:
 * `local` is that number. The name the fake hands out is unique across
 * contexts instead, so that a name used under the wrong context can be
 * caught; and it then acts on the current context's texture of the same
 * local number, as a real GL would, which is another wall's atlas. */
typedef struct {
    FakeContext *owner;
    unsigned local;
    uint8_t *pixels;
    int w, h;
    int bpp;                        /* 4 for an RGBA picture, 1 for a mask */
} FakeTexture;

typedef struct {
    int max_texture;                /* GL_MAX_TEXTURE_SIZE */
    float max_point;                /* the top of GL_ALIASED_POINT_SIZE_RANGE */
    int win_w, win_h;
    int ndisplays;                  /* zero: one display, win_w by win_h */
    FakeDisplay disp[4];

    uint8_t *atlas;                 /* the texture glTexImage2D made last */
    int atlas_w, atlas_h, atlas_made;

    FakeContext ctx[FAKE_CONTEXTS];
    int nctx;
    FakeContext *current;           /* as SDL_GL_MakeCurrent left it */
    FakeTexture tex[FAKE_TEXTURES]; /* by name; zero is no texture */
    FakeContext *buffer_owner[FAKE_BUFFERS], *program_owner[FAKE_PROGRAMS];
    int ntex, nbuffers, nprograms;
    int tex_deleted;                /* textures let go with glDeleteTextures */
    int wrong_context;             /* GL calls made for one context with
                                     * another current, or none */
    uint32_t fb[16];                /* the framebuffer each CRTC shows */

    int kmsdrm;                     /* whether the window is on KMS at all */
    int nconnectors;
    FakeConnector conn[4];
    int set_result;                 /* what drmModeConnectorSetProperty returns */
    int set_calls;
    uint32_t set_connector;
    uint64_t set_value;

    int clears, swaps;              /* glClear calls, and frames put up */
    unsigned framebuffer;           /* the one bound: zero is the window's */
    int swaps_uncleared;            /* frames put up without a clear first */
    int window_draws;               /* draws made into the window's framebuffer */
    int fb_draws;                   /* and into one of the painter's own */

    int es3;                        /* GL_VERSION says ES 3, and the pixel
                                     * buffer calls are there to be had */
    int read_pattern;               /* glReadPixels gives each byte a value
                                     * from its place, `read_byte`, not zero */
    int reads;                      /* glReadPixels calls */
    long read_bytes;                /* and the bytes they asked for */
    int read_into_buffer;           /* of those, read into a pixel buffer */
    int maps;                       /* pixel buffers mapped to be read */
    int threads;                    /* threads started */
    int framebuffers;               /* framebuffers made, less those let go */
    int fontless;                   /* the font has no glyph for anything */
} Fake;

extern Fake fake;

void fake_reset(void);

/* The storage of texture `name`, as the painter's atlas is read back. */
const FakeTexture *fake_texture(unsigned name);

/* The byte glReadPixels puts at byte `x` of row `y` under read_pattern. */
#define read_byte(x, y) ((uint8_t)((x) * 3 + (y) * 7 + 1))

/* Whether the fake font draws this code point two cells wide. */
int fake_wide(uint32_t cp);

#endif
