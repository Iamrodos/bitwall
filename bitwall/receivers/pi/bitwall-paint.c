/*
 * bitwall-paint: a wall painted straight onto the display, with no
 * terminal in the way, and nothing between it and the sender's pipe.
 *
 * Reads the byte stream a sender writes and puts it on the glass. Given
 * `--run`, it is the whole receiver: it keeps each wall's door, opens a
 * session for each caller, shows the status page while nobody is
 * sending, and honours blank and font requests. Given stdin instead, it
 * is only a painter, which is what a test or a benchmark wants.
 *
 * A wall is not a terminal. What arrives is already cells -- absolutely
 * positioned runs, a colour, and characters -- because every sender
 * composes them, and even a live tmux session is decoded into cells
 * before it leaves the sending machine. So the grammar to read is three
 * things: move the cursor, set a colour, draw a run. No scrollback, no
 * reflow, no selection, no wrapping, no mouse, no OSC. What a terminal
 * emulator spends its time on is precisely what a wall never asks for.
 *
 * Most cells are rectangles, not glyphs. Block and half-block code points
 * are geometry, and SHAPES says where in the cell the ink goes as
 * fractions of it; box drawing is lines from the middle of the cell to
 * its edges, and BOX says which edges and how heavy. Drawing those as
 * fills means the font is never consulted for the characters walls use
 * most, that a line meets its neighbour with no seam, and that two
 * receivers agree about what a block looks like rather than deferring to
 * whatever face each one happened to load. The same tables drive the web
 * receiver.
 *
 * On the GPU the picture is the grid itself. Every cell is one mask from
 * an atlas -- a block shape rasterised once per font, a shade as a flat
 * alpha, a glyph as the font drew it -- and two colours, and the shader
 * mixes ground into ink through the mask. The whole grid is redrawn
 * every frame in one call, straight into the back buffer: a persistent
 * canvas would cost this board more in memory traffic than drawing
 * fourteen thousand cells does, and a canvas is what a diff-only stream
 * would otherwise need. Frames are diffs, so the grid keeps what was not
 * mentioned.
 *
 * In software the window surface itself is the picture and presenting
 * pushes out the rectangles that changed. Both paths are here and the
 * choice is made at runtime, because a Pi has a GPU and the test driver
 * has none.
 *
 * The pipe is drained by a thread. Painting a frame takes tens of
 * milliseconds during which nothing reads, and a fifo is only 64 KB: a
 * sender writing through ssh stalls, the pipes empty, and then the
 * painter waits for bytes that are still on the far machine. A reader
 * that takes bytes the moment they land, into memory the size of many
 * frames, keeps both ends busy at once.
 *
 * Glyphs are cached by what they look like. A cell that is a real
 * character is rendered once and kept, because walls draw the same few
 * hundred combinations over and over -- a dashboard has one palette and
 * a film has almost no text at all.
 *
 * Everything that belongs to one screen is in one Screen struct, and
 * every function takes a pointer to it. The Wall around it owns the
 * door, its sessions and the conversation. A board with two outputs is
 * two walls in one process, each a Wall and its Screen: its own door,
 * directory, font, GL context and atlas, idle clock and blank, and
 * nothing shared but the loop that services them all. Wall N lives in
 * `bitwall/N/` in the run directory. Walls are numbered by DRM connector, in
 * the card's own order, not by whatever order SDL lists displays in.
 *
 * Above the base a wall holds up to LAYERS-1 video layers, each pictures
 * over the grid: each frame a QOI image, decoded and put over the cells at
 * real pixels, blended by its alpha, lowest layer first. On the GPU each
 * is a texture drawn after the grid; in software each is blitted onto the
 * glass for as long as it takes to push out, as the overlay is. Each
 * layer's `in` has a reader thread of its own, so a layer that falls
 * behind holds up only its own writer. A layer asks for a place, `above`,
 * `top` or a number, and is told the one it got. The base may be a video
 * layer too, over black glass.
 *
 * The thread that draws never waits on a picture: a layer's frames are
 * decoded, and pictures and mirrors of the glass encoded, by a pool of
 * worker threads (pool.c), and it only uploads and reads back. A layer's
 * frames go up in the order sent, and its other commands are acted on
 * after them.
 *
 * A video layer may have a stream on a fifo of its own, `data`: QOI
 * images back to back, decoded here, or HEVC, H.264 or MJPEG, decoded by
 * ffmpeg in a child process a stream, on the board's own decoder where it
 * has one, and read back as YUV planes that the GPU turns to colour as it
 * draws them. The wall plays a stream out on its own clock, frame i at
 * start + i/fps, so walls whose clocks agree show one film together.
 *
 * The glass goes out the same way, for a controller that asks: raw I420
 * frames, each stream at its own size and rate, one every 1/fps, on a
 * fifo beside its session, for an encoder to read; outputs.c owns them.
 * While any stream runs the scene is drawn once into a picture of its
 * own and copied to the screen, and every stream's frame is a pass or two
 * from that one picture and a read of a byte and a half a pixel, handed
 * to the stream's writer thread. BITWALL_STREAM_DRAW_TWICE=1 draws the
 * glass again for each frame instead, as a picture of it is, and turns it
 * to I420 here; BITWALL_STREAM_SYNC=1 reads each frame as it is drawn
 * where GLES 3 would otherwise read it a frame later.
 *
 * What it asks of the machine it runs on -- the display, the GPU context,
 * child processes, the decoders the board has and what the machine says
 * of itself -- is behind the seams in platform.h, each with an
 * implementation per platform that this file includes at its end; a Pi's
 * are display-kms.c, gpu-gles.c, child-posix.c, probe-v4l2.c and
 * machine-linux.c, a Mac's display-sdl.c, gpu-gl.c, child-posix.c,
 * probe-mac.c and machine-mac.c. The compositor above them is the same
 * everywhere. On a Mac it paints a window, or with `--glass WxH` no
 * screen at all, a glass that size that only streams and pictures look
 * at.
 *
 * Built by the Makefile beside it, on the Pi beside these files, where
 * bitwall-screen looks for it:
 *
 *     apt install libsdl2-dev libsdl2-ttf-dev libdrm-dev libgles2-mesa-dev libpng-dev \
 *         libjpeg-dev
 *     make
 *
 * and on a Mac, with Homebrew's sdl2, sdl2_ttf, libpng, jpeg-turbo and
 * ffmpeg, `make PLATFORM=mac`.
 *
 * which is one cc of this file, the rest coming in by #include:
 *
 *     cc -std=c99 -O2 -Wall -Wextra -DBITWALL_VERSION="\"$version\"" -DBITWALL_JPEG \
 *        -o bitwall-paint bitwall-paint.c \
 *        $(pkg-config --cflags --libs sdl2 SDL2_ttf libdrm glesv2 libpng libjpeg) -lpthread
 *
 * BITWALL_VERSION is what `receiver` says, the hash a controller compares
 * with its own copy of these files to tell a painter built from other
 * source; left out, it says `-`.
 *
 * A cells stream may carry kitty graphics, pictures placed in its cells:
 * each image decoded as it arrives, uploaded once as a texture, and each
 * placement drawn over the cells it covers as the grid is drawn, so it
 * moves with them when the font changes. They are the base session's, and
 * go when it does.
 *
 * tests/paint builds it against fakes of all four, and the real libpng,
 * to run anywhere.
 */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE

#ifdef BITWALL_WIN
#include "compat-win.h"
#else
#include <dirent.h>
#include <poll.h>
#include <ifaddrs.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#endif
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "SDL.h"
#include "SDL_opengles2.h"
#include "SDL_ttf.h"

/* The QOI decoder, Dominic Szablewski's reference qoi.h, vendored beside
 * this file under its MIT licence, which is at its head. */
#define QOI_NO_STDIO
#define QOI_IMPLEMENTATION
#include "qoi.h"

/* PNG for kitty graphics is the system's libpng, through its simplified
 * API: the pictures come from senders, and the system's copy is the one
 * that gets security updates with the rest of the OS. */
#include <png.h>

/* JPEG frames are the system's libjpeg, libjpeg-turbo on the Pis, for the
 * same reason. Built with -DBITWALL_JPEG and linked with it, the wall says
 * `jpeg` among the images it takes; built without, it does not. */
#ifdef BITWALL_JPEG
#include <setjmp.h>
#include <jpeglib.h>
#endif

/* ------------------------------------------------------------ constants */

/* Which build this is, as a report says it: the first twelve hex digits of
 * the SHA-256 of its sources one after the other, in the order the
 * Makefile and bitwall/receivers/__init__.py list them, passed in as it is
 * built, or `-` for a build that was not told. */
#ifndef BITWALL_VERSION
#define BITWALL_VERSION "-"
#endif

/* How many rendered glyphs to keep. A dashboard uses a few hundred
 * combinations and a film almost none, so this is generous; it exists so
 * that a wall which cycles through every colour cannot grow without end. */
#define CACHE 4096

/* Slots in the glyph table: twice the fill so that probes stay short. */
#define CACHE_SLOTS 8192

/* Bytes an escape sequence may span before it is judged never to end.
 * The longest a wall sends is a truecolour pair, under forty bytes. */
#define MAX_CSI 64

/* Bytes a command string may span, matching protocol.MAX_MESSAGE. */
#define MAX_APC 4096

/* Parameters kept from one escape sequence. */
#define MAX_PARAMS 32

/* Code points painted per fill of ground. A longer run is painted in
 * pieces, each with its own ground, which looks the same. */
#define RUN_CHUNK 512

/* Changed rectangles remembered between presents on the software path.
 * Past this many the whole picture is copied instead, which is cheaper
 * than remembering them anyway. */
#define DIRTY_MAX 8192

/* Cells per draw when each is four corners: their indices have to fit
 * in a short. A grid with more is drawn in as many calls as it takes. */
#define BATCH 16000

/* The smallest side the atlas is made with. It grows from here, in
 * powers of two, until it holds the grid twice over or the GPU will not
 * go further. */
#define ATLAS_MIN 64

/* A cell's entry in the record of what the grid shows, marking the
 * right-hand half of a wide glyph. Code points stop well short of it. */
#define TAIL 0x80000000u

/* A braille code point the sender marked faint (SGR 2): its unlit dots
 * are shown as well, in the cell's ground, with the wall's black between
 * the dots, the way a dot-matrix display's unlit dots glow faintly.
 * Carried in the code point, as TAIL is, so the mask, the glyph and the
 * record of what a cell shows all know it without a field of their own. */
#define FAINT 0x40000000u

/* The four diagonal halves of a cell, U+25E2-25E5: lower right, lower
 * left, upper left, upper right. Drawn as geometry out to the cell's own
 * corners, so a row of them makes one slope with no seam where a glyph
 * would stop short of the edge. */
#define TRIANGLE 0x25E2
#define ARC      0x256D   /* ╭ ╮ ╯ ╰, U+256D-2570 */

/* What a screen of fragments and a thousand vertices cost this GPU, in
 * milliseconds, measured on a Pi 3 and used only to choose between one
 * vertex per cell and four. Another board gets a slower choice at
 * worst, never a wrong picture. */
#define MS_PER_MEGAPIXEL 6.0
#define MS_PER_KVERTEX 0.19

/* The parse buffer. A sequence split by a read waits here for its other
 * half, and a frame is parsed in pieces this big. */
#define BUF_SIZE (1 << 20)

/* How much of the stream may sit in memory ahead of the painter. Several
 * frames at any size a wall uses, so the sender never sees the pipe full
 * while a frame is being painted. */
#define INBOX_MAX (8u << 20)

/* What the reader thread asks the pipe for at a time. */
#define READ_SIZE (1 << 18)

/* The most walls one painter drives: more outputs than any board here
 * has. */
#define MAX_WALLS 8

/* The conversation's timers, as the reference daemon had them: silence
 * before we ask whether the sender is there, how long it has to answer,
 * and silence that outlasts the repaint a sender promises. */
#define QUIET 5.0
#define ANSWER 2.0
#define STALE 35.0
#define DARK_ASK 60.0     /* how often a wall nobody is watching asks */
#define STATUS_PERIOD 1.0
/* How long a new size of the glass holds before the wall takes it: a
 * window dragged is many sizes, and only the one it stops at is drawn. */
#define SETTLE 300.0
/* The smallest glass a controller may ask for, each way. */
#define GLASS_MIN 64
/* How long a wall with nobody sending to it keeps showing its status
 * page before going dark. The escalation belongs to the receiver rather
 * than the protocol: it has to still work when the sender has vanished,
 * which is exactly when the screen should go off, so a sender that is
 * no longer there cannot be the thing that decides. */
#define DOZE 300.0
/* How long the glass stays black before the monitor itself is put to
 * sleep. Black is instant and comes back instantly; standby saves the
 * panel but takes a moment to wake, so it follows rather than replaces. */
#define STANDBY 60.0

/* Mirrors: at most this many sessions at once, since each keeps a copy
 * of the grid and costs a comparison of it every frame; every 100ms
 * unless asked otherwise, and never faster than every 50. */
#define MAX_MIRRORS 4       /* on a wall */
#define MIRRORS 2           /* for any one session */
#define MIRROR_EVERY 100.0
#define MIRROR_FASTEST 50

/* A picture or a mirror of the glass is QOI, or JPEG where `format=jpeg`
 * asks and the wall was built with libjpeg, at JPEG_QUALITY where it says
 * no `quality`; `report` says which in `pictures`. */
#define JPEG_QUALITY 85
#ifdef BITWALL_JPEG
#define PICTURES_MADE "qoi jpeg"
#else
#define PICTURES_MADE "qoi"
#endif

/* What a sender may say about itself: keys, how long each may be, and
 * how much of it in all. Small enough that a wall cannot be filled by
 * something that only had to be able to write a pipe. */
#define META_KEYS 32
#define META_KEY 33
#define META_VALUE 257
#define META_TOTAL 4096

/* How often the overlay's numbers are worked out and put up, in ms, and
 * how far in from the edge of the glass it sits, in pixels. */
#define STATS_PERIOD 500.0
#define STATS_INSET 8

/* The layers a wall takes, the base included: the base at z=0 and video
 * layers above it, 1 to LAYERS-1. Every table of layers is this long. */
#define LAYERS 8
/* A video frame's limits, and how long it stays up when `hold` is not
 * said. */
#define FRAME_SIDE 4096
#define FRAME_MAX (16u << 20)
#define HOLD_MS 1000
/* The most a video layer's reader holds that the painter has not taken:
 * small, so a painter that falls behind fills the writer's pipe and the
 * sender drops frames, rather than a queue of them building up here. */
#define VIDEO_INBOX (256u << 10)

typedef struct { uint8_t r, g, b; } Rgb;

/* The default colours, and what `default` in a cell resolves to. */
static const Rgb INK = {204, 204, 204};
static const Rgb GROUND = {0, 0, 0};
static const SDL_Color WHITE = {255, 255, 255, 255};

/* -------------------------------------------------------------- shapes */

/* Where the ink goes inside a cell, as fractions of it. A character with
 * a shape is never asked of the font. */
typedef struct { double x, y, w, h; } Frac;

#define MAXRECTS 8          /* the most a shape is made of: a double cross, or braille */

typedef struct {
    int n;                  /* rectangles in this shape; zero means none */
    Frac r[MAXRECTS];
} Shape;

#define SHAPE_FIRST 0x2500
#define SHAPE_LAST  0x259F
#define SHAPE_LEDS  0x25AA  /* the leds emitter, kept beside the blocks */
#define BRAILLE     0x2800  /* U+2800-28FF, eight dots to a cell */
#define SHAPE_DOTS  (SHAPE_LAST - SHAPE_FIRST + 2)   /* braille's first slot */
/* The blocks of Symbols for Legacy Computing: sextants, the eighth lines
 * and fractions, and the centred and middle quarters (U+1FB00-1FB3B,
 * U+1FB70-1FB8B, U+1FBE4-1FBE7; the slopes and the rest of the block are
 * the font's); octants (U+1CD00-1CDE5); and the quarter bars
 * (U+1CEA0-1CEAF). Solid fills on a finer grid than the quadrants, drawn
 * for the same reason the quadrants are. With the blocks above, every
 * pattern of a cell cut two across and four down is one of these. */
#define LEGACY      0x1FB00
#define NLEGACY     0xE8
#define OCTANT      0x1CD00
#define NOCTANT     0xE6
#define QUARTER     0x1CEA0
#define NQUARTER    0x10
#define SHAPE_MORE  (SHAPE_DOTS + 0x100)             /* the legacy blocks' first slot */
#define NSHAPES (SHAPE_MORE + NLEGACY + NOCTANT + NQUARTER)
#define NBOX 0x80           /* U+2500-257F, the box drawing block */

static Shape SHAPES[NSHAPES];

/* Box drawing as arm weights [up, right, down, left]: 0 none, 1 light,
 * 2 heavy, 3 double. Drawn as lines from the middle of the cell out to
 * its edges, because a glyph stops short of the edge and a maze or a
 * border drawn in type is a row of dashes. Rounded corners are arcs of
 * their own (arc_in); dashed and diagonal lines are not here and stay
 * glyphs. */
enum { UP, RIGHT, DOWN, LEFT };
#define L_ 1
#define H_ 2
#define D_ 3
static const uint8_t BOX[NBOX][4] = {
    [0x00] = {0, L_, 0, L_}, [0x01] = {0, H_, 0, H_},
    [0x02] = {L_, 0, L_, 0}, [0x03] = {H_, 0, H_, 0},
    [0x0c] = {0, L_, L_, 0}, [0x0d] = {0, H_, L_, 0},
    [0x0e] = {0, L_, H_, 0}, [0x0f] = {0, H_, H_, 0},
    [0x10] = {0, 0, L_, L_}, [0x11] = {0, 0, L_, H_},
    [0x12] = {0, 0, H_, L_}, [0x13] = {0, 0, H_, H_},
    [0x14] = {L_, L_, 0, 0}, [0x15] = {L_, H_, 0, 0},
    [0x16] = {H_, L_, 0, 0}, [0x17] = {H_, H_, 0, 0},
    [0x18] = {L_, 0, 0, L_}, [0x19] = {L_, 0, 0, H_},
    [0x1a] = {H_, 0, 0, L_}, [0x1b] = {H_, 0, 0, H_},
    [0x1c] = {L_, L_, L_, 0}, [0x1d] = {L_, H_, L_, 0},
    [0x1e] = {H_, L_, L_, 0}, [0x1f] = {L_, L_, H_, 0},
    [0x20] = {H_, L_, H_, 0}, [0x21] = {H_, H_, L_, 0},
    [0x22] = {L_, H_, H_, 0}, [0x23] = {H_, H_, H_, 0},
    [0x24] = {L_, 0, L_, L_}, [0x25] = {L_, 0, L_, H_},
    [0x26] = {H_, 0, L_, L_}, [0x27] = {L_, 0, H_, L_},
    [0x28] = {H_, 0, H_, L_}, [0x29] = {H_, 0, L_, H_},
    [0x2a] = {L_, 0, H_, H_}, [0x2b] = {H_, 0, H_, H_},
    [0x2c] = {0, L_, L_, L_}, [0x2d] = {0, L_, L_, H_},
    [0x2e] = {0, H_, L_, L_}, [0x2f] = {0, H_, L_, H_},
    [0x30] = {0, L_, H_, L_}, [0x31] = {0, L_, H_, H_},
    [0x32] = {0, H_, H_, L_}, [0x33] = {0, H_, H_, H_},
    [0x34] = {L_, L_, 0, L_}, [0x35] = {L_, L_, 0, H_},
    [0x36] = {L_, H_, 0, L_}, [0x37] = {L_, H_, 0, H_},
    [0x38] = {H_, L_, 0, L_}, [0x39] = {H_, L_, 0, H_},
    [0x3a] = {H_, H_, 0, L_}, [0x3b] = {H_, H_, 0, H_},
    [0x3c] = {L_, L_, L_, L_}, [0x3d] = {L_, L_, L_, H_},
    [0x3e] = {L_, H_, L_, L_}, [0x3f] = {L_, H_, L_, H_},
    [0x40] = {H_, L_, L_, L_}, [0x41] = {L_, L_, H_, L_},
    [0x42] = {H_, L_, H_, L_}, [0x43] = {H_, L_, L_, H_},
    [0x44] = {H_, H_, L_, L_}, [0x45] = {L_, L_, H_, H_},
    [0x46] = {L_, H_, H_, L_}, [0x47] = {H_, H_, L_, H_},
    [0x48] = {L_, H_, H_, H_}, [0x49] = {H_, L_, H_, H_},
    [0x4a] = {H_, H_, H_, L_}, [0x4b] = {H_, H_, H_, H_},
    [0x50] = {0, D_, 0, D_}, [0x51] = {D_, 0, D_, 0},
    [0x52] = {0, D_, L_, 0}, [0x53] = {0, L_, D_, 0},
    [0x54] = {0, D_, D_, 0}, [0x55] = {0, 0, L_, D_},
    [0x56] = {0, 0, D_, L_}, [0x57] = {0, 0, D_, D_},
    [0x58] = {L_, D_, 0, 0}, [0x59] = {D_, L_, 0, 0},
    [0x5a] = {D_, D_, 0, 0}, [0x5b] = {L_, 0, 0, D_},
    [0x5c] = {D_, 0, 0, L_}, [0x5d] = {D_, 0, 0, D_},
    [0x5e] = {L_, D_, L_, 0}, [0x5f] = {D_, L_, D_, 0},
    [0x60] = {D_, D_, D_, 0}, [0x61] = {L_, 0, L_, D_},
    [0x62] = {D_, 0, D_, L_}, [0x63] = {D_, 0, D_, D_},
    [0x64] = {0, D_, L_, D_}, [0x65] = {0, L_, D_, L_},
    [0x66] = {0, D_, D_, D_}, [0x67] = {L_, D_, 0, D_},
    [0x68] = {D_, L_, 0, L_}, [0x69] = {D_, D_, 0, D_},
    [0x6a] = {L_, D_, L_, D_}, [0x6b] = {D_, L_, D_, L_},
    [0x6c] = {D_, D_, D_, D_},
    [0x74] = {0, 0, 0, L_}, [0x75] = {L_, 0, 0, 0},
    [0x76] = {0, L_, 0, 0}, [0x77] = {0, 0, L_, 0},
    [0x78] = {0, 0, 0, H_}, [0x79] = {H_, 0, 0, 0},
    [0x7a] = {0, H_, 0, 0}, [0x7b] = {0, 0, H_, 0},
    [0x7c] = {0, H_, 0, L_}, [0x7d] = {L_, 0, H_, 0},
    [0x7e] = {0, L_, 0, H_}, [0x7f] = {H_, 0, L_, 0},
};
#undef L_
#undef H_
#undef D_

/* The slot a code point's shape would have, or -1 for one with none. */
static int shape_slot(uint32_t cp)
{
    if (cp >= SHAPE_FIRST && cp <= SHAPE_LAST)
        return (int)(cp - SHAPE_FIRST);
    if (cp == SHAPE_LEDS)
        return SHAPE_DOTS - 1;
    if (cp >= BRAILLE && cp < BRAILLE + 0x100)
        return SHAPE_DOTS + (int)(cp - BRAILLE);
    if (cp >= LEGACY && cp < LEGACY + NLEGACY)
        return SHAPE_MORE + (int)(cp - LEGACY);
    if (cp >= OCTANT && cp < OCTANT + NOCTANT)
        return SHAPE_MORE + NLEGACY + (int)(cp - OCTANT);
    if (cp >= QUARTER && cp < QUARTER + NQUARTER)
        return SHAPE_MORE + NLEGACY + NOCTANT + (int)(cp - QUARTER);
    return -1;
}

/* The index of a code point's shape, or -1 where the font must draw it. */
static int shape_index(uint32_t cp)
{
    int i = shape_slot(cp);
    const uint8_t *a;
    if (i < 0)
        return -1;
    if (i >= NBOX)
        return SHAPES[i].n ? i : -1;
    a = BOX[i];
    return a[0] | a[1] | a[2] | a[3] ? i : -1;
}

static void shape_add(uint32_t cp, double x, double y, double w, double h)
{
    Shape *s = &SHAPES[shape_slot(cp)];
    if (s->n < MAXRECTS) {
        s->r[s->n].x = x; s->r[s->n].y = y;
        s->r[s->n].w = w; s->r[s->n].h = h;
        s->n++;
    }
}

/* A pattern of a cell cut two across and `rows` down, bit b the b+1th
 * part counted along each row from the top left, as the characters' own
 * names count them. Rows alike and next to each other are one rectangle. */
static void grid_add(uint32_t cp, int bits, int rows)
{
    int r = 0;
    while (r < rows) {
        int row = bits >> (2 * r) & 3, end = r + 1;
        while (end < rows && (bits >> (2 * end) & 3) == row)
            end++;
        if (row)
            shape_add(cp, row == 2 ? 0.5 : 0, (double)r / rows,
                      row == 3 ? 1 : 0.5, (double)(end - r) / rows);
        r = end;
    }
}

/* Symbols for Legacy Computing's solid blocks. Sextants are every pattern
 * of a 2x3 cell but the four other characters draw (empty, full, the
 * left and right halves), in order; octants every pattern of a 2x4 cell
 * but the 26 other characters draw, in order: the halves, the quadrants,
 * the quarters of a row or a column, the three quarters, the corner
 * quarter bars and the middle quarters. */
static void legacy_init(void)
{
    static const uint8_t drawn[] = {
        0x00, 0x01, 0x02, 0x03, 0x05, 0x0a, 0x0f, 0x14, 0x28, 0x3f, 0x40, 0x50, 0x55,
        0x5a, 0x5f, 0x80, 0xa0, 0xa5, 0xaa, 0xaf, 0xc0, 0xf0, 0xf5, 0xfa, 0xfc, 0xff};
    /* The quarter bars, U+1CEA0-1CEAF: [x, y, w, h] in quarters. */
    static const uint8_t bars[16][4] = {
        {2, 3, 2, 1}, {1, 3, 3, 1}, {0, 3, 3, 1}, {0, 3, 2, 1},
        {0, 2, 1, 2}, {0, 1, 1, 3}, {0, 0, 1, 3}, {0, 0, 1, 2},
        {0, 0, 2, 1}, {0, 0, 3, 1}, {1, 0, 3, 1}, {2, 0, 2, 1},
        {3, 0, 1, 2}, {3, 0, 1, 3}, {3, 1, 1, 3}, {3, 2, 1, 2}};
    /* The upper and right fractions, U+1FB82-1FB8B, in eighths. */
    static const int parts[5] = {2, 3, 5, 6, 7};
    uint32_t cp = LEGACY;
    int b, i, k;
    for (b = 1; b < 63; b++)
        if (b != 21 && b != 42)
            grid_add(cp++, b, 3);
    for (cp = OCTANT, b = 0; b < 256; b++) {
        for (k = 0; k < (int)sizeof drawn && drawn[k] != b; k++)
            ;
        if (k == (int)sizeof drawn)
            grid_add(cp++, b, 4);
    }
    for (i = 0; i < 16; i++)
        shape_add(QUARTER + (uint32_t)i, bars[i][0] / 4.0, bars[i][1] / 4.0,
                  bars[i][2] / 4.0, bars[i][3] / 4.0);
    for (i = 0; i < 6; i++) {                      /* one eighth, 2nd to 7th */
        shape_add(0x1FB70 + (uint32_t)i, (i + 1) / 8.0, 0, 1 / 8.0, 1);
        shape_add(0x1FB76 + (uint32_t)i, 0, (i + 1) / 8.0, 1, 1 / 8.0);
    }
    shape_add(0x1FB7C, 0, 0, 1 / 8.0, 7 / 8.0);    /* left and lower */
    shape_add(0x1FB7C, 0, 7 / 8.0, 1, 1 / 8.0);
    shape_add(0x1FB7D, 0, 0, 1, 1 / 8.0);          /* left and upper */
    shape_add(0x1FB7D, 0, 1 / 8.0, 1 / 8.0, 7 / 8.0);
    shape_add(0x1FB7E, 0, 0, 1, 1 / 8.0);          /* right and upper */
    shape_add(0x1FB7E, 7 / 8.0, 1 / 8.0, 1 / 8.0, 7 / 8.0);
    shape_add(0x1FB7F, 7 / 8.0, 0, 1 / 8.0, 7 / 8.0);   /* right and lower */
    shape_add(0x1FB7F, 0, 7 / 8.0, 1, 1 / 8.0);
    shape_add(0x1FBE4, 0.25, 0, 0.5, 0.5);         /* upper centre quarter */
    shape_add(0x1FBE5, 0.25, 0.5, 0.5, 0.5);       /* lower centre */
    shape_add(0x1FBE6, 0, 0.25, 0.5, 0.5);         /* middle left */
    shape_add(0x1FBE7, 0.5, 0.25, 0.5, 0.5);       /* middle right */
    shape_add(0x1FB80, 0, 0, 1, 1 / 8.0);          /* upper and lower */
    shape_add(0x1FB80, 0, 7 / 8.0, 1, 1 / 8.0);
    for (i = 0; i < 4; i++)                        /* rows 1, 3, 5 and 8 */
        shape_add(0x1FB81, 0, (i == 3 ? 7 : 2 * i) / 8.0, 1, 1 / 8.0);
    for (i = 0; i < 5; i++) {
        shape_add(0x1FB82 + (uint32_t)i, 0, 0, 1, parts[i] / 8.0);
        shape_add(0x1FB87 + (uint32_t)i, 1 - parts[i] / 8.0, 0, parts[i] / 8.0, 1);
    }
}

static void shapes_init(void)
{
    int i;
    memset(SHAPES, 0, sizeof SHAPES);
    shape_add(0x2588, 0, 0, 1, 1);                 /* full block */
    shape_add(0x2580, 0, 0, 1, 0.5);               /* upper half */
    shape_add(0x2584, 0, 0.5, 1, 0.5);             /* lower half */
    shape_add(0x258c, 0, 0, 0.5, 1);               /* left half */
    shape_add(0x2590, 0.5, 0, 0.5, 1);             /* right half */
    shape_add(0x2596, 0, 0.5, 0.5, 0.5);           /* quadrants */
    shape_add(0x2597, 0.5, 0.5, 0.5, 0.5);
    shape_add(0x2598, 0, 0, 0.5, 0.5);
    shape_add(0x2599, 0, 0, 0.5, 1);
    shape_add(0x2599, 0.5, 0.5, 0.5, 0.5);
    shape_add(0x259a, 0, 0, 0.5, 0.5);
    shape_add(0x259a, 0.5, 0.5, 0.5, 0.5);
    shape_add(0x259b, 0, 0, 1, 0.5);
    shape_add(0x259b, 0, 0.5, 0.5, 0.5);
    shape_add(0x259c, 0, 0, 1, 0.5);
    shape_add(0x259c, 0.5, 0.5, 0.5, 0.5);
    shape_add(0x259d, 0.5, 0, 0.5, 0.5);
    shape_add(0x259e, 0.5, 0, 0.5, 0.5);
    shape_add(0x259e, 0, 0.5, 0.5, 0.5);
    shape_add(0x259f, 0.5, 0, 0.5, 1);
    shape_add(0x259f, 0, 0.5, 0.5, 0.5);
    shape_add(SHAPE_LEDS, 0.1, 0.3, 0.8, 0.4);     /* the leds emitter */
    /* Braille as dots on an even grid: two across and four down, each a
     * square 0.6 of its slot and centred in it, as the font draws them.
     * The font's are placed within its own line height, which a cell is
     * taller than, so typed braille leaves a band between rows; these
     * space the same across a cell boundary as within one. Bit b of the
     * code point is dot b+1: 1-3 down the left, 4-6 down the right, then
     * 7 and 8 along the bottom. */
    {
        static const int col[8] = {0, 0, 0, 1, 1, 1, 0, 1};
        static const int row[8] = {0, 1, 2, 0, 1, 2, 3, 3};
        int b, bit;
        for (b = 1; b < 0x100; b++)
            for (bit = 0; bit < 8; bit++)
                if (b & (1 << bit))
                    shape_add(BRAILLE + (uint32_t)b, col[bit] * 0.5 + 0.1,
                              row[bit] * 0.25 + 0.05, 0.3, 0.15);
    }
    for (i = 1; i < 8; i++) {                      /* eighths, up and across */
        if (i == 4)
            continue;                              /* the halves, above */
        shape_add(0x2580 + i, 0, 1 - i / 8.0, 1, i / 8.0);
        shape_add(0x2588 + i, 0, 0, (8 - i) / 8.0, 1);
    }
    shape_add(0x2594, 0, 0, 1, 1 / 8.0);           /* upper one eighth */
    shape_add(0x2595, 7 / 8.0, 0, 1 / 8.0, 1);     /* right one eighth */
    legacy_init();
}

/* A fraction of a cell in pixels. Each edge is rounded on its own, so two
 * halves meet on the same pixel whatever the size and nothing between
 * them is left unpainted. */
static void frac_px(const Frac *r, int cw, int ch, SDL_Rect *p)
{
    int x0 = (int)(r->x * cw + 0.5), x1 = (int)((r->x + r->w) * cw + 0.5);
    int y0 = (int)(r->y * ch + 0.5), y1 = (int)((r->y + r->h) * ch + 0.5);
    p->x = x0;
    p->y = y0;
    p->w = x1 - x0 > 1 ? x1 - x0 : 1;
    p->h = y1 - y0 > 1 ? y1 - y0 : 1;
}

/* A braille pattern's dots in pixels. Every dot the same whole number of
 * pixels, 0.6 of its slot, and centred in it: a dot's edges each rounded
 * on their own would make some a pixel bigger than others, which a screen
 * of them shows as a ragged grid. */
static int dots_px(int bits, int cw, int ch, SDL_Rect *p)
{
    static const int col[8] = {0, 0, 0, 1, 1, 1, 0, 1};
    static const int row[8] = {0, 1, 2, 0, 1, 2, 3, 3};
    int dw = (int)(cw * 0.3 + 0.5), dh = (int)(ch * 0.15 + 0.5), bit, n = 0;
    if (dw < 1) dw = 1;
    if (dh < 1) dh = 1;
    for (bit = 0; bit < 8; bit++) {
        if (!(bits & (1 << bit)))
            continue;
        p[n].x = (int)((col[bit] * 0.5 + 0.25) * cw + 0.5) - dw / 2;
        p[n].y = (int)((row[bit] * 0.25 + 0.125) * ch + 0.5) - dh / 2;
        p[n].w = dw;
        p[n].h = dh;
        n++;
    }
    return n;
}

/* Whether pixel (x, y) of a cell cw by ch lies in triangle `which`, 0-3
 * from TRIANGLE, judged by its centre. A triangle and the one that
 * completes its cell share every pixel between them, one each, so two
 * halves in two colours leave neither a gap nor an overlap. */
static int tri_in(int which, int x, int y, int cw, int ch)
{
    double u = (x + 0.5) / cw, v = (y + 0.5) / ch;
    switch (which) {
    case 0: return u + v >= 1;      /* lower right */
    case 1: return v >= u;          /* lower left */
    case 2: return u + v < 1;       /* upper left */
    default: return v < u;          /* upper right */
    }
}

/* A triangle as one rectangle a pixel row, at most `room` of them.
 * Returns how many. */
static int tri_px(int which, int cw, int ch, SDL_Rect *out, int room)
{
    int x, y, n = 0;
    for (y = 0; y < ch && n < room; y++) {
        int lo = -1, hi = -1;
        for (x = 0; x < cw; x++)
            if (tri_in(which, x, y, cw, ch)) {
                if (lo < 0)
                    lo = x;
                hi = x;
            }
        if (lo < 0)
            continue;
        out[n].x = lo;
        out[n].y = y;
        out[n].w = hi - lo + 1;
        out[n].h = 1;
        n++;
    }
    return n;
}

typedef struct { int lo, hi; } Span;

static int strokes(int weight, int len, int light, int heavy, Span out[2]);

/* Whether pixel x, y of a cell cw by ch is on arc `which` of ╭ ╮ ╯ ╰ at
 * font size `size`: a quarter ellipse centred on the cell's corner, from
 * the middle of one edge to the middle of the next, through the centre
 * lines of the light strokes that meet it and as thick as they are, so a
 * rounded box joins its straight sides with no step. */
static int arc_in(int which, int x, int y, int cw, int ch, int size)
{
    int light = (int)(size / 12.0 + 0.5), heavy = (int)(size / 5.0 + 0.5);
    Span v[2], h[2];
    double xc, yc, cx, cy, rx, ry, dx, dy, f, gx, gy, half;
    if (light < 1)
        light = 1;
    if (heavy < 2)
        heavy = 2;
    strokes(1, cw, light, heavy, v);
    strokes(1, ch, light, heavy, h);
    xc = (v[0].lo + v[0].hi) / 2.0;
    yc = (h[0].lo + h[0].hi) / 2.0;
    /* ╭ down and right, ╮ down and left, ╯ up and left, ╰ up and right. */
    cx = which == 0 || which == 3 ? cw : 0;
    cy = which < 2 ? ch : 0;
    rx = cx > 0 ? cw - xc : xc;
    ry = cy > 0 ? ch - yc : yc;
    if (rx <= 0 || ry <= 0)
        return 0;
    dx = x + 0.5 - cx;
    dy = y + 0.5 - cy;
    /* The distance to the ellipse, near it, is the ellipse's equation
     * over the length of its gradient; compared squared, so there is no
     * root to take. */
    f = dx * dx / (rx * rx) + dy * dy / (ry * ry) - 1;
    gx = 2 * dx / (rx * rx);
    gy = 2 * dy / (ry * ry);
    half = light / 2.0;
    return f * f <= half * half * (gx * gx + gy * gy);
}

/* An arc as one rectangle a pixel row, at most `room` of them. */
static int arc_px(int which, int cw, int ch, int size, SDL_Rect *out, int room)
{
    int x, y, n = 0;
    for (y = 0; y < ch && n < room; y++) {
        int lo = -1, hi = -1;
        for (x = 0; x < cw; x++)
            if (arc_in(which, x, y, cw, ch, size)) {
                if (lo < 0)
                    lo = x;
                hi = x;
            }
        if (lo < 0)
            continue;
        out[n].x = lo;
        out[n].y = y;
        out[n].w = hi - lo + 1;
        out[n].h = 1;
        n++;
    }
    return n;
}

/* Where an arm's lines lie across it, in a cell `len` pixels that way:
 * one span for a light or heavy arm, two for a double, each centred the
 * same way whatever the character so that neighbours line up. */
static int strokes(int weight, int len, int light, int heavy, Span out[2])
{
    int t = weight == 2 ? heavy : light, n = weight == 3 ? 2 : 1, i;
    int gap = light, width = n == 2 ? 2 * t + gap : t;
    int lo = (len - width) / 2;
    if (lo < 0)
        lo = 0;
    out[0].lo = lo;
    out[0].hi = lo + t;
    if (n == 2) {
        out[1].lo = lo + t + gap;
        out[1].hi = lo + 2 * t + gap;
    }
    for (i = 0; i < n; i++) {
        if (out[i].hi > len)
            out[i].hi = len;
        if (out[i].lo > out[i].hi)
            out[i].lo = out[i].hi;
    }
    return n;
}

/* How far a line of an arm reaches into the middle, measured along the
 * arm. `far` says which end of the perpendicular lines to stop at, the
 * one past them (1) or the one before them (0).
 *
 * A single line stops across whatever crosses it, so a corner or a tee
 * is closed. One line of a double pair turns into the nearest line of
 * the arm on its own side; with nothing on that side it runs straight on
 * if the arm continues, and otherwise turns into the far line of the arm
 * opposite, which is the outside of a corner. */
static int reach(const uint8_t *a, int arm, int side, int other, int n,
                 int len, int light, int heavy, int far)
{
    Span sp[2], sq[2];
    int k, m, best = far ? -1 : INT_MAX, ahead = (arm + 2) % 4, mid = len / 2;
    if (n == 1) {
        int sides[2] = {side, other}, s;
        for (s = 0; s < 2; s++) {
            if (!a[sides[s]])
                continue;
            m = strokes(a[sides[s]], len, light, heavy, sp);
            for (k = 0; k < m; k++)
                best = far ? (sp[k].hi > best ? sp[k].hi : best)
                           : (sp[k].lo < best ? sp[k].lo : best);
        }
        if (best != -1 && best != INT_MAX)
            return best;
        if (a[ahead])
            return mid;
        strokes(a[arm], len, light, heavy, sq);
        return far ? sq[0].hi : sq[0].lo;
    }
    if (a[side]) {
        m = strokes(a[side], len, light, heavy, sp);
        return far ? sp[0].hi : sp[m - 1].lo;
    }
    if (a[ahead])
        return mid;
    if (a[other]) {
        m = strokes(a[other], len, light, heavy, sp);
        return far ? sp[m - 1].hi : sp[0].lo;
    }
    return mid;
}

/* Merge rectangles that line up and touch, so a straight line is one
 * rectangle the width of the cell and may be filled across a whole run. */
static int rects_merge(SDL_Rect *r, int n)
{
    int i, j, again = 1;
    while (again) {
        again = 0;
        for (i = 0; i < n && !again; i++)
            for (j = i + 1; j < n && !again; j++) {
                SDL_Rect *p = &r[i], *q = &r[j];
                if (p->y == q->y && p->h == q->h
                    && p->x <= q->x + q->w && q->x <= p->x + p->w) {
                    int x0 = p->x < q->x ? p->x : q->x;
                    int x1 = p->x + p->w > q->x + q->w ? p->x + p->w : q->x + q->w;
                    p->x = x0;
                    p->w = x1 - x0;
                } else if (p->x == q->x && p->w == q->w
                           && p->y <= q->y + q->h && q->y <= p->y + p->h) {
                    int y0 = p->y < q->y ? p->y : q->y;
                    int y1 = p->y + p->h > q->y + q->h ? p->y + p->h : q->y + q->h;
                    p->y = y0;
                    p->h = y1 - y0;
                } else {
                    continue;
                }
                r[j] = r[--n];
                again = 1;
            }
    }
    return n;
}

/* A box drawing character as rectangles in a cell `cw` by `ch`, with
 * lines as thick as a font of `size` would draw them. Returns how many. */
static int box_px(const uint8_t *a, int cw, int ch, int size, SDL_Rect *out)
{
    int light = (int)(size / 12.0 + 0.5), heavy = (int)(size / 5.0 + 0.5);
    int arm, j, n, m = 0;
    Span sp[2];
    if (light < 1)
        light = 1;
    if (heavy < 2)
        heavy = 2;
    for (arm = 0; arm < 4; arm++) {
        int vertical = arm == UP || arm == DOWN;
        /* The arms either side of this one: for a vertical arm the left
         * line of a pair is on the left, for a horizontal one the top
         * line is on the upper side. */
        int first = vertical ? LEFT : UP, second = vertical ? RIGHT : DOWN;
        int len = vertical ? ch : cw, towards = arm == UP || arm == LEFT;
        if (!a[arm])
            continue;
        n = strokes(a[arm], vertical ? cw : ch, light, heavy, sp);
        for (j = 0; j < n; j++) {
            int side = j == 0 ? first : second, other = j == 0 ? second : first;
            int at = reach(a, arm, side, other, n, len, light, heavy, towards);
            int lo = towards ? 0 : at, hi = towards ? at : len;
            SDL_Rect *r = &out[m];
            if (hi <= lo || sp[j].hi <= sp[j].lo)
                continue;
            if (vertical) {
                r->x = sp[j].lo; r->w = sp[j].hi - sp[j].lo;
                r->y = lo; r->h = hi - lo;
            } else {
                r->y = sp[j].lo; r->h = sp[j].hi - sp[j].lo;
                r->x = lo; r->w = hi - lo;
            }
            m++;
        }
    }
    return rects_merge(out, m);
}
/* Shading is a blend rather than a shape: the ink at that strength over
 * the ground, which is what the character means and what a font would
 * draw as a dot pattern too fine to survive a small cell. Zero means the
 * code point is not a shade. */
static double shade_of(uint32_t cp)
{
    switch (cp) {
    case 0x2591: return 0.25;
    case 0x2592: return 0.5;
    case 0x2593: return 0.75;
    default: return 0;
    }
}

/* ------------------------------------------------------------- palette */

/* The xterm 256-colour cube, built rather than tabulated. */
static Rgb PALETTE[256];

static void palette_init(void)
{
    static const Rgb named[16] = {
        {0, 0, 0}, {170, 0, 0}, {0, 170, 0}, {170, 85, 0},
        {0, 0, 170}, {170, 0, 170}, {0, 170, 170}, {170, 170, 170},
        {85, 85, 85}, {255, 85, 85}, {85, 255, 85}, {255, 255, 85},
        {85, 85, 255}, {255, 85, 255}, {85, 255, 255}, {255, 255, 255}};
    static const uint8_t steps[6] = {0, 95, 135, 175, 215, 255};
    int i, r, g, b, n = 16;
    memcpy(PALETTE, named, sizeof named);
    for (r = 0; r < 6; r++)
        for (g = 0; g < 6; g++)
            for (b = 0; b < 6; b++) {
                PALETTE[n].r = steps[r];
                PALETTE[n].g = steps[g];
                PALETTE[n].b = steps[b];
                n++;
            }
    for (i = 0; i < 24; i++) {
        uint8_t v = (uint8_t)(8 + i * 10);
        PALETTE[n].r = v; PALETTE[n].g = v; PALETTE[n].b = v;
        n++;
    }
}

/* ----------------------------------------------------------------- pen */

/* The colours a run is drawn in, as the last SGR left them. */
typedef struct {
    Rgb fg, bg;
    int fg_set, bg_set;     /* unset means the screen's own default */
    int bold, reverse;
    int faint;              /* braille shows its unlit dots */
    Rgb ink, ground;        /* what this pen paints, resolved once */
} Pen;

/* Work out the pair this pen paints, once per colour change. A run asks
 * for its colours and there are far more runs than there are changes of
 * colour, so the resolving of defaults and of reverse happens here. */
static void pen_settle(Pen *p)
{
    Rgb fg = p->fg_set ? p->fg : INK;
    Rgb bg = p->bg_set ? p->bg : GROUND;
    if (p->reverse) {
        p->ink = bg;
        p->ground = fg;
    } else {
        p->ink = fg;
        p->ground = bg;
    }
}

static void pen_reset(Pen *p)
{
    p->fg_set = p->bg_set = 0;
    p->bold = p->reverse = p->faint = 0;
    pen_settle(p);
}

/* Apply one SGR's parameters. Only what a wall sends: reset, bold,
 * faint, reverse, the 16 named colours, and the 256 and truecolour forms. A
 * parameter that is not one of those leaves the pen alone, so an unknown
 * attribute costs a colour rather than a frame. */
static void pen_take(Pen *pen, const int *v, int n)
{
    int i = 0;
    while (i < n) {
        int p = v[i];
        if (p == 0) {
            pen_reset(pen);
        } else if (p == 1) {
            pen->bold = 1;
        } else if (p == 2) {
            pen->faint = 1;
        } else if (p == 7) {
            pen->reverse = 1;
        } else if (p == 22) {
            pen->bold = pen->faint = 0;
        } else if (p == 27) {
            pen->reverse = 0;
        } else if (p == 39) {
            pen->fg_set = 0;
        } else if (p == 49) {
            pen->bg_set = 0;
        } else if (p >= 30 && p <= 37) {
            pen->fg = PALETTE[p - 30 + (pen->bold ? 8 : 0)];
            pen->fg_set = 1;
        } else if (p >= 40 && p <= 47) {
            pen->bg = PALETTE[p - 40];
            pen->bg_set = 1;
        } else if (p >= 90 && p <= 97) {
            pen->fg = PALETTE[p - 90 + 8];
            pen->fg_set = 1;
        } else if (p >= 100 && p <= 107) {
            pen->bg = PALETTE[p - 100 + 8];
            pen->bg_set = 1;
        } else if ((p == 38 || p == 48) && i + 1 < n) {
            int kind = v[i + 1];
            Rgb c;
            if (kind == 5 && i + 2 < n) {
                c = PALETTE[v[i + 2] & 255];
                i += 2;
            } else if (kind == 2 && i + 4 < n) {
                c.r = (uint8_t)(v[i + 2] & 255);
                c.g = (uint8_t)(v[i + 3] & 255);
                c.b = (uint8_t)(v[i + 4] & 255);
                i += 4;
            } else {
                i += 1;
                continue;
            }
            if (p == 38) {
                pen->fg = c;
                pen->fg_set = 1;
            } else {
                pen->bg = c;
                pen->bg_set = 1;
            }
        }
        i++;
    }
    pen_settle(pen);
}

/* -------------------------------------------------------------- screen */

/* One character's mask, rendered once and kept: on the GPU its place in
 * the atlas, and in software a surface carrying ink and ground, because
 * a blit is a copy. */
typedef struct {
    int used;
    uint32_t cp;
    uint32_t fg, bg;        /* packed 0xRRGGBB; zero on the GPU path */
    SDL_Surface *surf;      /* software: the cell, in its colours */
    int w, h;
    int slot;               /* GPU: where in the atlas the mask begins */
    uint16_t u, v;          /* GPU: that place, as the shader wants it */
    int cells;              /* a wide glyph takes two */
} Glyph;

/* One corner of one cell, as the GPU sees it: where it is, its colours,
 * where its mask begins in the atlas, and which corner this is, which
 * the GPU interpolates into a position within the cell. Twenty bytes:
 * a grid of sixty-five thousand cells is a megabyte and a bit a frame. */
typedef struct {
    uint16_t x, y;
    uint8_t ink[4], ground[4];
    uint16_t u, v;
    uint8_t fx, fy, pad[2];
} Vert;

/* What a grid's vertices depend on besides the cells: see gl_key. */
#define GL_KEY 9

/* The GPU path's state. */
typedef struct {
    SDL_GLContext ctx;
    GLuint glass_fb, glass_tex; /* the glass where there is no window to
                                 * put it up in (gpu_glass_fb) */
    GLuint prog_quad, prog_point, vbo[3], ibo, atlas;
    int which;              /* the vertex buffer this frame goes in */
    int whole;              /* whether that buffer holds a whole frame */
    long whole_key[GL_KEY]; /* and what its vertices were made against */
    uint16_t slot_u;        /* one slot's width, as the shader wants it */
    int points;             /* one vertex per cell rather than four */
    int max_point;          /* the largest point this GPU draws */
    int max_texture;        /* the largest texture side it takes */
    int atlas_w, atlas_h;
    int slot_cols, slot_rows, next_slot;
    Vert *verts;            /* the grid, cols * rows * 4 corners */
    uint32_t *shows;        /* the code point each cell shows, TAIL for
                             * the second half of a wide one, so that an
                             * atlas started again can put them back */
    int rebuilding;         /* putting them back, just now */
    int made;               /* masks made since the atlas last started */
    Glyph *last;            /* the mask found last, asked for again next */
    uint8_t *mask;          /* scratch: one mask, two cells wide at most */
} Gl;

struct Wall;

/* One look at the counters, taken each time the overlay is refreshed.
 * Two looks a second apart are what its rates are worked out from. */
typedef struct {
    double t, bytes, out, parse, flush, swap, cpu, gpu;
    long frames, refreshes, painted;
} Sample;

/* The overlay: what is asked of it, and what it was last drawn as. It
 * is the receiver's own and sits on top of the grid, so none of this is
 * the picture. */
typedef struct {
    int on, at, size;       /* where: an index into POINTS */
    unsigned show;          /* a bit for each of MEASURES */
    TTF_Font *font;         /* at `size`, whatever the wall's font is */
    int font_size;
    SDL_Rect box;           /* where it goes on the glass; empty until drawn */
    SDL_Rect was;           /* software: where it was last pushed out */
    GLuint tex, prog, vbo;  /* GPU: the text as a mask, and how to draw it */
    SDL_Surface *soft;      /* software: the box and its text */
    SDL_Surface *under;     /* software: the picture beneath, while it is up */
    double last;
    Sample seen[3];         /* the latest looks, newest last */
    int nseen;
} Stats;

/* What a video layer shows: its latest frame, where on the glass it goes,
 * and until when. Who draws it is the wall's business; this is only what
 * the glass shows. A frame is RGBA, or on the GPU three planes of YUV
 * that the shader turns to colour as it draws, so a film never goes
 * through RGB in memory. */
typedef struct {
    int on;                 /* a frame is up */
    int x, y, w, h;         /* where on the glass */
    double until;           /* when it comes down, by now_ms() */
    int yuv;                /* GPU: the frame is `plane`, not `tex` */
    GLuint tex;             /* GPU: the frame, RGBA */
    int tw, th;             /* GPU: the size `tex` was made at */
    GLuint plane[3];        /* GPU: Y, U and V, one byte a sample */
    int pw, ph;             /* GPU: the size the planes were made at, Y's */
    SDL_Surface *soft;      /* software: the frame, ARGB, blended as it is blitted;
                             * only what lands on the glass, from sx, sy within it */
    int sx, sy;
    SDL_Surface *under;     /* software: the picture beneath it, while it is up */
    SDL_Rect box, was;      /* software: where it is on the glass, and was last pushed out */
} Pic;

/* Kitty graphics' limits: an image received and its sides, as a video
 * frame's; images and placements a session holds; decoded pixels a wall
 * holds; and the longest single string, past which one is refused whole. */
#define KITTY_RECEIVED FRAME_MAX
#define KITTY_SIDE FRAME_SIDE
#define KITTY_IMAGES 32
#define KITTY_PLACES 64
#define KITTY_WALL (64u << 20)
#define KITTY_STRING (512u << 10)

/* The keys of one kitty string, as far as they are drawn here. `other` is
 * a key besides `m` and `q`, which a chunk after the first never has. */
typedef struct {
    char a, t, d, o;
    int f, s, v, m, U, C, z, c, r;
    uint32_t i, p;
    int other;
} KKeys;

/* One image a session defined: its id, its size, and its pixels, on the
 * GPU as a texture and in software as RGBA kept for scaling. */
typedef struct {
    int used;
    uint32_t id;
    int w, h;
    size_t bytes;           /* decoded, counted against the wall */
    GLuint tex;             /* GPU */
    uint8_t *rgba;          /* software */
} KImage;

/* One placement: which image, at which cell, and the cells asked for; zero
 * where kitty works them out. `seq` orders placements at one z. In software
 * the picture at its size on the glass, and what it covers while it is up. */
typedef struct {
    int used;
    int image;              /* its index in the images */
    uint32_t pid;
    int col, row, c, r, z;
    long seq;
    SDL_Surface *soft, *under;
    SDL_Rect box, was;
} KPlace;

/* The kitty graphics of the stream the grid is painted from, which is the
 * base session's: its images and placements, and an image still arriving
 * in chunks. They go when the session does. */
typedef struct {
    KImage img[KITTY_IMAGES];
    KPlace place[KITTY_PLACES];
    size_t decoded;
    long seq;
    uint32_t auto_id;
    int pending;            /* an image is arriving in chunks */
    int refused;            /* and was refused: its chunks are let go */
    KKeys pk;               /* its first chunk's keys */
    uint8_t *data;          /* what has been decoded of its base64 so far */
    size_t len, cap;
    uint32_t quad;          /* base64 carried across chunks: bits, and */
    int nquad, bad;         /* how many sextets; a character not base64 */
    int skip;               /* a string too long to hold is being let go */
    GLuint prog_ink[2];     /* GPU: the grid's text alone, quads and points */
} Kitty;

/* A cells layer above the base: the wall's grid again, the same size and
 * place, with every cell it has not drawn transparent. What each cell
 * shows is kept as the character and two colours that carry an alpha, so
 * its vertices can be made again whenever the font, the glass or the
 * atlas changes under it; and the cursor and pen its stream is read with,
 * since a layer's stream is a sender's and goes on from where it was. */
typedef struct LGrid {
    int cols, rows;
    uint32_t *cp;           /* each cell's character, TAIL for the second
                             * half of a wide one, 0 for nothing drawn */
    uint8_t *rgba;          /* each cell's ink and ground, RGBA, 8 bytes */
    Vert *verts;            /* what it is drawn from, made by lgrid_build */
    long key[GL_KEY];       /* what they were made against */
    int stale;              /* the cells have changed since they were made */
    int uploaded;           /* the buffer holds the vertices as made */
    GLuint vbo;
    int x, y;               /* the cursor */
    Pen pen;
    uint8_t ink_a, ground_a;    /* the pen's alphas: a ground left unset is none */
    int skip;               /* a command string too long to hold is let go */
    struct Face *face;      /* a face of its own, or none: the base's grid */
    int own;                /* what it asked: 0 none, 1 a font, 2 a grid */
    int want_cols, want_rows;   /* the grid it asked, kept through the glass */
    int fw, fh;             /* the glass its face was made for */
} LGrid;

/* A font at one size and everything made from it: the cell, the grid it
 * makes on the glass and where that sits, the shapes drawn in the cell,
 * the glyph cache, and on the GPU the atlas the masks are in. The screen
 * holds the base's in its own fields; a cells layer with a size of its own
 * holds one here, and face_swap trades it in while the layer is made and
 * drawn, so every function that paints a cell paints in that face. */
typedef struct Face {
    TTF_Font *font;
    int size, cw, ch, tx, ty, cols, rows, ox, oy;
    SDL_Rect (*px)[MAXRECTS];
    uint8_t *pn, *ptile;
    Glyph *glyphs;
    int nglyphs;
    long restarts;
    GLuint atlas;
    int atlas_w, atlas_h, slot_cols, slot_rows, next_slot, points, made, rebuilding;
    uint16_t slot_u;
    uint8_t *mask;
    Glyph *last;
} Face;

/* The display, and what is currently on it. */
typedef struct Screen {
    struct Wall *wall;      /* who to tell about requests in the stream */

    /* which wall this is, of how many, and the output it is on */
    int index, count;
    int display;            /* SDL's number for the display it is on */
    char output[32];        /* the connector's name, as the kernel gives it */
    uint32_t pin;           /* the connector DPMS goes to, or zero to
                             * find one; pinned whenever there are two
                             * walls, so that one never sleeps the other */

    /* the glass */
    SDL_Window *win;
    int gpu;                /* the GL path; otherwise software */
    Gl gl;
    SDL_Surface *glass;     /* the window surface: the picture, in software */
    SDL_Surface *paper;     /* where the picture waits while the glass is blank */
    SDL_Surface *target;    /* glass or paper, whichever is being painted */
    const char *backend;
    int width, height;      /* the glass as the wall is drawn on it: turned
                             * a quarter, the window's sides swapped */
    int phys_w, phys_h;     /* the window, as the output shows it */
    int ask_w, ask_h;       /* with no screen, the glass as last asked for */
    int settle_w, settle_h; /* a new size the display offers, and since */
    double settle_at;       /* when, until it has held for SETTLE */
    int turn;               /* degrees clockwise the picture is turned on
                             * the output, for a screen hung that way:
                             * 0, 90, 180 or 270; on the GPU path only */
    int capturing;          /* drawing the glass for a picture, upright */

    /* the grid */
    const char *font_path;
    TTF_Font *font;
    int size;
    int cw, ch, cols, rows;
    int tx, ty;                 /* where a typed glyph sits in its cell, when the cell is bigger than the font's */
    int ox, oy;             /* where the grid starts: the pixels a whole
                             * cell cannot use, split between both sides */
    SDL_Rect (*px)[MAXRECTS];   /* each shape in this cell, once per font:
                                 * NSHAPES of them */
    uint8_t *pn;                /* how many rectangles each one is */
    uint8_t *ptile;             /* whether it may be one fill across a run */

    /* the cursor and the pen */
    int x, y;
    Pen pen;
    int cut;                /* the stream being parsed has ended under feed():
                             * what is left of it is the last sender's */

    /* what has changed since the last present */
    int drew, blanked;
    int owed;               /* a layer above changed and the glass has not */
    double blanked_at;      /* when the glass went black, for standby */
    int lit;                /* whether the output is awake */
    int drm;                /* the card, borrowed from SDL, or -1 */
    uint32_t connector, dpms;   /* what to ask, and which property; zero
                                 * until an output has been found */
    SDL_Rect dirty[DIRTY_MAX];
    int ndirty, dirty_all;

    /* glyphs */
    Glyph *glyphs;          /* CACHE_SLOTS of them */
    int nglyphs;

    /* the parser's buffer */
    uint8_t buf[BUF_SIZE];
    size_t len;

    /* one read taken from the base's inbox, on its way into the buffer */
    uint8_t raw[READ_SIZE];

    /* accounting */
    int nodraw;             /* parse and skip the painting, to time the parse */
    long frames;            /* times the glass was put up */
    long refreshes;         /* of those, the overlay's own, with no frame in them */
    long painted;           /* of those, the base's frames */
    long restarts;          /* times the atlas or the glyph table filled */
    double t_prev, t_active, t_flush, t_swap;   /* time spent between presents that followed each other */
    double bytes_in, t_parse;   /* read from every session, and time spent
                                 * parsing and painting the base's */
    double bytes_out;       /* written to every session's `out` */

    Stats st;

    /* the video layers, by z: a video base at 0 and the layers above it,
     * and the glass they were put up on */
    Pic pics[LAYERS];
    LGrid *cells[LAYERS];   /* a cells layer's grid at that z, or none */
    GLuint prog_pic, prog_yuv, vbo_pic;
    GLuint prog_lgrid[2];   /* a cells layer, as quads and as points */
    int pic_seen[2];

    /* the glass drawn for a picture: whole, and then at the size asked */
    GLuint cap_fb[2], cap_tex[2], cap_prog, cap_vbo;
    int cap_w[2], cap_h[2];
    long pic_frames;        /* frames put up on a layer above */

    struct Outputs *outs;   /* the glass's streams, while any runs (outputs.c) */

    Kitty kitty;
    int in_face;            /* a layer's face is swapped in (face_swap) */
} Screen;

/* What the outputs (outputs.c) do for the code before them. */
struct Session;
static void gl_glass(Screen *s);
static void outputs_stale(Screen *s);
static void outputs_measure(Screen *s, struct Session *x, char *out, size_t max);
static void wall_area(struct Wall *w);
static void wall_repaint(struct Wall *w);
static void wall_above_repaint(struct Wall *w);
static int wall_holds_picture(struct Wall *w);
static void base_command(struct Wall *w, char **words, int n, const char *line);
static const char *rest_after(const char *line, int skip);

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

/* Report the grid and the font size that produced it. The size is
 * stamped so a sender can tell a current report from its own request
 * echoed back beside a stale grid. */
static void announce(Screen *s)
{
    if (s->wall) {
        /* A wall says its area on every session, not on stdout. */
        wall_area(s->wall);
        return;
    }
    /* A painter with no conversation, reading stdin: its area on stdout is
     * all a benchmark or a test has to go on. */
    if (s->index == 0) {
        printf("area %dx%d %d\n", s->cols, s->rows, s->size);
        fflush(stdout);
    }
}

/* What the painter asks of the machine it runs on. */
#include "platform.h"

/* The window's size, and the glass's: the same, or with its sides swapped
 * where the picture is turned a quarter, so the wall is drawn, measured
 * and said to be the shape it hangs, taller than wide on its side. */
static void take_size(Screen *s)
{
    display_size(s, &s->phys_w, &s->phys_h);
    if (s->gpu && (s->turn == 90 || s->turn == 270)) {
        s->width = s->phys_h;
        s->height = s->phys_w;
    } else {
        s->width = s->phys_w;
        s->height = s->phys_h;
    }
}

/* The turn, as every shader that draws on the glass takes it: the cosine
 * and sine of the angle the picture turns in clip space, anticlockwise
 * there, so a clockwise turn on the glass is its sine negated. None while
 * the glass is drawn for a picture, which is taken upright. */
static void gl_turn(Screen *s, GLuint prog)
{
    float c = 1, n = 0;
    if (!s->capturing) {
        if (s->turn == 90)
            c = 0, n = -1;
        else if (s->turn == 180)
            c = -1, n = 0;
        else if (s->turn == 270)
            c = 0, n = 1;
    }
    glUniform2f(glGetUniformLocation(prog, "u_turn"), c, n);
}

/* ---- the GPU path: shaders */

/* A cell as four corners. The corner's fraction of the cell rides along
 * as an attribute and comes out interpolated in the fragment shader as
 * the fragment's own position within the cell. */
static const char *VS_QUAD =
    "precision mediump float;\n"
    "attribute vec2 a_pos; attribute vec4 a_ink; attribute vec4 a_ground;\n"
    "attribute vec2 a_uv; attribute vec2 a_f;\n"
    "uniform vec2 u_screen; uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying vec2 v_f;\n"
    "void main() {\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x)\n"
    "                     * vec2(a_pos.x / u_screen.x * 2.0 - 1.0,\n"
    "                            1.0 - a_pos.y / u_screen.y * 2.0), 0.0, 1.0);\n"
    "  v_ink = a_ink; v_ground = a_ground; v_uv = a_uv; v_f = a_f;\n"
    "}\n";

/* Ground mixed into ink through the mask, which is the whole of painting
 * a cell: a block is a mask of ones and zeros, a shade a flat fraction,
 * a glyph whatever the font drew. A faint braille cell comes with its
 * ground's alpha at zero and a mask of three levels instead: ink for a
 * lit dot, ground for an unlit one, and the wall's black, GROUND, for
 * what lies between them. */
#define GHOSTED \
    "  if (v_ground.a < 0.5)\n" \
    "    gl_FragColor = a > 0.75 ? vec4(v_ink.rgb, 1.0)\n" \
    "                 : a > 0.25 ? vec4(v_ground.rgb, 1.0) : vec4(0.0, 0.0, 0.0, 1.0);\n" \
    "  else\n" \
    "    gl_FragColor = mix(v_ground, v_ink, a);\n"

static const char *FS_QUAD =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying vec2 v_f;\n"
    "void main() {\n"
    "  float a = texture2D(u_atlas, v_uv + v_f * u_slot).a;\n"
    GHOSTED
    "}\n";

/* A cell as one point. A point is square and a cell is not, so the
 * point is the cell's longer side and the fragment shader throws away
 * what falls outside the cell. A point is square on the output whichever
 * way the picture is turned, so the fragment shader turns its place in
 * the point back into the cell's before it looks. Cheaper than four corners wherever the
 * grid is dense, because it is the vertices this GPU runs out of. */
static const char *VS_POINT =
    "precision mediump float;\n"
    "attribute vec2 a_pos; attribute vec4 a_ink; attribute vec4 a_ground;\n"
    "attribute vec2 a_uv;\n"
    "uniform vec2 u_screen; uniform vec2 u_cell; uniform float u_size; uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv;\n"
    "void main() {\n"
    "  vec2 c = a_pos + u_cell * 0.5;\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x)\n"
    "                     * vec2(c.x / u_screen.x * 2.0 - 1.0, 1.0 - c.y / u_screen.y * 2.0),\n"
    "                     0.0, 1.0);\n"
    "  gl_PointSize = u_size;\n"
    "  v_ink = a_ink; v_ground = a_ground; v_uv = a_uv;\n"
    "}\n";

static const char *FS_POINT =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot; uniform vec2 u_cell; uniform float u_size;\n"
    "uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv;\n"
    "void main() {\n"
    "  vec2 q = gl_PointCoord - 0.5;\n"
    "  q = vec2(u_turn.x * q.x - u_turn.y * q.y, u_turn.y * q.x + u_turn.x * q.y);\n"
    "  vec2 f = (q * u_size + u_cell * 0.5) / u_cell;\n"
    "  if (f.x < 0.0 || f.y < 0.0 || f.x >= 1.0 || f.y >= 1.0) discard;\n"
    "  float a = texture2D(u_atlas, v_uv + f * u_slot).a;\n"
    GHOSTED
    "}\n";

/* A cells layer above the base: the same cell, its ink and its ground
 * each with an alpha of its own -- the ink's in its fourth byte, the
 * ground's in a_gal, since the ground's fourth byte is the sign of faint
 * braille -- and the result premultiplied, so that a cell with no ground
 * puts down its ink alone and one with neither puts down nothing. A faint
 * cell's unlit dots are its ground and what lies between them nothing. */
#define LAYERED \
    "  vec4 i = vec4(v_ink.rgb * v_ink.a, v_ink.a);\n" \
    "  vec4 g = vec4(v_ground.rgb * v_gal, v_gal);\n" \
    "  vec4 c;\n" \
    "  if (v_ground.a < 0.5)\n" \
    "    c = a > 0.75 ? i : a > 0.25 ? g : vec4(0.0);\n" \
    "  else\n" \
    "    c = mix(g, i, a);\n" \
    "  if (c.a < 0.004) discard;\n" \
    "  gl_FragColor = c;\n"

static const char *VS_LQUAD =
    "precision mediump float;\n"
    "attribute vec2 a_pos; attribute vec4 a_ink; attribute vec4 a_ground;\n"
    "attribute vec2 a_uv; attribute vec2 a_f; attribute float a_gal;\n"
    "uniform vec2 u_screen; uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying vec2 v_f;\n"
    "varying float v_gal;\n"
    "void main() {\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x)\n"
    "                     * vec2(a_pos.x / u_screen.x * 2.0 - 1.0,\n"
    "                            1.0 - a_pos.y / u_screen.y * 2.0), 0.0, 1.0);\n"
    "  v_ink = a_ink; v_ground = a_ground; v_uv = a_uv; v_f = a_f; v_gal = a_gal;\n"
    "}\n";

static const char *FS_LQUAD =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying vec2 v_f;\n"
    "varying float v_gal;\n"
    "void main() {\n"
    "  float a = texture2D(u_atlas, v_uv + v_f * u_slot).a;\n"
    LAYERED
    "}\n";

static const char *VS_LPOINT =
    "precision mediump float;\n"
    "attribute vec2 a_pos; attribute vec4 a_ink; attribute vec4 a_ground;\n"
    "attribute vec2 a_uv; attribute float a_gal;\n"
    "uniform vec2 u_screen; uniform vec2 u_cell; uniform float u_size; uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying float v_gal;\n"
    "void main() {\n"
    "  vec2 c = a_pos + u_cell * 0.5;\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x)\n"
    "                     * vec2(c.x / u_screen.x * 2.0 - 1.0, 1.0 - c.y / u_screen.y * 2.0),\n"
    "                     0.0, 1.0);\n"
    "  gl_PointSize = u_size;\n"
    "  v_ink = a_ink; v_ground = a_ground; v_uv = a_uv; v_gal = a_gal;\n"
    "}\n";

static const char *FS_LPOINT =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot; uniform vec2 u_cell; uniform float u_size;\n"
    "uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying float v_gal;\n"
    "void main() {\n"
    "  vec2 q = gl_PointCoord - 0.5;\n"
    "  q = vec2(u_turn.x * q.x - u_turn.y * q.y, u_turn.y * q.x + u_turn.x * q.y);\n"
    "  vec2 f = (q * u_size + u_cell * 0.5) / u_cell;\n"
    "  if (f.x < 0.0 || f.y < 0.0 || f.x >= 1.0 || f.y >= 1.0) discard;\n"
    "  float a = texture2D(u_atlas, v_uv + f * u_slot).a;\n"
    LAYERED
    "}\n";

static GLuint gl_shader(GLenum kind, const char *text)
{
    GLuint sh = glCreateShader(kind);
    GLint ok = 0;
    const char *parts[2];
    parts[0] = gpu_preamble();
    parts[1] = text;
    glShaderSource(sh, 2, parts, NULL);
    glCompileShader(sh);
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(sh, sizeof log, NULL, log);
        fprintf(stderr, "bitwall-paint: shader: %s\n", log);
        glDeleteShader(sh);
        return 0;
    }
    return sh;
}

static GLuint gl_program(const char *vs, const char *fs)
{
    GLuint v = gl_shader(GL_VERTEX_SHADER, vs), f = gl_shader(GL_FRAGMENT_SHADER, fs);
    GLuint p;
    GLint ok = 0;
    if (!v || !f)
        return 0;
    p = glCreateProgram();
    glAttachShader(p, v);
    glAttachShader(p, f);
    glLinkProgram(p);
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    glDeleteShader(v);
    glDeleteShader(f);
    if (!ok) {
        char log[512];
        glGetProgramInfoLog(p, sizeof log, NULL, log);
        fprintf(stderr, "bitwall-paint: link: %s\n", log);
        glDeleteProgram(p);
        return 0;
    }
    return p;
}

/* Bring up the GL path on a window made for it. Anything missing means
 * the software path instead, which is a slower wall rather than none. */
static int gl_setup(Screen *s)
{
    Gl *g = &s->gl;
    GLint maxtex = 0;
    GLfloat range[2] = {1, 1};
    GLushort *index;
    int i;

    if (gpu_context(s) < 0)
        return -1;
    g->prog_quad = gl_program(VS_QUAD, FS_QUAD);
    g->prog_point = gl_program(VS_POINT, FS_POINT);
    if (!g->prog_quad || !g->prog_point)
        return -1;

    glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxtex);
    if (maxtex < ATLAS_MIN)
        return -1;          /* no atlas worth the name; paint in software */
    g->max_texture = maxtex;
    glGetFloatv(GL_ALIASED_POINT_SIZE_RANGE, range);
    g->max_point = (int)range[1];

    /* The texture's storage is made per font, by atlas_fit, because how
     * big it needs to be depends on the cell. */
    glGenTextures(1, &g->atlas);
    glBindTexture(GL_TEXTURE_2D, g->atlas);
    /* Nearest, because a mask is sampled once per pixel of the cell it
     * was made for: the pixels are the same pixels. */
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    glGenBuffers(3, g->vbo);
    glGenBuffers(1, &g->ibo);
    index = malloc(sizeof *index * 6 * BATCH);
    if (!index)
        return -1;
    for (i = 0; i < BATCH; i++) {
        index[i * 6 + 0] = (GLushort)(i * 4);
        index[i * 6 + 1] = (GLushort)(i * 4 + 1);
        index[i * 6 + 2] = (GLushort)(i * 4 + 2);
        index[i * 6 + 3] = (GLushort)(i * 4);
        index[i * 6 + 4] = (GLushort)(i * 4 + 2);
        index[i * 6 + 5] = (GLushort)(i * 4 + 3);
    }
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g->ibo);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, sizeof *index * 6 * BATCH, index, GL_STATIC_DRAW);
    free(index);

    glDisable(GL_BLEND);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    if (glGetError() != GL_NO_ERROR)
        return -1;
    return 0;
}

/* Draw the whole grid into the back buffer. */
/* What the grid's vertices were made against: were any of these to
 * change, a buffer filled before would draw the wrong cells, pixels or
 * masks. */
static void gl_key(Screen *s, long *k)
{
    k[0] = s->cols;
    k[1] = s->rows;
    k[2] = s->cw;
    k[3] = s->ch;
    k[4] = s->width;
    k[5] = s->height;
    k[6] = s->gl.atlas_w;
    k[7] = (long)s->gl.atlas_h << 1 | s->gl.points;
    k[8] = s->restarts;
}

/* The grid's text alone, for going over a kitty picture put beneath it:
 * the ground let through and the ink blended over by the mask. */
#define INKED \
    "  if (v_ground.a < 0.5) {\n" \
    "    if (a <= 0.75) discard;\n" \
    "    gl_FragColor = vec4(v_ink.rgb, 1.0);\n" \
    "  } else {\n" \
    "    if (a < 0.004) discard;\n" \
    "    gl_FragColor = vec4(v_ink.rgb, a);\n" \
    "  }\n"

static const char *FS_QUAD_INK =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv; varying vec2 v_f;\n"
    "void main() {\n"
    "  float a = texture2D(u_atlas, v_uv + v_f * u_slot).a;\n"
    INKED
    "}\n";

static const char *FS_POINT_INK =
    "precision mediump float;\n"
    "uniform sampler2D u_atlas; uniform vec2 u_slot; uniform vec2 u_cell; uniform float u_size;\n"
    "uniform vec2 u_turn;\n"
    "varying vec4 v_ink; varying vec4 v_ground; varying vec2 v_uv;\n"
    "void main() {\n"
    "  vec2 q = gl_PointCoord - 0.5;\n"
    "  q = vec2(u_turn.x * q.x - u_turn.y * q.y, u_turn.y * q.x + u_turn.x * q.y);\n"
    "  vec2 f = (q * u_size + u_cell * 0.5) / u_cell;\n"
    "  if (f.x < 0.0 || f.y < 0.0 || f.x >= 1.0 || f.y >= 1.0) discard;\n"
    "  float a = texture2D(u_atlas, v_uv + f * u_slot).a;\n"
    INKED
    "}\n";

static void gl_grid(Screen *s, int ink);
static void kitty_gl(Screen *s);

/* The grid and the kitty pictures placed in it. */
static void gl_draw(Screen *s)
{
    gl_grid(s, 0);
    kitty_gl(s);
}

/* The grid, whole into the back buffer; or with `ink` its text alone, over
 * what is there, from the vertices the grid was drawn from just before. */
static void gl_grid(Screen *s, int ink)
{
    Gl *g = &s->gl;
    GLuint p = g->points ? g->prog_point : g->prog_quad;
    GLint a_pos, a_ink, a_ground, a_uv, a_f;
    int cells = s->cols * s->rows, per = g->points ? 1 : 4, done = 0;
    long key[GL_KEY];

    gl_use(s);
    if (ink) {
        GLuint *k = &s->kitty.prog_ink[g->points];
        if (!*k)
            *k = g->points ? gl_program(VS_POINT, FS_POINT_INK)
                           : gl_program(VS_QUAD, FS_QUAD_INK);
        if (!*k)
            return;
        p = *k;
    }
    a_pos = glGetAttribLocation(p, "a_pos");
    a_ink = glGetAttribLocation(p, "a_ink");
    a_ground = glGetAttribLocation(p, "a_ground");
    a_uv = glGetAttribLocation(p, "a_uv");
    a_f = g->points ? -1 : glGetAttribLocation(p, "a_f");
    if (s->capturing)
        glViewport(0, 0, s->width, s->height);
    else
        glViewport(0, 0, s->phys_w, s->phys_h);
    /* The grid is drawn whole every frame but does not cover the glass:
     * the pixels round it are put back to black, or they keep whatever
     * picture last reached them. */
    if (!ink) {
        glClearColor(GROUND.r / 255.0f, GROUND.g / 255.0f, GROUND.b / 255.0f, 1);
        glClear(GL_COLOR_BUFFER_BIT);
    }
    glUseProgram(p);
    glUniform2f(glGetUniformLocation(p, "u_screen"), (float)s->width, (float)s->height);
    gl_turn(s, p);
    glUniform2f(glGetUniformLocation(p, "u_slot"), (float)s->cw / g->atlas_w, (float)s->ch / g->atlas_h);
    glUniform1i(glGetUniformLocation(p, "u_atlas"), 0);
    if (g->points) {
        glUniform2f(glGetUniformLocation(p, "u_cell"), (float)s->cw, (float)s->ch);
        glUniform1f(glGetUniformLocation(p, "u_size"), (float)(s->cw > s->ch ? s->cw : s->ch));
    }
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, g->atlas);
    /* Three buffers in turn, so that filling this frame's never waits
     * for the GPU to finish reading the last one. Part way through a
     * frame of the base's, the grid is its last whole frame, still in the
     * buffer it went up in, so that a layer above can be put up without
     * half a frame beneath it; unless the font, the atlas or the glass
     * has changed since, when the grid goes up as it is. */
    gl_key(s, key);
    if (ink) {
        glBindBuffer(GL_ARRAY_BUFFER, g->vbo[g->which]);
        glEnable(GL_BLEND);
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    } else if (s->drew && g->whole && !memcmp(key, g->whole_key, sizeof key)) {
        glBindBuffer(GL_ARRAY_BUFFER, g->vbo[g->which]);
    } else {
        g->which = (g->which + 1) % 3;
        glBindBuffer(GL_ARRAY_BUFFER, g->vbo[g->which]);
        gpu_vertices(g->verts, sizeof(Vert) * cells * per);
        g->whole = !s->drew;
        memcpy(g->whole_key, key, sizeof key);
    }
    glEnableVertexAttribArray(a_pos);
    glEnableVertexAttribArray(a_ink);
    glEnableVertexAttribArray(a_ground);
    glEnableVertexAttribArray(a_uv);
    if (a_f >= 0)
        glEnableVertexAttribArray(a_f);
    while (done < cells) {
        int batch = cells - done;
        /* With a buffer bound, GL takes the "pointer" as an offset into
         * it. The offset is made a pointer only once, from an integer,
         * because arithmetic on a null pointer is undefined in C. */
        uintptr_t base = sizeof(Vert) * (size_t)done * per;
#define AT(off) ((const void *)(base + (off)))
        if (!g->points && batch > BATCH)
            batch = BATCH;
        glVertexAttribPointer(a_pos, 2, GL_UNSIGNED_SHORT, GL_FALSE, sizeof(Vert), AT(0));
        glVertexAttribPointer(a_ink, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(4));
        glVertexAttribPointer(a_ground, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(8));
        glVertexAttribPointer(a_uv, 2, GL_UNSIGNED_SHORT, GL_TRUE, sizeof(Vert), AT(12));
        if (g->points) {
            glDrawArrays(GL_POINTS, 0, batch);
        } else {
            glVertexAttribPointer(a_f, 2, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(16));
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g->ibo);
            glDrawElements(GL_TRIANGLES, batch * 6, GL_UNSIGNED_SHORT, 0);
        }
#undef AT
        done += batch;
    }
    if (ink)
        glDisable(GL_BLEND);
}

/* Point cell `i` at a mask, or at the second half of a wide one, and
 * note which character that is. Its colours are left as they were. */
static void cell_mask(Screen *s, int i, const Glyph *m, int tail)
{
    Gl *g = &s->gl;
    int per = g->points ? 1 : 4, k;
    uint16_t u = tail && m->cells == 2 ? (uint16_t)(m->u + g->slot_u) : m->u;
    for (k = 0; k < per; k++) {
        g->verts[i * per + k].u = u;
        g->verts[i * per + k].v = m->v;
    }
    g->shows[i] = m->cp | (tail ? TAIL : 0);
}

/* Set one cell of the grid: its colours and the mask it shows. */
static void cell_put(Screen *s, int x, int y, Rgb ink, Rgb ground, const Glyph *m, int tail)
{
    Gl *g = &s->gl;
    Vert v;
    int k;
    v.x = (uint16_t)(s->ox + x * s->cw);
    v.y = (uint16_t)(s->oy + y * s->ch);
    v.ink[0] = ink.r; v.ink[1] = ink.g; v.ink[2] = ink.b; v.ink[3] = 255;
    v.ground[0] = ground.r; v.ground[1] = ground.g; v.ground[2] = ground.b;
    v.ground[3] = m->cp & FAINT ? 0 : 255;     /* the shader's sign of three levels */
    v.u = v.v = 0;
    v.fx = v.fy = v.pad[0] = v.pad[1] = 0;
    if (g->points) {
        g->verts[y * s->cols + x] = v;
        cell_mask(s, y * s->cols + x, m, tail);
        return;
    }
    for (k = 0; k < 4; k++) {
        Vert *c = &g->verts[(y * s->cols + x) * 4 + k];
        *c = v;
        if (k == 1 || k == 2) {
            c->x = (uint16_t)(c->x + s->cw);
            c->fx = 255;
        }
        if (k >= 2) {
            c->y = (uint16_t)(c->y + s->ch);
            c->fy = 255;
        }
    }
    cell_mask(s, y * s->cols + x, m, tail);
}

static void fill(Screen *s, const SDL_Rect *r, int n, Rgb c)
{
    if (n <= 0 || s->nodraw)
        return;
    SDL_FillRects(s->target, r, n, SDL_MapRGB(s->target->format, c.r, c.g, c.b));
}

static void dirty_add(Screen *s, const SDL_Rect *r)
{
    if (s->gpu || s->dirty_all)
        return;             /* the GPU path redraws the whole grid */
    if (s->ndirty == DIRTY_MAX) {
        s->dirty_all = 1;
        s->ndirty = 0;
        return;
    }
    s->dirty[s->ndirty++] = *r;
}

static const Glyph *mask_of(Screen *s, uint32_t cp);

/* Paint the whole picture in ground. */
static void clear(Screen *s)
{
    if (s->gpu) {
        const Glyph *m = mask_of(s, ' ');
        int x, y;
        for (y = 0; y < s->rows; y++)
            for (x = 0; x < s->cols; x++)
                cell_put(s, x, y, INK, GROUND, m, 0);
    } else {
        SDL_FillRect(s->target, NULL,
                     SDL_MapRGB(s->target->format, GROUND.r, GROUND.g, GROUND.b));
    }
    s->dirty_all = 1;
    s->ndirty = 0;
    s->drew = 1;
}

/* ---- the overlay */

/* Where the overlay may sit: an edge or a corner by the compass, or the
 * centre. */
static const char *POINTS[] = {"n", "ne", "e", "se", "s", "sw", "w", "nw", "c"};
#define NPOINTS 9
#define POINT_NE 1

/* What the overlay can show, in the order it shows them. PROTOCOL.md
 * says what each one means. */
static const char *MEASURES[] = {"wall", "grid", "fps", "in", "out", "frame", "draw", "swap",
                                 "atlas", "inbox", "sender", "pong", "display", "power", "cpu",
                                 "render",
                                 "temp"};
#define NMEASURES 17
#define MEASURE_FPS (1u << 2)
#define MEASURE_TEMP (1u << 16)
#define THERMAL "/sys/class/thermal/thermal_zone0/temp"
#define MEASURE_ALL ((1u << NMEASURES) - 1)
/* The overlay's lines: the wall's panel, a gap, and a row for each
 * session, as many as fit. */
#define STATS_ROWS 16
#define STATS_LINES (NMEASURES + 1 + STATS_ROWS)

/* The overlay as one quad in pixels, its mask stretched across it. */
static const char *VS_STATS =
    "precision mediump float;\n"
    "attribute vec2 a_pos; attribute vec2 a_uv;\n"
    "uniform vec2 u_screen; uniform vec2 u_turn;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "  gl_Position = vec4(mat2(u_turn.x, u_turn.y, -u_turn.y, u_turn.x)\n"
    "                     * vec2(a_pos.x / u_screen.x * 2.0 - 1.0,\n"
    "                            1.0 - a_pos.y / u_screen.y * 2.0), 0.0, 1.0);\n"
    "  v_uv = a_uv;\n"
    "}\n";

/* White text on black at sixty per cent, blended over the grid. */
static const char *FS_STATS =
    "precision mediump float;\n"
    "uniform sampler2D u_text;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "  float a = texture2D(u_text, v_uv).a;\n"
    "  gl_FragColor = vec4(vec3(a), 0.6 + 0.4 * a);\n"
    "}\n";

/* The text into a texture of the overlay's own, in this wall's context.
 * Its own texture, so the atlas never holds anything but the grid's. */
static int stats_upload(Screen *s, const uint8_t *cov, int w, int h)
{
    Stats *st = &s->st;
    gl_use(s);
    if (!st->prog) {
        st->prog = gl_program(VS_STATS, FS_STATS);
        if (!st->prog)
            return -1;
        glGenTextures(1, &st->tex);
        glGenBuffers(1, &st->vbo);
        glBindTexture(GL_TEXTURE_2D, st->tex);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
    glBindTexture(GL_TEXTURE_2D, st->tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_ALPHA, w, h, 0, GL_ALPHA, GL_UNSIGNED_BYTE, cov);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    return 0;
}

/* The text onto a box of its own, opaque, in the glass's format. The
 * picture is never drawn on: show() puts this up and takes it off again
 * around each present. */
static int stats_soft(Screen *s, const uint8_t *cov, int w, int h)
{
    Stats *st = &s->st;
    SDL_PixelFormat *f = s->glass->format;
    int x, y;
    if (f->BytesPerPixel != 4)
        return -1;
    if (!st->soft || st->soft->w != w || st->soft->h != h) {
        SDL_FreeSurface(st->soft);
        SDL_FreeSurface(st->under);
        st->soft = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, f->format);
        st->under = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, f->format);
        if (!st->soft || !st->under) {
            SDL_FreeSurface(st->soft);
            SDL_FreeSurface(st->under);
            st->soft = st->under = NULL;
            return -1;
        }
        SDL_SetSurfaceBlendMode(st->soft, SDL_BLENDMODE_NONE);
        SDL_SetSurfaceBlendMode(st->under, SDL_BLENDMODE_NONE);
    }
    for (y = 0; y < h; y++) {
        Uint32 *row = (Uint32 *)((uint8_t *)st->soft->pixels + (size_t)y * st->soft->pitch);
        for (x = 0; x < w; x++) {
            Uint8 v = (Uint8)(32 + 223 * cov[(size_t)y * w + x] / 255);
            row[x] = SDL_MapRGB(st->soft->format, v, v, v);
        }
    }
    return 0;
}

/* Make the overlay from its lines: each rendered at the overlay's own
 * size, one under another in a box, and the box placed on the glass. */
static void stats_render(Screen *s, char (*lines)[96], int n)
{
    Stats *st = &s->st;
    SDL_Color white = {255, 255, 255, 255};
    SDL_Surface *text[STATS_LINES];
    const char *at = POINTS[st->at];
    int i, x, y, w = 0, h, skip, pad, ok;
    uint8_t *cov;

    if (!st->font || st->font_size != st->size) {
        TTF_Font *f = TTF_OpenFont(s->font_path, st->size);
        if (!f)
            return;
        if (st->font)
            TTF_CloseFont(st->font);
        st->font = f;
        st->font_size = st->size;
    }
    if (n > STATS_LINES)
        n = STATS_LINES;
    skip = TTF_FontLineSkip(st->font);
    pad = st->size / 2;
    for (i = 0; i < n; i++) {
        text[i] = TTF_RenderUTF8_Blended(st->font, lines[i], white);
        if (text[i] && text[i]->w > w)
            w = text[i]->w;
    }
    w += 2 * pad;
    h = n * skip + 2 * pad;
    if (w > s->width)
        w = s->width;
    if (h > s->height)
        h = s->height;
    cov = calloc((size_t)w * h, 1);
    for (i = 0; i < n; i++) {
        SDL_Surface *t = text[i];
        if (!t)
            continue;
        if (cov && t->format->BytesPerPixel == 4)
            for (y = 0; y < t->h && pad + i * skip + y < h; y++) {
                const Uint32 *row = (const Uint32 *)((const uint8_t *)t->pixels + (size_t)y * t->pitch);
                uint8_t *out = cov + (size_t)(pad + i * skip + y) * w + pad;
                for (x = 0; x < t->w && pad + x < w; x++)
                    out[x] = (uint8_t)((row[x] & t->format->Amask) >> t->format->Ashift);
            }
        SDL_FreeSurface(t);
    }
    if (!cov)
        return;
    ok = s->gpu ? stats_upload(s, cov, w, h) : stats_soft(s, cov, w, h);
    free(cov);
    if (ok < 0) {
        st->box.w = st->box.h = 0;
        return;
    }
    st->box.w = w;
    st->box.h = h;
    st->box.x = strchr(at, 'w') ? STATS_INSET
              : strchr(at, 'e') ? s->width - w - STATS_INSET : (s->width - w) / 2;
    st->box.y = strchr(at, 'n') ? STATS_INSET
              : strchr(at, 's') ? s->height - h - STATS_INSET : (s->height - h) / 2;
    if (st->box.x < 0)
        st->box.x = 0;
    if (st->box.y < 0)
        st->box.y = 0;
}

/* Blend the overlay over the grid just drawn. */
/* Whether the overlay is up. It is kept while nobody is sending, and put
 * back when somebody is, but not drawn over the status page: numbers
 * about a picture are nothing to read when there is no picture. */
static int stats_shown(const Screen *s);

static void stats_gl(Screen *s)
{
    Stats *st = &s->st;
    GLfloat x0 = (GLfloat)st->box.x, y0 = (GLfloat)st->box.y;
    GLfloat x1 = x0 + st->box.w, y1 = y0 + st->box.h;
    GLfloat v[16] = {x0, y0, 0, 0, x1, y0, 1, 0, x1, y1, 1, 1, x0, y1, 0, 1};
    GLint a_pos, a_uv;
    if (!stats_shown(s) || !st->box.w || !st->prog)
        return;
    gl_use(s);
    glUseProgram(st->prog);
    a_pos = glGetAttribLocation(st->prog, "a_pos");
    a_uv = glGetAttribLocation(st->prog, "a_uv");
    glUniform2f(glGetUniformLocation(st->prog, "u_screen"), (float)s->width, (float)s->height);
    gl_turn(s, st->prog);
    glUniform1i(glGetUniformLocation(st->prog, "u_text"), 0);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, st->tex);
    glBindBuffer(GL_ARRAY_BUFFER, st->vbo);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof v, v, GL_STREAM_DRAW);
    glEnableVertexAttribArray(a_pos);
    glEnableVertexAttribArray(a_uv);
    glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat), (const void *)0);
    glVertexAttribPointer(a_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                          (const void *)(uintptr_t)(2 * sizeof(GLfloat)));
    /* The grid's first six indices are one quad's, corner for corner. */
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s->gl.ibo);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, 0);
    glDisable(GL_BLEND);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
}

/* ---- the layers above the grid */

/* A frame as it came: each pixel sampled once, and blended over what is
 * beneath by its own alpha. */
static const char *FS_PIC =
    "precision mediump float;\n"
    "uniform sampler2D u_pic;\n"
    "varying vec2 v_uv;\n"
    "void main() { gl_FragColor = texture2D(u_pic, v_uv); }\n";

/* Colour from BT.709 YUV at studio range, as a film is encoded: the three
 * planes sampled at the same place, the chroma planes being half the
 * size, and turned to colour as the frame is drawn. */
static const char *FS_YUV =
    "precision mediump float;\n"
    "uniform sampler2D u_y; uniform sampler2D u_u; uniform sampler2D u_v;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "  float y = 1.1644 * (texture2D(u_y, v_uv).r - 0.0627);\n"
    "  float u = texture2D(u_u, v_uv).r - 0.502;\n"
    "  float v = texture2D(u_v, v_uv).r - 0.502;\n"
    "  gl_FragColor = vec4(y + 1.7927 * v, y - 0.2132 * u - 0.5329 * v, y + 2.1124 * u, 1.0);\n"
    "}\n";

/* One texture of a layer's, made if it is not yet and bound, filtered
 * smooth when the frame is scaled and sharp when it is not. */
static void pic_texture(GLuint *tex, int smooth)
{
    if (!*tex)
        glGenTextures(1, tex);
    glBindTexture(GL_TEXTURE_2D, *tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, smooth ? GL_LINEAR : GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, smooth ? GL_LINEAR : GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

/* An RGBA frame into a layer's texture: made again only when its size
 * changes, and otherwise written over in place. */
static void pic_upload(Pic *p, const uint8_t *rgba, int w, int h, int smooth)
{
    pic_texture(&p->tex, smooth);
    if (p->tw != w || p->th != h) {
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
        p->tw = w;
        p->th = h;
    } else {
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    }
}

/* In software, the layer's surface at w by h, made again only when the
 * size changes: ARGB, blended as it is blitted, and a surface as the glass
 * is for what it covers. -1 when there is no memory for it. */
static int pic_soft_make(Screen *s, Pic *p, int w, int h)
{
    if (p->soft && p->soft->w == w && p->soft->h == h)
        return 0;
    SDL_FreeSurface(p->soft);
    SDL_FreeSurface(p->under);
    p->soft = SDL_CreateRGBSurfaceWithFormat(0, w, h, 32, SDL_PIXELFORMAT_ARGB8888);
    p->under = SDL_CreateRGBSurfaceWithFormat(0, w, h, s->glass->format->BitsPerPixel,
                                              s->glass->format->format);
    if (!p->soft || !p->under) {
        SDL_FreeSurface(p->soft);
        SDL_FreeSurface(p->under);
        p->soft = p->under = NULL;
        p->on = 0;
        return -1;
    }
    SDL_SetSurfaceBlendMode(p->soft, SDL_BLENDMODE_BLEND);
    SDL_SetSurfaceBlendMode(p->under, SDL_BLENDMODE_NONE);
    return 0;
}

/* In software, the part of a picture w by h at x, y that lands on the
 * glass, into `v`; 0 when none of it does. Only that part is scaled and
 * kept, so a picture far off the glass costs nothing. */
static int pic_seen(const Screen *s, int x, int y, int w, int h, SDL_Rect *v)
{
    int x1 = x + w, y1 = y + h;
    v->x = x < 0 ? 0 : x;
    v->y = y < 0 ? 0 : y;
    if (x1 > s->glass->w) x1 = s->glass->w;
    if (y1 > s->glass->h) y1 = s->glass->h;
    v->w = x1 - v->x;
    v->h = y1 - v->y;
    return v->w > 0 && v->h > 0;
}

static void pic_placed(Screen *s, Pic *p, int x, int y, int w, int h, double until)
{
    p->on = 1;
    p->x = x;
    p->y = y;
    p->w = w;
    p->h = h;
    p->until = until;
    s->pic_frames++;
}

/* Put a frame up on layer `k`: `rgba`, w by h, drawn to the box dw by dh
 * with its top left at x,y on the glass, until `until`. On the GPU it is
 * scaled as it is drawn, smoothly where the box is not its own size; in
 * software it is scaled here, each pixel the nearest sample. -1 when this
 * glass cannot hold it. */
static int pic_set(Screen *s, int k, const uint8_t *rgba, int w, int h, int x, int y,
                   int dw, int dh, double until)
{
    Pic *p = &s->pics[k];
    if (s->gpu) {
        if (w > s->gl.max_texture || h > s->gl.max_texture)
            return -1;
        gl_use(s);
        pic_upload(p, rgba, w, h, dw != w || dh != h);
        p->yuv = 0;
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    } else {
        int r, c;
        SDL_Rect v;
        p->sx = p->sy = 0;
        if (pic_seen(s, x, y, dw, dh, &v)) {
            if (pic_soft_make(s, p, v.w, v.h) < 0)
                return -1;
            p->sx = v.x - x;
            p->sy = v.y - y;
            SDL_LockSurface(p->soft);
            for (r = 0; r < v.h; r++) {
                Uint32 *d = (Uint32 *)((uint8_t *)p->soft->pixels + (size_t)r * p->soft->pitch);
                const uint8_t *q = rgba + (size_t)((long)(p->sy + r) * h / dh) * w * 4;
                for (c = 0; c < v.w; c++) {
                    const uint8_t *o = q + (size_t)((long)(p->sx + c) * w / dw) * 4;
                    d[c] = (Uint32)o[3] << 24 | (Uint32)o[0] << 16 | (Uint32)o[1] << 8 | o[2];
                }
            }
            SDL_UnlockSurface(p->soft);
        }
    }
    pic_placed(s, p, x, y, dw, dh, until);
    return 0;
}

/* Where a frame w by h goes to fill its box, x, y, w, h on the glass, or
 * the glass where `box` is NULL or its w 0: the largest size that fits
 * with its shape kept, centred, or against the box's top (`align` -1) or
 * its bottom (1) where the frame is wider than the box is. What falls off
 * the glass is not drawn. */
static void pic_fit(const Screen *s, int w, int h, int align, const int *box, SDL_Rect *r)
{
    int bx = 0, by = 0, bw = s->width, bh = s->height;
    double k;
    if (box && box[2] > 0) {
        bx = box[0];
        by = box[1];
        bw = box[2];
        bh = box[3];
    }
    k = (double)bw / w < (double)bh / h ? (double)bw / w : (double)bh / h;
    r->w = (int)(w * k + 0.5);
    r->h = (int)(h * k + 0.5);
    if (r->w < 1) r->w = 1;
    if (r->h < 1) r->h = 1;
    r->x = bx + (bw - r->w) / 2;
    r->y = by + (align < 0 ? 0 : align > 0 ? bh - r->h : (bh - r->h) / 2);
}

static uint8_t clamp8(int v)
{
    return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v);
}

/* One sample of a YUV 4:2:0 frame as ARGB, BT.709 at studio range, as the
 * GPU's shader has it. */
static Uint32 yuv_argb(const uint8_t *px, int w, int h, int x, int y)
{
    int cw = (w + 1) / 2, ch = (h + 1) / 2;
    const uint8_t *u = px + (size_t)w * h, *v = u + (size_t)cw * ch;
    int Y = 298 * (px[(size_t)y * w + x] - 16);
    int U = u[(size_t)(y / 2) * cw + x / 2] - 128, V = v[(size_t)(y / 2) * cw + x / 2] - 128;
    return 0xFF000000u | (Uint32)clamp8((Y + 459 * V + 128) >> 8) << 16
           | (Uint32)clamp8((Y - 55 * U - 136 * V + 128) >> 8) << 8
           | clamp8((Y + 541 * U + 128) >> 8);
}

/* Put a frame of a data stream up on layer `k`, filling its box (pic_fit):
 * RGBA, or YUV 4:2:0 as three planes one after another, w by h. It stays
 * up until the next, since a stream's frames have no hold. On the GPU the
 * frame is scaled as it is drawn; in software it is scaled here, each
 * pixel the nearest sample. -1 when this glass cannot hold it. */
static int pic_frame(Screen *s, int k, const uint8_t *px, int w, int h, int yuv, int align,
                     const int *box)
{
    Pic *p = &s->pics[k];
    SDL_Rect at;
    pic_fit(s, w, h, align, box, &at);
    if (s->gpu) {
        if (w > s->gl.max_texture || h > s->gl.max_texture)
            return -1;
        gl_use(s);
        if (yuv) {
            int i, cw = (w + 1) / 2, ch = (h + 1) / 2;
            const uint8_t *plane = px;
            for (i = 0; i < 3; i++) {
                int pw = i ? cw : w, ph = i ? ch : h;
                pic_texture(&p->plane[i], 1);
                if (p->pw != w || p->ph != h)
                    glTexImage2D(GL_TEXTURE_2D, 0, GL_LUMINANCE, pw, ph, 0, GL_LUMINANCE,
                                 GL_UNSIGNED_BYTE, plane);
                else
                    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, pw, ph, GL_LUMINANCE,
                                    GL_UNSIGNED_BYTE, plane);
                plane += (size_t)pw * ph;
            }
            p->pw = w;
            p->ph = h;
        } else {
            pic_upload(p, px, w, h, at.w != w || at.h != h);
        }
        p->yuv = yuv;
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    } else {
        int r, c;
        SDL_Rect v;
        p->sx = p->sy = 0;
        if (pic_seen(s, at.x, at.y, at.w, at.h, &v)) {
            if (pic_soft_make(s, p, v.w, v.h) < 0)
                return -1;
            p->sx = v.x - at.x;
            p->sy = v.y - at.y;
            SDL_LockSurface(p->soft);
            for (r = 0; r < v.h; r++) {
                Uint32 *d = (Uint32 *)((uint8_t *)p->soft->pixels + (size_t)r * p->soft->pitch);
                int sy = (int)((long)(p->sy + r) * h / at.h);
                for (c = 0; c < v.w; c++) {
                    int sx = (int)((long)(p->sx + c) * w / at.w);
                    const uint8_t *q = px + ((size_t)sy * w + sx) * 4;
                    d[c] = yuv ? yuv_argb(px, w, h, sx, sy)
                               : (Uint32)q[3] << 24 | (Uint32)q[0] << 16 | (Uint32)q[1] << 8
                                     | q[2];
                }
            }
            SDL_UnlockSurface(p->soft);
        }
    }
    pic_placed(s, p, at.x, at.y, at.w, at.h, 1e300);
    return 0;
}

/* Whether the glass has changed size since the layers above were put up;
 * when it has they are taken down, since their frames were made for the
 * pixels as they were. A new font is not a change: it moves the cells,
 * not the pixels, and a layer above plays on over it. */
static int pics_moved(Screen *s)
{
    int now[2] = {s->width, s->height}, k;
    if (!memcmp(now, s->pic_seen, sizeof now))
        return 0;
    memcpy(s->pic_seen, now, sizeof now);
    for (k = 0; k < LAYERS; k++)
        s->pics[k].on = 0;
    return 1;
}

/* -- a cells layer above the base ----------------------------------------- */

static void face_swap(Screen *s, Face *f);
static void face_free(Screen *s, Face *f);

/* Every cell of a layer's grid back to nothing drawn: transparent. */
static void lgrid_wipe(LGrid *g)
{
    memset(g->cp, 0, (size_t)g->cols * g->rows * sizeof *g->cp);
    memset(g->rgba, 0, (size_t)g->cols * g->rows * 8);
    g->stale = 1;
}

/* A layer's grid made the wall's size again, kept as it was where the
 * size has not changed, and otherwise wiped for its sender to repaint.
 * Zero, or -1 if memory ran out. */
static int lgrid_fit(Screen *s, LGrid *g)
{
    int cols = g->face ? g->face->cols : s->cols, rows = g->face ? g->face->rows : s->rows;
    size_t n = (size_t)cols * rows;
    if (g->cp && g->cols == cols && g->rows == rows) {
        g->stale = 1;
        return 0;
    }
    free(g->cp);
    free(g->rgba);
    free(g->verts);
    g->cols = cols;
    g->rows = rows;
    g->cp = calloc(n, sizeof *g->cp);
    g->rgba = calloc(n, 8);
    g->verts = calloc(n * 4, sizeof(Vert));
    g->x = g->y = 0;
    g->stale = 1;
    g->uploaded = 0;
    return g->cp && g->rgba && g->verts ? 0 : -1;
}

static LGrid *lgrid_new(Screen *s)
{
    LGrid *g = calloc(1, sizeof *g);
    if (!g)
        return NULL;
    pen_reset(&g->pen);
    g->ink_a = 255;
    if (lgrid_fit(s, g) < 0) {
        free(g->cp);
        free(g->rgba);
        free(g->verts);
        free(g);
        return NULL;
    }
    return g;
}

static void lgrid_free(Screen *s, LGrid *g)
{
    if (!g)
        return;
    if (g->vbo && s->gpu) {
        gl_use(s);
        glDeleteBuffers(1, &g->vbo);
    }
    face_free(s, g->face);
    free(g->cp);
    free(g->rgba);
    free(g->verts);
    free(g);
}

/* A layer's vertices made from its cells, as cell_put makes the base's:
 * the masks from the one atlas, the ink's alpha in its fourth byte and the
 * ground's in the first byte past the corner. A mask made here may start
 * the atlas again, which moves every mask made before it, so the whole is
 * made again until it is made in one atlas. */
static void lgrid_build(Screen *s, LGrid *g)
{
    Gl *gl = &s->gl;
    int per = gl->points ? 1 : 4, tries, i, k;
    int cells = g->cols * g->rows;
    if (g->cols != s->cols || g->rows != s->rows)
        return;
    for (tries = 0; tries < 3; tries++) {
        long restarts = s->restarts;
        for (i = 0; i < cells; i++) {
            uint32_t cp = g->cp[i];
            const uint8_t *c = g->rgba + (size_t)i * 8;
            const Glyph *m = mask_of(s, cp ? cp & ~TAIL : ' ');
            int tail = (cp & TAIL) != 0;
            uint16_t u = tail && m->cells == 2 ? (uint16_t)(m->u + gl->slot_u) : m->u;
            Vert v;
            v.x = (uint16_t)(s->ox + (i % g->cols) * s->cw);
            v.y = (uint16_t)(s->oy + (i / g->cols) * s->ch);
            memcpy(v.ink, c, 4);
            memcpy(v.ground, c + 4, 3);
            v.ground[3] = cp & FAINT ? 0 : 255;
            v.u = u;
            v.v = m->v;
            v.fx = v.fy = 0;
            v.pad[0] = c[7];
            v.pad[1] = 0;
            if (!cp)
                v.ink[3] = v.pad[0] = 0;
            for (k = 0; k < per; k++) {
                Vert *d = &g->verts[(size_t)i * per + k];
                *d = v;
                if (per == 4 && (k == 1 || k == 2)) {
                    d->x = (uint16_t)(d->x + s->cw);
                    d->fx = 255;
                }
                if (per == 4 && k >= 2) {
                    d->y = (uint16_t)(d->y + s->ch);
                    d->fy = 255;
                }
            }
        }
        if (s->restarts == restarts)
            break;
    }
    gl_key(s, g->key);
    g->stale = 0;
    g->uploaded = 0;
}

/* A cells layer, over what is drawn beneath it. */
static void lgrid_gl(Screen *s, LGrid *g)
{
    Gl *gl = &s->gl;
    GLuint *pp = &s->prog_lgrid[gl->points], p;
    GLint a_pos, a_ink, a_ground, a_uv, a_f, a_gal;
    int cells = g->cols * g->rows, per = gl->points ? 1 : 4, done = 0;
    long key[GL_KEY];
    if (g->cols != s->cols || g->rows != s->rows || !cells)
        return;
    gl_key(s, key);
    if (g->stale || memcmp(key, g->key, sizeof key))
        lgrid_build(s, g);
    if (!*pp)
        *pp = gl->points ? gl_program(VS_LPOINT, FS_LPOINT) : gl_program(VS_LQUAD, FS_LQUAD);
    if (!(p = *pp))
        return;
    if (!g->vbo)
        glGenBuffers(1, &g->vbo);
    glUseProgram(p);
    a_pos = glGetAttribLocation(p, "a_pos");
    a_ink = glGetAttribLocation(p, "a_ink");
    a_ground = glGetAttribLocation(p, "a_ground");
    a_uv = glGetAttribLocation(p, "a_uv");
    a_gal = glGetAttribLocation(p, "a_gal");
    a_f = gl->points ? -1 : glGetAttribLocation(p, "a_f");
    glUniform2f(glGetUniformLocation(p, "u_screen"), (float)s->width, (float)s->height);
    gl_turn(s, p);
    glUniform2f(glGetUniformLocation(p, "u_slot"), (float)s->cw / gl->atlas_w,
                (float)s->ch / gl->atlas_h);
    glUniform1i(glGetUniformLocation(p, "u_atlas"), 0);
    if (gl->points) {
        glUniform2f(glGetUniformLocation(p, "u_cell"), (float)s->cw, (float)s->ch);
        glUniform1f(glGetUniformLocation(p, "u_size"), (float)(s->cw > s->ch ? s->cw : s->ch));
    }
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, gl->atlas);
    glBindBuffer(GL_ARRAY_BUFFER, g->vbo);
    if (!g->uploaded) {
        gpu_vertices(g->verts, sizeof(Vert) * cells * per);
        g->uploaded = 1;
    }
    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glEnableVertexAttribArray(a_pos);
    glEnableVertexAttribArray(a_ink);
    glEnableVertexAttribArray(a_ground);
    glEnableVertexAttribArray(a_uv);
    glEnableVertexAttribArray(a_gal);
    if (a_f >= 0)
        glEnableVertexAttribArray(a_f);
    while (done < cells) {
        int batch = cells - done;
        uintptr_t base = sizeof(Vert) * (size_t)done * per;
#define AT(off) ((const void *)(base + (off)))
        if (!gl->points && batch > BATCH)
            batch = BATCH;
        glVertexAttribPointer(a_pos, 2, GL_UNSIGNED_SHORT, GL_FALSE, sizeof(Vert), AT(0));
        glVertexAttribPointer(a_ink, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(4));
        glVertexAttribPointer(a_ground, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(8));
        glVertexAttribPointer(a_uv, 2, GL_UNSIGNED_SHORT, GL_TRUE, sizeof(Vert), AT(12));
        glVertexAttribPointer(a_gal, 1, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(18));
        if (gl->points) {
            glDrawArrays(GL_POINTS, 0, batch);
        } else {
            glVertexAttribPointer(a_f, 2, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vert), AT(16));
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, gl->ibo);
            glDrawElements(GL_TRIANGLES, batch * 6, GL_UNSIGNED_SHORT, 0);
        }
#undef AT
        done += batch;
    }
    glDisableVertexAttribArray(a_gal);
    glDisable(GL_BLEND);
}

/* The layers above the grid, drawn over it in order: at each z a
 * picture, or a cells layer's grid. */
static void pics_gl(Screen *s)
{
    int k;
    for (k = 0; k < LAYERS; k++) {
        Pic *p = &s->pics[k];
        if (s->cells[k]) {
            LGrid *g = s->cells[k];
            if (g->face)
                face_swap(s, g->face);
            lgrid_gl(s, g);
            if (g->face) {
                face_swap(s, g->face);
                glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
            }
        }
        GLfloat x0 = (GLfloat)p->x, y0 = (GLfloat)p->y, x1 = x0 + p->w, y1 = y0 + p->h;
        GLfloat v[16] = {x0, y0, 0, 0, x1, y0, 1, 0, x1, y1, 1, 1, x0, y1, 0, 1};
        GLint a_pos, a_uv;
        GLuint prog;
        if (!p->on || (p->yuv ? !p->plane[2] : !p->tex))
            continue;
        if (!s->vbo_pic)
            glGenBuffers(1, &s->vbo_pic);
        if (!s->prog_pic)
            s->prog_pic = gl_program(VS_STATS, FS_PIC);
        if (p->yuv && !s->prog_yuv)
            s->prog_yuv = gl_program(VS_STATS, FS_YUV);
        prog = p->yuv ? s->prog_yuv : s->prog_pic;
        if (!prog)
            return;
        glUseProgram(prog);
        a_pos = glGetAttribLocation(prog, "a_pos");
        a_uv = glGetAttribLocation(prog, "a_uv");
        glUniform2f(glGetUniformLocation(prog, "u_screen"), (float)s->width,
                    (float)s->height);
        gl_turn(s, prog);
        if (p->yuv) {
            static const char *const planes[3] = {"u_y", "u_u", "u_v"};
            int i;
            for (i = 2; i >= 0; i--) {
                glUniform1i(glGetUniformLocation(prog, planes[i]), i);
                glActiveTexture(GL_TEXTURE0 + i);
                glBindTexture(GL_TEXTURE_2D, p->plane[i]);
            }
        } else {
            glUniform1i(glGetUniformLocation(prog, "u_pic"), 0);
            glActiveTexture(GL_TEXTURE0);
            glBindTexture(GL_TEXTURE_2D, p->tex);
        }
        glBindBuffer(GL_ARRAY_BUFFER, s->vbo_pic);
        glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof v, v, GL_STREAM_DRAW);
        glEnableVertexAttribArray(a_pos);
        glEnableVertexAttribArray(a_uv);
        glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat), (const void *)0);
        glVertexAttribPointer(a_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                              (const void *)(uintptr_t)(2 * sizeof(GLfloat)));
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s->gl.ibo);
        if (!p->yuv) {
            glEnable(GL_BLEND);
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        }
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, 0);
        glDisable(GL_BLEND);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    }
}

/* In software, the layers above go onto the glass for as long as it takes
 * to push them out, with what they cover kept aside and put back after,
 * as the overlay is: `up` puts them on, lowest first, and then takes them
 * off again, highest first. */
static void pics_soft(Screen *s, int up)
{
    int i;
    for (i = 0; i < LAYERS; i++) {
        Pic *p = &s->pics[up ? i : LAYERS - 1 - i];
        if (up) {
            SDL_Rect box = {p->x, p->y, p->w, p->h}, from, at, whole;
            int x1 = box.x + box.w, y1 = box.y + box.h;
            if (box.x < 0) box.x = 0;
            if (box.y < 0) box.y = 0;
            if (x1 > s->glass->w) x1 = s->glass->w;
            if (y1 > s->glass->h) y1 = s->glass->h;
            box.w = x1 - box.x;
            box.h = y1 - box.y;
            if (p->was.w)
                dirty_add(s, &p->was);      /* where it was, the picture again */
            if (!p->on || !p->soft || box.w <= 0 || box.h <= 0) {
                p->box = p->was = (SDL_Rect){0, 0, 0, 0};
                continue;
            }
            p->box = p->was = box;
            whole = (SDL_Rect){0, 0, box.w, box.h};
            SDL_BlitSurface(s->glass, &box, p->under, &whole);
            from = (SDL_Rect){box.x - p->x - p->sx, box.y - p->y - p->sy, box.w, box.h};
            at = box;
            SDL_BlitSurface(p->soft, &from, s->glass, &at);
            dirty_add(s, &box);
        } else if (p->box.w) {
            SDL_Rect whole = {0, 0, p->box.w, p->box.h}, at = p->box;
            SDL_BlitSurface(p->under, &whole, s->glass, &at);
        }
    }
}

/* ---- kitty graphics
 *
 * Pictures in the grid's own stream, as kitty's terminal protocol sends
 * them: `ESC _G <keys> ; <base64> ESC \`. Each image is decoded as its last
 * chunk arrives and, on the GPU, uploaded once as a texture; each placement
 * is a quad over the cells it covers, worked out from the cell size every
 * time it is drawn, so a font change moves it with its cells. What is
 * refused is said on the base session's `out`. */

static void kitty_say(Screen *s, uint32_t id, const char *why);

static void kitty_pending_end(Kitty *k)
{
    free(k->data);
    k->data = NULL;
    k->len = k->cap = 0;
    k->pending = k->refused = 0;
    k->quad = 0;
    k->nquad = k->bad = 0;
}

/* The keys, and where the payload starts. A key this painter does not draw
 * and is not asked to refuse is let by. */
static size_t kitty_keys(const uint8_t *p, size_t n, KKeys *k)
{
    size_t i = 0;
    memset(k, 0, sizeof *k);
    while (i < n && p[i] != ';') {
        uint8_t key = p[i];
        size_t v = i + 2, e;
        long long num = 0;
        int neg = 0;
        if (i + 1 >= n || p[i + 1] != '=') {
            while (i < n && p[i] != ',' && p[i] != ';')
                i++;
            if (i < n && p[i] == ',')
                i++;
            continue;
        }
        for (e = v; e < n && p[e] != ',' && p[e] != ';'; e++)
            ;
        if (v < e && p[v] == '-')
            neg = 1;
        {
            size_t d;
            for (d = v + neg; d < e && p[d] >= '0' && p[d] <= '9'; d++)
                if (num < 100000000000LL)
                    num = num * 10 + (p[d] - '0');
        }
        if (neg)
            num = -num;
        if (num > INT_MAX) num = INT_MAX;
        if (num < INT_MIN) num = INT_MIN;
        if (key != 'm' && key != 'q')
            k->other = 1;
        switch (key) {
        case 'a': k->a = v < e ? (char)p[v] : 0; break;
        case 't': k->t = v < e ? (char)p[v] : 0; break;
        case 'd': k->d = v < e ? (char)p[v] : 0; break;
        case 'o': k->o = v < e ? (char)p[v] : 0; break;
        case 'f': k->f = (int)num; break;
        case 's': k->s = (int)num; break;
        case 'v': k->v = (int)num; break;
        case 'm': k->m = (int)num; break;
        case 'U': k->U = (int)num; break;
        case 'C': k->C = (int)num; break;
        case 'z': k->z = (int)num; break;
        case 'c': k->c = num < 0 ? 0 : num > 1000 ? 1000 : (int)num; break;
        case 'r': k->r = num < 0 ? 0 : num > 1000 ? 1000 : (int)num; break;
        case 'i': k->i = num < 0 ? 0 : (uint32_t)(num > 0xFFFFFFFFLL ? 0xFFFFFFFFLL : num); break;
        case 'p': k->p = num < 0 ? 0 : (uint32_t)(num > 0xFFFFFFFFLL ? 0xFFFFFFFFLL : num); break;
        default: break;
        }
        i = e < n && p[e] == ',' ? e + 1 : e;
    }
    return i < n ? i + 1 : n;
}

/* Base64 onto the image arriving, carried across chunks. -1 once what it
 * decodes to is past the limit. */
static int kitty_b64(Kitty *k, const uint8_t *p, size_t n)
{
    size_t i, need = k->len + (n / 4 + 1) * 3;
    if (need > KITTY_RECEIVED + 3)
        need = KITTY_RECEIVED + 3;
    if (need > k->cap) {
        size_t cap = k->cap ? k->cap : 4096;
        uint8_t *d;
        while (cap < need)
            cap *= 2;
        d = realloc(k->data, cap);
        if (!d)
            return -1;
        k->data = d;
        k->cap = cap;
    }
    static int8_t table[256];
    if (!table['B']) {
        const char *a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        memset(table, -1, sizeof table);
        for (i = 0; i < 64; i++)
            table[(uint8_t)a[i]] = (int8_t)i;
    }
    for (i = 0; i < n; i++) {
        uint8_t c = p[i];
        int v = table[c];
        if (v >= 0 && k->nquad == 0 && i + 4 <= n && k->len + 3 <= KITTY_RECEIVED) {
            /* a whole quad at once, the common case */
            int b = table[p[i + 1]], d = table[p[i + 2]], e = table[p[i + 3]];
            if ((b | d | e) >= 0) {
                uint32_t q = (uint32_t)v << 18 | (uint32_t)b << 12 | (uint32_t)d << 6 | (uint32_t)e;
                k->data[k->len++] = (uint8_t)(q >> 16);
                k->data[k->len++] = (uint8_t)(q >> 8);
                k->data[k->len++] = (uint8_t)q;
                i += 3;
                continue;
            }
        }
        if (v >= 0)
            ;
        else if (c == '=') {
            /* padding closes a quad early */
            if (k->nquad == 2)
                k->data[k->len++] = (uint8_t)(k->quad >> 4);
            else if (k->nquad == 3) {
                k->data[k->len++] = (uint8_t)(k->quad >> 10);
                k->data[k->len++] = (uint8_t)(k->quad >> 2);
            }
            k->quad = 0;
            k->nquad = 0;
            continue;
        } else if (c == ' ' || c == '\n' || c == '\r') continue;
        else { k->bad = 1; continue; }
        k->quad = k->quad << 6 | (uint32_t)v;
        if (++k->nquad == 4) {
            if (k->len + 3 > KITTY_RECEIVED)
                return -1;
            k->data[k->len++] = (uint8_t)(k->quad >> 16);
            k->data[k->len++] = (uint8_t)(k->quad >> 8);
            k->data[k->len++] = (uint8_t)k->quad;
            k->quad = 0;
            k->nquad = 0;
        }
    }
    return 0;
}

static int kitty_find(Screen *s, uint32_t id)
{
    int i;
    for (i = 0; i < KITTY_IMAGES; i++)
        if (s->kitty.img[i].used && s->kitty.img[i].id == id)
            return i;
    return -1;
}

/* A placement taken down: in software the glass where it was is pushed out
 * again, now without it. */
static void kitty_unplace(Screen *s, KPlace *pl)
{
    if (!pl->used)
        return;
    if (!s->gpu && pl->was.w) {
        dirty_add(s, &pl->was);
        s->owed = 1;
    }
    SDL_FreeSurface(pl->soft);
    SDL_FreeSurface(pl->under);
    memset(pl, 0, sizeof *pl);
    s->owed = 1;
}

static void kitty_forget(Screen *s, int idx)
{
    KImage *im = &s->kitty.img[idx];
    int j;
    for (j = 0; j < KITTY_PLACES; j++)
        if (s->kitty.place[j].used && s->kitty.place[j].image == idx)
            kitty_unplace(s, &s->kitty.place[j]);
    if (im->tex) {
        gl_use(s);
        glDeleteTextures(1, &im->tex);
    }
    free(im->rgba);
    s->kitty.decoded -= im->bytes;
    memset(im, 0, sizeof *im);
}

/* Everything the stream's sender had: its session has ended. */
static void kitty_clear(Screen *s)
{
    int i;
    for (i = 0; i < KITTY_IMAGES; i++)
        if (s->kitty.img[i].used)
            kitty_forget(s, i);
    kitty_pending_end(&s->kitty);
    s->kitty.skip = 0;
}

/* Where a placement is on the glass, and the cells it covers, from the
 * cell size now: c and r both given, stretched to fill them; one given,
 * the other from the picture's shape; neither, its own pixels. */
static void kitty_box(const Screen *s, const KPlace *pl, SDL_Rect *r, int *cols, int *rows)
{
    const KImage *im = &s->kitty.img[pl->image];
    int w, h, cw = s->cw > 0 ? s->cw : 1, ch = s->ch > 0 ? s->ch : 1;
    if (pl->c && pl->r) {
        w = pl->c * cw;
        h = pl->r * ch;
    } else if (pl->c) {
        w = pl->c * cw;
        h = (int)((double)w * im->h / im->w + 0.5);
    } else if (pl->r) {
        h = pl->r * ch;
        w = (int)((double)h * im->w / im->h + 0.5);
    } else {
        w = im->w;
        h = im->h;
    }
    r->x = s->ox + pl->col * cw;
    r->y = s->oy + pl->row * ch;
    r->w = w < 1 ? 1 : w;
    r->h = h < 1 ? 1 : h;
    if (cols)
        *cols = (r->w + cw - 1) / cw;
    if (rows)
        *rows = (r->h + ch - 1) / ch;
}

/* The placements in the order they are drawn: by z, then as placed. */
static int kitty_order(const Screen *s, int *out)
{
    int n = 0, i, j;
    for (i = 0; i < KITTY_PLACES; i++) {
        const KPlace *a = &s->kitty.place[i];
        if (!a->used)
            continue;
        for (j = n; j > 0; j--) {
            const KPlace *b = &s->kitty.place[out[j - 1]];
            if (b->z < a->z || (b->z == a->z && b->seq < a->seq))
                break;
            out[j] = out[j - 1];
        }
        out[j] = i;
        n++;
    }
    return n;
}

/* Place image `idx` at the cursor. */
static void kitty_place(Screen *s, const KKeys *k, int idx)
{
    Kitty *kt = &s->kitty;
    KPlace *pl = NULL;
    int j, cols, rows;
    if (k->p)
        for (j = 0; j < KITTY_PLACES; j++)
            if (kt->place[j].used && kt->place[j].image == idx && kt->place[j].pid == k->p) {
                kitty_unplace(s, &kt->place[j]);
                pl = &kt->place[j];
                break;
            }
    for (j = 0; !pl && j < KITTY_PLACES; j++)
        if (!kt->place[j].used)
            pl = &kt->place[j];
    if (!pl) {
        kitty_say(s, k->i, "too many");
        return;
    }
    pl->used = 1;
    pl->image = idx;
    pl->pid = k->p;
    pl->col = s->x;
    pl->row = s->y;
    pl->c = k->c;
    pl->r = k->r;
    pl->z = k->z;
    pl->seq = ++kt->seq;
    s->owed = 1;
    if (!k->C) {
        SDL_Rect box;
        kitty_box(s, pl, &box, &cols, &rows);
        s->x += cols;
        s->y += rows - 1;
        if (s->x > s->cols) s->x = s->cols;
        if (s->y > s->rows - 1) s->y = s->rows - 1;
        if (s->y < 0) s->y = 0;
    }
}

/* The image whose last chunk has come: decoded, checked against the
 * limits, kept, and placed if it was sent with a=T. */
static void kitty_finish(Screen *s)
{
    Kitty *kt = &s->kitty;
    KKeys *k = &kt->pk;
    char a = k->a ? k->a : 't';
    int f = k->f ? k->f : 32, w, h, idx = -1, i;
    uint8_t *px = NULL;
    png_image png;
    size_t bytes, old = 0;
    KImage *im;
    if (kt->nquad == 2)
        kt->data[kt->len++] = (uint8_t)(kt->quad >> 4);
    else if (kt->nquad == 3) {
        kt->data[kt->len++] = (uint8_t)(kt->quad >> 10);
        kt->data[kt->len++] = (uint8_t)(kt->quad >> 2);
    }
    kt->nquad = 0;
    if (a != 'q') {
        if (k->i)
            idx = kitty_find(s, k->i);
        for (i = 0; idx < 0 && i < KITTY_IMAGES; i++)
            if (!kt->img[i].used)
                idx = i;
        if (idx < 0) {
            kitty_say(s, k->i, "too many");
            return;
        }
    }
    if (f != 100 && f != 24 && f != 32) {
        kitty_say(s, k->i, "not offered");
        return;
    }
    if (kt->bad || !kt->len) {
        kitty_say(s, k->i, "does not decode");
        return;
    }
    memset(&png, 0, sizeof png);
    png.version = PNG_IMAGE_VERSION;
    if (f == 100) {
        if (!png_image_begin_read_from_memory(&png, kt->data, kt->len)) {
            png_image_free(&png);
            kitty_say(s, k->i, "does not decode");
            return;
        }
        if (png.width > KITTY_SIDE || png.height > KITTY_SIDE) {
            png_image_free(&png);
            kitty_say(s, k->i, "too big");
            return;
        }
        w = (int)png.width;
        h = (int)png.height;
    } else {
        w = k->s;
        h = k->v;
        if (w < 1 || h < 1) {
            kitty_say(s, k->i, "does not decode");
            return;
        }
        if (w > KITTY_SIDE || h > KITTY_SIDE) {
            kitty_say(s, k->i, "too big");
            return;
        }
        if (kt->len != (size_t)w * h * (f == 24 ? 3 : 4)) {
            kitty_say(s, k->i, "does not decode");
            return;
        }
    }
    bytes = (size_t)w * h * 4;
    if (idx >= 0 && kt->img[idx].used)
        old = kt->img[idx].bytes;
    if (kt->decoded - old + bytes > KITTY_WALL
        || (s->gpu && (w > s->gl.max_texture || h > s->gl.max_texture))) {
        if (f == 100)
            png_image_free(&png);
        kitty_say(s, k->i, "too big");
        return;
    }
    if (f == 100) {
        /* finish_read frees the image, whether or not it succeeds */
        png.format = PNG_FORMAT_RGBA;
        px = malloc(bytes);
        if (!px) {
            png_image_free(&png);
            kitty_say(s, k->i, "too big");
            return;
        }
        if (!png_image_finish_read(&png, NULL, px, 0, NULL)) {
            free(px);
            kitty_say(s, k->i, "does not decode");
            return;
        }
    } else {
        size_t n = (size_t)w * h, j;
        px = malloc(bytes);
        if (!px) {
            kitty_say(s, k->i, "too big");
            return;
        }
        if (f == 32)
            memcpy(px, kt->data, bytes);
        else
            for (j = 0; j < n; j++) {
                px[j * 4] = kt->data[j * 3];
                px[j * 4 + 1] = kt->data[j * 3 + 1];
                px[j * 4 + 2] = kt->data[j * 3 + 2];
                px[j * 4 + 3] = 255;
            }
    }
    if (a == 'q') {
        free(px);
        kitty_say(s, k->i, NULL);
        return;
    }
    im = &kt->img[idx];
    if (im->used) {
        /* The same id sent again is the picture replaced where it is placed. */
        int j;
        for (j = 0; j < KITTY_PLACES; j++)
            if (kt->place[j].used && kt->place[j].image == idx) {
                SDL_FreeSurface(kt->place[j].soft);
                kt->place[j].soft = NULL;
            }
        free(im->rgba);
        im->rgba = NULL;
        kt->decoded -= im->bytes;
    } else {
        im->used = 1;
        im->id = k->i ? k->i : 0xF0000000u + (kt->auto_id++ & 0x0FFFFFFFu);
    }
    im->w = w;
    im->h = h;
    im->bytes = bytes;
    kt->decoded += bytes;
    if (s->gpu) {
        gl_use(s);
        pic_texture(&im->tex, 1);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
        free(px);
    } else {
        im->rgba = px;
    }
    s->owed = 1;
    if (a == 'T')
        kitty_place(s, k, idx);
}

static void kitty_delete(Screen *s, const KKeys *k)
{
    Kitty *kt = &s->kitty;
    char d = k->d ? k->d : 'a';
    int j, idx;
    switch (d) {
    case 'a':
    case 'A':
        for (j = 0; j < KITTY_PLACES; j++)
            kitty_unplace(s, &kt->place[j]);
        if (d == 'A')
            for (j = 0; j < KITTY_IMAGES; j++)
                if (kt->img[j].used)
                    kitty_forget(s, j);
        break;
    case 'i':
    case 'I':
    case 'p':
    case 'P':
        idx = k->i ? kitty_find(s, k->i) : -1;
        if (idx < 0 || ((d == 'p' || d == 'P') && !k->p))
            break;
        for (j = 0; j < KITTY_PLACES; j++)
            if (kt->place[j].used && kt->place[j].image == idx
                && (!k->p || kt->place[j].pid == k->p))
                kitty_unplace(s, &kt->place[j]);
        if (d == 'I') {
            for (j = 0; j < KITTY_PLACES; j++)
                if (kt->place[j].used && kt->place[j].image == idx)
                    break;
            if (j == KITTY_PLACES)
                kitty_forget(s, idx);
        }
        break;
    default:
        kitty_say(s, k->i, "not offered");
        break;
    }
}

/* A refusal on a string that said more chunks follow: they are let go. */
static void kitty_refuse(Screen *s, const KKeys *k, const char *why)
{
    kitty_say(s, k->i, why);
    if (k->m) {
        s->kitty.pending = 1;
        s->kitty.refused = 1;
        s->kitty.pk = *k;
    }
}

/* One kitty string, the bytes after `ESC _G`. */
static void kitty_apc(Screen *s, const uint8_t *p, size_t n)
{
    Kitty *kt = &s->kitty;
    KKeys k;
    size_t off = kitty_keys(p, n, &k);
    char a;
    int idx;
    if (kt->pending && !k.other) {
        if (!kt->refused && kitty_b64(kt, p + off, n - off) < 0) {
            kitty_say(s, kt->pk.i, "too big");
            kt->refused = 1;
        }
        if (!k.m) {
            if (!kt->refused)
                kitty_finish(s);
            kitty_pending_end(kt);
        }
        return;
    }
    kitty_pending_end(kt);      /* one whose chunks stopped coming */
    a = k.a ? k.a : 't';
    if (k.U || k.o) {
        kitty_refuse(s, &k, "not offered");
        return;
    }
    switch (a) {
    case 't':
    case 'T':
    case 'q':
        if (k.t && k.t != 'd') {
            kitty_refuse(s, &k, "not offered");
            return;
        }
        kt->pending = 1;
        kt->pk = k;
        if (kitty_b64(kt, p + off, n - off) < 0) {
            kitty_say(s, k.i, "too big");
            kt->refused = 1;
        }
        if (!k.m) {
            if (!kt->refused)
                kitty_finish(s);
            kitty_pending_end(kt);
        }
        break;
    case 'p':
        idx = k.i ? kitty_find(s, k.i) : -1;
        if (idx < 0)
            kitty_say(s, k.i, "no such image");
        else
            kitty_place(s, &k, idx);
        break;
    case 'd':
        kitty_delete(s, &k);
        break;
    default:
        kitty_refuse(s, &k, "not offered");
        break;
    }
}

/* A string too long to hold: refused, and its bytes let go up to its end. */
static void kitty_overlong(Screen *s)
{
    Kitty *kt = &s->kitty;
    kitty_say(s, kt->pending ? kt->pk.i : 0, "too big");
    if (kt->pending)
        kt->refused = 1;
    kt->skip = 1;
}

/* One picture as a quad over its box, blended by its alpha. */
static void kitty_quad(Screen *s, GLuint tex, const SDL_Rect *r, int smooth)
{
    GLfloat x0 = (GLfloat)r->x, y0 = (GLfloat)r->y, x1 = x0 + r->w, y1 = y0 + r->h;
    GLfloat v[16] = {x0, y0, 0, 0, x1, y0, 1, 0, x1, y1, 1, 1, x0, y1, 0, 1};
    GLint a_pos, a_uv;
    if (!s->vbo_pic)
        glGenBuffers(1, &s->vbo_pic);
    if (!s->prog_pic)
        s->prog_pic = gl_program(VS_STATS, FS_PIC);
    if (!s->prog_pic)
        return;
    glUseProgram(s->prog_pic);
    a_pos = glGetAttribLocation(s->prog_pic, "a_pos");
    a_uv = glGetAttribLocation(s->prog_pic, "a_uv");
    glUniform2f(glGetUniformLocation(s->prog_pic, "u_screen"), (float)s->width, (float)s->height);
    gl_turn(s, s->prog_pic);
    glUniform1i(glGetUniformLocation(s->prog_pic, "u_pic"), 0);
    glActiveTexture(GL_TEXTURE0);
    pic_texture(&tex, smooth);
    glBindBuffer(GL_ARRAY_BUFFER, s->vbo_pic);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof v, v, GL_STREAM_DRAW);
    glEnableVertexAttribArray(a_pos);
    glEnableVertexAttribArray(a_uv);
    glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat), (const void *)0);
    glVertexAttribPointer(a_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                          (const void *)(uintptr_t)(2 * sizeof(GLfloat)));
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, s->gl.ibo);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, 0);
    glDisable(GL_BLEND);
}

/* The placements, on the grid just drawn: those below the text first, the
 * text again over them, then the rest. */
static void kitty_gl(Screen *s)
{
    int order[KITTY_PLACES], n = kitty_order(s, order), i, below = 0;
    if (!n)
        return;
    for (i = 0; i < n; i++) {
        const KPlace *pl = &s->kitty.place[order[i]];
        const KImage *im = &s->kitty.img[pl->image];
        SDL_Rect r;
        if (pl->z >= 0 && below) {
            gl_grid(s, 1);
            below = 0;
        }
        if (pl->z < 0)
            below = 1;
        kitty_box(s, pl, &r, NULL, NULL);
        if (im->tex)
            kitty_quad(s, im->tex, &r, r.w != im->w || r.h != im->h);
    }
    if (below)
        gl_grid(s, 1);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
}

/* In software each placement is scaled onto a surface of its own, the part
 * of it on the glass, nearest sample, and made again only when that part
 * changes; it goes on the glass for as long as the glass is pushed out,
 * as a layer above does, the text not drawn again over it. */
static SDL_Surface *kitty_soft_make(Screen *s, KPlace *pl, const SDL_Rect *r, const SDL_Rect *box)
{
    const KImage *im = &s->kitty.img[pl->image];
    int x, y;
    if (pl->soft && pl->soft->w == box->w && pl->soft->h == box->h && pl->box.x == box->x
        && pl->box.y == box->y)
        return pl->soft;
    SDL_FreeSurface(pl->soft);
    SDL_FreeSurface(pl->under);
    pl->soft = SDL_CreateRGBSurfaceWithFormat(0, box->w, box->h, 32, SDL_PIXELFORMAT_ARGB8888);
    pl->under = SDL_CreateRGBSurfaceWithFormat(0, box->w, box->h, s->glass->format->BitsPerPixel,
                                               s->glass->format->format);
    if (!pl->soft || !pl->under || !im->rgba) {
        SDL_FreeSurface(pl->soft);
        SDL_FreeSurface(pl->under);
        pl->soft = pl->under = NULL;
        return NULL;
    }
    SDL_SetSurfaceBlendMode(pl->soft, SDL_BLENDMODE_BLEND);
    SDL_SetSurfaceBlendMode(pl->under, SDL_BLENDMODE_NONE);
    SDL_LockSurface(pl->soft);
    for (y = 0; y < box->h; y++) {
        Uint32 *d = (Uint32 *)((uint8_t *)pl->soft->pixels + (size_t)y * pl->soft->pitch);
        int sy = (int)((long)(box->y - r->y + y) * im->h / r->h);
        for (x = 0; x < box->w; x++) {
            int sx = (int)((long)(box->x - r->x + x) * im->w / r->w);
            const uint8_t *q = im->rgba + ((size_t)sy * im->w + sx) * 4;
            d[x] = (Uint32)q[3] << 24 | (Uint32)q[0] << 16 | (Uint32)q[1] << 8 | q[2];
        }
    }
    SDL_UnlockSurface(pl->soft);
    return pl->soft;
}

static void kitty_soft(Screen *s, int up)
{
    int order[KITTY_PLACES], n = kitty_order(s, order), i;
    for (i = 0; i < n; i++) {
        KPlace *pl = &s->kitty.place[order[up ? i : n - 1 - i]];
        if (up) {
            SDL_Rect r, box, at, whole;
            int x1, y1;
            kitty_box(s, pl, &r, NULL, NULL);
            box = r;
            x1 = box.x + box.w;
            y1 = box.y + box.h;
            if (box.x < 0) box.x = 0;
            if (box.y < 0) box.y = 0;
            if (x1 > s->glass->w) x1 = s->glass->w;
            if (y1 > s->glass->h) y1 = s->glass->h;
            box.w = x1 - box.x;
            box.h = y1 - box.y;
            if (pl->was.w)
                dirty_add(s, &pl->was);
            if (box.w <= 0 || box.h <= 0 || !kitty_soft_make(s, pl, &r, &box)) {
                pl->box = pl->was = (SDL_Rect){0, 0, 0, 0};
                continue;
            }
            pl->box = pl->was = box;
            whole = (SDL_Rect){0, 0, box.w, box.h};
            SDL_BlitSurface(s->glass, &box, pl->under, &whole);
            at = box;
            SDL_BlitSurface(pl->soft, &whole, s->glass, &at);
            dirty_add(s, &box);
        } else if (pl->box.w && pl->under) {
            SDL_Rect whole = {0, 0, pl->box.w, pl->box.h}, at = pl->box;
            SDL_BlitSurface(pl->under, &whole, s->glass, &at);
        }
    }
}

/* In software, a pixel of the glass as a picture has the placements over
 * it blended in, as they would be pushed out. */
static void kitty_blend(const Screen *s, const int *order, int n, int sx, int sy, uint8_t *o)
{
    int i, c;
    for (i = 0; i < n; i++) {
        const KPlace *pl = &s->kitty.place[order[i]];
        const KImage *im = &s->kitty.img[pl->image];
        SDL_Rect r;
        const uint8_t *q;
        unsigned a;
        kitty_box(s, pl, &r, NULL, NULL);
        if (!im->rgba || sx < r.x || sy < r.y || sx >= r.x + r.w || sy >= r.y + r.h)
            continue;
        q = im->rgba + ((size_t)((long)(sy - r.y) * im->h / r.h) * im->w
                        + (size_t)((long)(sx - r.x) * im->w / r.w)) * 4;
        a = q[3];
        for (c = 0; c < 3; c++)
            o[c] = (uint8_t)((q[c] * a + o[c] * (255 - a) + 127) / 255);
    }
}

/* ---- the glass as a picture */

/* A quad given straight in clip space, for drawing one texture over a
 * whole target. */
static const char *VS_COPY =
    "attribute vec2 a_pos; attribute vec2 a_uv; varying vec2 v_uv;\n"
    "void main() { gl_Position = vec4(a_pos, 0.0, 1.0); v_uv = a_uv; }\n";

/* Each pixel of a smaller glass the mean of sixteen looks spread across
 * the pixels it covers, each look itself the mean of four: a box filter,
 * so text shrinks to its weight rather than breaking up. */
static const char *FS_DOWN =
    "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\n"
    "precision mediump float;\n#endif\n"
    "uniform sampler2D u_src; uniform vec2 u_foot;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "  vec4 c = vec4(0.0);\n"
    "  for (int i = 0; i < 4; i++)\n"
    "    for (int j = 0; j < 4; j++)\n"
    "      c += texture2D(u_src, v_uv + (vec2(float(i), float(j)) - 1.5) * 0.25 * u_foot);\n"
    "  gl_FragColor = vec4(c.rgb / 16.0, 1.0);\n"
    "}\n";

/* Framebuffer `k` of the two a picture is drawn through, w by h, made or
 * remade to that size and bound. -1 when GL will not draw into it. */
static int cap_target(Screen *s, int k, int w, int h)
{
    if (!s->cap_tex[k]) {
        glGenTextures(1, &s->cap_tex[k]);
        glGenFramebuffers(1, &s->cap_fb[k]);
    }
    if (s->cap_w[k] != w || s->cap_h[k] != h) {
        glBindTexture(GL_TEXTURE_2D, s->cap_tex[k]);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        glBindFramebuffer(GL_FRAMEBUFFER, s->cap_fb[k]);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                               s->cap_tex[k], 0);
        s->cap_w[k] = w;
        s->cap_h[k] = h;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, s->cap_fb[k]);
    return glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE ? 0 : -1;
}

/* The glass as it shows, every layer and not the overlay, as RGBA w by h,
 * top row first; null where it cannot be read. On the GPU the grid and
 * the layers above are drawn again into a framebuffer of the glass's own,
 * and a smaller picture is drawn from that on the GPU as well, so only
 * the pixels asked for are read back. In software the glass is sampled
 * where it is, with the layers above blended in as they would be. */
static uint8_t *glass_rgba(Screen *s, int w, int h)
{
    uint8_t *px;
    int x, y;
    if (w < 1 || h < 1)
        return NULL;
    px = malloc((size_t)w * h * 4);
    if (!px)
        return NULL;
    if (s->gpu) {
        Gl *g = &s->gl;
        int small = w != s->width || h != s->height, ok;
        if (s->width > g->max_texture || s->height > g->max_texture
            || w > g->max_texture || h > g->max_texture) {
            free(px);
            return NULL;
        }
        gl_use(s);
        ok = cap_target(s, 0, s->width, s->height) == 0;
        if (ok) {
            s->capturing = 1;
            gl_draw(s);
            pics_gl(s);
            s->capturing = 0;
        }
        if (ok && small && (ok = cap_target(s, 1, w, h) == 0)) {
            GLfloat v[16] = {-1, -1, 0, 0, 1, -1, 1, 0, 1, 1, 1, 1, -1, 1, 0, 1};
            GLint a_pos, a_uv;
            if (!s->cap_prog) {
                s->cap_prog = gl_program(VS_COPY, FS_DOWN);
                glGenBuffers(1, &s->cap_vbo);
            }
            ok = s->cap_prog != 0;
            if (ok) {
                glViewport(0, 0, w, h);
                glUseProgram(s->cap_prog);
                a_pos = glGetAttribLocation(s->cap_prog, "a_pos");
                a_uv = glGetAttribLocation(s->cap_prog, "a_uv");
                glUniform1i(glGetUniformLocation(s->cap_prog, "u_src"), 0);
                glUniform2f(glGetUniformLocation(s->cap_prog, "u_foot"), 1.0f / w, 1.0f / h);
                glActiveTexture(GL_TEXTURE0);
                glBindTexture(GL_TEXTURE_2D, s->cap_tex[0]);
                glBindBuffer(GL_ARRAY_BUFFER, s->cap_vbo);
                glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof v, v, GL_STREAM_DRAW);
                glEnableVertexAttribArray(a_pos);
                glEnableVertexAttribArray(a_uv);
                glVertexAttribPointer(a_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                                      (const void *)0);
                glVertexAttribPointer(a_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(GLfloat),
                                      (const void *)(uintptr_t)(2 * sizeof(GLfloat)));
                glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g->ibo);
                glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, 0);
            }
        }
        if (ok)
            glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px);
        glBindFramebuffer(GL_FRAMEBUFFER, gpu_glass_fb(s));
        glViewport(0, 0, s->phys_w, s->phys_h);
        glBindTexture(GL_TEXTURE_2D, g->atlas);
        if (!ok) {
            free(px);
            return NULL;
        }
        /* GL reads from the bottom row up. */
        for (y = 0; y < h / 2; y++)
            for (x = 0; x < w * 4; x++) {
                uint8_t *a = px + (size_t)y * w * 4 + x, *b = px + (size_t)(h - 1 - y) * w * 4 + x;
                uint8_t c = *a;
                *a = *b;
                *b = c;
            }
        for (x = 0; x < w * h; x++)
            px[x * 4 + 3] = 255;
        return px;
    } else {
        SDL_Surface *g = s->target;
        int abgr = g->format->format == SDL_PIXELFORMAT_ABGR8888, k;
        int korder[KITTY_PLACES], nk = kitty_order(s, korder);
        if (g->format->BytesPerPixel != 4) {
            free(px);
            return NULL;
        }
        for (y = 0; y < h; y++) {
            int sy = (int)((long)y * g->h / h);
            const Uint32 *row = (const Uint32 *)((const uint8_t *)g->pixels + (size_t)sy * g->pitch);
            for (x = 0; x < w; x++) {
                int sx = (int)((long)x * g->w / w);
                Uint32 c = row[sx];
                uint8_t *o = px + ((size_t)y * w + x) * 4;
                o[0] = (uint8_t)(abgr ? c : c >> 16);
                o[1] = (uint8_t)(c >> 8);
                o[2] = (uint8_t)(abgr ? c >> 16 : c);
                o[3] = 255;
                if (nk)
                    kitty_blend(s, korder, nk, sx, sy, o);
                for (k = 0; k < LAYERS; k++) {
                    const Pic *p = &s->pics[k];
                    Uint32 f, a;
                    int i;
                    if (!p->on || !p->soft || sx < p->x || sy < p->y || sx >= p->x + p->w
                        || sy >= p->y + p->h)
                        continue;
                    f = ((const Uint32 *)((const uint8_t *)p->soft->pixels
                                          + (size_t)(sy - p->y) * p->soft->pitch))[sx - p->x];
                    a = f >> 24;
                    for (i = 0; i < 3; i++) {
                        Uint32 sc = f >> (16 - 8 * i) & 0xff;
                        o[i] = (uint8_t)((sc * a + o[i] * (255 - a) + 127) / 255);
                    }
                }
            }
        }
        return px;
    }
}

/* Put the glass in step with the picture. On the GPU that is the grid
 * drawn again; in software the picture is already on the window surface
 * and only the changed rectangles need pushing out. Part way through a
 * frame of the base's only the layers and the overlay are pushed out, and
 * what the base has changed so far waits for its delimiter. */
static void show(Screen *s)
{
    double t0 = now_ms(), t1;
    int held = s->drew && !s->gpu ? s->ndirty : 0;
    int held_all = s->drew && !s->gpu ? s->dirty_all : 0;
    if (s->gpu && !s->win) {
        /* No screen: there is nothing to put up, and the glass is drawn
         * only when it is looked at -- a stream's next frame, a picture --
         * from the scene as it stands then. */
        outputs_stale(s);
    } else if (s->gpu) {
        gl_glass(s);        /* which makes this screen's context current */
        stats_gl(s);
        t1 = now_ms();
        gpu_swap(s);
        s->t_flush += t1 - t0;
        s->t_swap += now_ms() - t1;
    } else {
        /* The overlay goes onto the glass for as long as it takes to push
         * it out, with the picture beneath kept aside and put back after,
         * so what is painted next is painted onto the picture alone. */
        Stats *st = &s->st;
        SDL_Rect box = st->box, at = box;
        int up = stats_shown(s) && box.w && st->soft;
        if (s->drew)
            s->dirty_all = 0;
        kitty_soft(s, 1);
        pics_soft(s, 1);
        if (up) {
            SDL_BlitSurface(s->glass, &box, st->under, NULL);
            SDL_BlitSurface(st->soft, NULL, s->glass, &at);
            dirty_add(s, &box);
        }
        if (st->was.w)
            dirty_add(s, &st->was);     /* where it was, now the picture again */
        st->was = up ? box : (SDL_Rect){0, 0, 0, 0};
        if (s->dirty_all) {
            display_push(s, NULL, 0);
            s->t_swap += now_ms() - t0;
        } else if (s->ndirty > held) {
            display_push(s, s->dirty + held, s->ndirty - held);
            s->t_swap += now_ms() - t0;
        }
        if (up) {
            at = box;
            SDL_BlitSurface(st->under, NULL, s->glass, &at);
        }
        pics_soft(s, 0);
        kitty_soft(s, 0);
    }
    s->ndirty = held;
    s->dirty_all = held_all;
    /* The video driver's events are never read, but some drivers do
     * their housekeeping in the pump, so it runs once a frame. */
    display_pump();
}

/* Put what has been drawn on the glass, at the end of a frame. While
 * blanked the picture keeps being painted but the glass stays black, so
 * that unblanking can put the current picture back at once. */
static void present(Screen *s)
{
    if (!s->drew)
        return;
    s->drew = 0;
    s->owed = 0;            /* the layers above go up with it */
    if (s->blanked) {
        s->ndirty = 0;
        return;
    }
    show(s);
    s->frames++;
    s->painted++;
    /* Only stretches where frames followed one another count as painting
     * time; a wall idle between senders is not slow, it is idle. */
    if (s->t_prev && now_ms() - s->t_prev < 500)
        s->t_active += now_ms() - s->t_prev;
    s->t_prev = now_ms();
}

/* Put the overlay up anew with no frame of the sender's in it. A frame
 * part painted is left to its delimiter, which puts the overlay up as
 * well: presenting it now would show the top half of a frame. */
static void refresh(Screen *s)
{
    if (s->blanked || s->drew)
        return;
    show(s);
    s->frames++;
    s->refreshes++;
}

/* A layer above the base has changed: the glass is owed a redraw, which
 * is put up once the wall's layers have all been read, so that several
 * frames in one pass are one redraw, and not at all when the base
 * presents first. */
static void redraw(Screen *s)
{
    s->owed = 1;
}

/* Put the glass up anew if a layer above is owed it, whatever the base is
 * doing: part way through a frame of its the base shows its last whole
 * one beneath. */
static void redraw_owed(Screen *s)
{
    if (!s->owed)
        return;
    s->owed = 0;
    if (s->blanked)
        return;
    show(s);
    s->frames++;
}

/* Take the picture away, or put it back. Blanking paints black rather
 * than stopping: a wall that simply stopped would hold whatever was last
 * on it, which is the one thing a blanked screen must not do. The picture
 * itself carries on being painted, on the GPU in its grid and in
 * software on the paper, so unblanking shows what is current. */
static void blank(Screen *s, int on)
{
    if (on == s->blanked)
        return;
    s->blanked = on;
    s->blanked_at = on ? now_ms() : 0;
    if (!on)
        power(s, 1);
    if (s->gpu) {
        gl_use(s);
        if (on) {
            glClearColor(0, 0, 0, 1);
            glClear(GL_COLOR_BUFFER_BIT);
        } else {
            gl_glass(s);
            stats_gl(s);
        }
        gpu_swap(s);
    } else if (on) {
        SDL_BlitSurface(s->glass, NULL, s->paper, NULL);
        SDL_FillRect(s->glass, NULL, SDL_MapRGB(s->glass->format, 0, 0, 0));
        s->target = s->paper;
        display_push(s, NULL, 0);
    } else {
        SDL_BlitSurface(s->paper, NULL, s->glass, NULL);
        s->target = s->glass;
        display_push(s, NULL, 0);
    }
    s->ndirty = 0;
    s->dirty_all = 0;
    display_pump();
}

static void glyphs_clear(Screen *s)
{
    int i;
    if (!s->glyphs)
        return;             /* no face loaded yet: nothing cached */
    for (i = 0; i < CACHE_SLOTS; i++) {
        Glyph *g = &s->glyphs[i];
        if (g->used && g->surf)
            SDL_FreeSurface(g->surf);
    }
    memset(s->glyphs, 0, CACHE_SLOTS * sizeof *s->glyphs);
    s->nglyphs = 0;
    s->gl.next_slot = 0;
    s->gl.last = NULL;
}

static uint32_t pack(Rgb c)
{
    return (uint32_t)c.r << 16 | (uint32_t)c.g << 8 | c.b;
}

static int utf8_encode(uint32_t cp, char *out)
{
    if (cp < 0x80) {
        out[0] = (char)cp;
        out[1] = 0;
        return 1;
    }
    if (cp < 0x800) {
        out[0] = (char)(0xC0 | cp >> 6);
        out[1] = (char)(0x80 | (cp & 0x3F));
        out[2] = 0;
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (char)(0xE0 | cp >> 12);
        out[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[2] = (char)(0x80 | (cp & 0x3F));
        out[3] = 0;
        return 3;
    }
    out[0] = (char)(0xF0 | cp >> 18);
    out[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    out[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
    out[3] = (char)(0x80 | (cp & 0x3F));
    out[4] = 0;
    return 4;
}

/* A code point's entry in the cache, or NULL with `*empty` set to where
 * it would go. */
static Glyph *glyph_find(Screen *s, uint32_t cp, uint32_t kf, uint32_t kb, uint32_t *empty)
{
    uint32_t h = (cp * 2654435761u) ^ (kf * 0x9E3779B1u) ^ (kb * 0x85EBCA77u);
    uint32_t i = h & (CACHE_SLOTS - 1);
    for (;;) {
        Glyph *g = &s->glyphs[i];
        if (!g->used) {
            *empty = i;
            return NULL;
        }
        if (g->cp == cp && g->fg == kf && g->bg == kb)
            return g;
        i = (i + 1) & (CACHE_SLOTS - 1);
    }
}

/* Find a code point's entry in the cache, making an empty one when it
 * is new. Full means starting again, which costs one stall on a wall
 * that has just changed its palette and beats growing for ever. */
static Glyph *glyph_slot(Screen *s, uint32_t cp, uint32_t kf, uint32_t kb, int *fresh)
{
    uint32_t h = (cp * 2654435761u) ^ (kf * 0x9E3779B1u) ^ (kb * 0x85EBCA77u);
    uint32_t i;
    Glyph *g = glyph_find(s, cp, kf, kb, &i);
    *fresh = 0;
    if (g)
        return g;
    g = &s->glyphs[i];
    if (s->nglyphs >= CACHE) {
        glyphs_clear(s);
        s->restarts++;
        g = &s->glyphs[h & (CACHE_SLOTS - 1)];
    }
    g->used = 1;
    g->cp = cp;
    g->fg = kf;
    g->bg = kb;
    s->nglyphs++;
    *fresh = 1;
    return g;
}

/* How many cells a glyph takes: two when the font advances past it by
 * well over one, which is the sender's cell model for wide characters. */
static int glyph_cells(Screen *s, const char *u8)
{
    int adv = 0, hh = 0;
    TTF_SizeUTF8(s->font, u8, &adv, &hh);
    return adv > s->cw * 3 / 2 ? 2 : 1;
}

/* Start the atlas and the table again, and put back the mask of every
 * cell on screen. A frame is a diff, so a cell nobody has mentioned for
 * an hour must still show what it was given; clearing the atlas alone
 * would leave it pointing at a slot some newer glyph has since taken.
 * The atlas holds the grid twice over wherever the GPU allows it, which
 * is more than any picture needs, so what is put back always fits and
 * leaves room for a good many more before the next time.
 *
 * Putting back costs the whole grid, so it is only done once enough new
 * masks have been made to pay for it. A screen whose picture alone
 * overflows the atlas or the table would otherwise put the grid back for
 * every new glyph; there it is cleared instead, and the cells it strands
 * are mended by the next restart that does put back. */
static void atlas_restart(Screen *s)
{
    Gl *gl = &s->gl;
    /* The base's cells are put back only in the base's face: a layer's,
     * swapped in, is made again whole by lgrid_build, which sees the
     * restart and goes round again. */
    int i, cells = s->cols * s->rows, rebuild = !s->in_face && gl->made >= cells / 8;
    glyphs_clear(s);
    s->restarts++;
    gl->made = 0;
    if (!rebuild)
        return;
    gl->rebuilding = 1;
    mask_of(s, ' ');        /* first, so it is there to fall back on */
    for (i = 0; i < cells; i++) {
        uint32_t cp = gl->shows[i];
        cell_mask(s, i, mask_of(s, cp & ~TAIL), (cp & TAIL) != 0);
    }
    gl->rebuilding = 0;
    gl->made = 0;
}

/* The mask for a code point, in the atlas, made if it is new. Blocks and
 * shades are made from the tables, everything else by the font; a space
 * is an empty mask. What the software path would have filled, pixel for
 * pixel, is what goes into the block masks. Returns the first slot and
 * how many cells wide the mask is. */
static const Glyph *mask_of(Screen *s, uint32_t cp)
{
    /* What a cell shows when there is no room for its own mask: nothing.
     * Only a GPU too small for the picture on it ever needs this. */
    static Glyph none = {.cp = ' ', .cells = 1};
    Gl *gl = &s->gl;
    int fresh, idx, w, h, cells, skip, total = gl->slot_cols * gl->slot_rows;
    int ghost = (cp & FAINT) != 0, tri = cp >= TRIANGLE && cp < TRIANGLE + 4;
    int arc = cp >= ARC && cp < ARC + 4;
    uint32_t at;
    Glyph *g;
    char u8[5] = {0};
    double t;
    /* A film is one character repeated across every cell, so the entry
     * just found is almost always the one wanted next. */
    if (gl->last && gl->last->used && gl->last->cp == cp)
        return gl->last;
    g = glyph_find(s, cp, 0, 0, &at);
    if (g) {
        gl->last = g;
        return g;
    }
    if (!ghost)
        utf8_encode(cp, u8);
    idx = ghost ? -1 : shape_index(cp);
    t = ghost ? 0 : shade_of(cp);
    cells = (idx >= 0 || t > 0 || cp == ' ' || ghost || tri || arc) ? 1 : glyph_cells(s, u8);
    /* Both halves of a wide mask in one row, side by side. */
    skip = cells == 2 && total && gl->next_slot % gl->slot_cols == gl->slot_cols - 1;
    if (s->nglyphs >= CACHE || gl->next_slot + skip + cells > total) {
        const Glyph *blank;
        if (!gl->rebuilding && total) {
            atlas_restart(s);
            skip = cells == 2 && gl->next_slot % gl->slot_cols == gl->slot_cols - 1;
        }
        if (gl->rebuilding || s->nglyphs >= CACHE
            || gl->next_slot + skip + cells > total) {
            blank = glyph_find(s, ' ', 0, 0, &at);
            return blank ? blank : &none;
        }
    }
    g = glyph_slot(s, cp, 0, 0, &fresh);
    gl->last = g;
    gl->made++;
    g->cells = cells;
    gl->next_slot += skip;
    g->slot = gl->next_slot;
    gl->next_slot += g->cells;
    g->u = (uint16_t)(((g->slot % gl->slot_cols) * s->cw * 65535L + gl->atlas_w / 2) / gl->atlas_w);
    g->v = (uint16_t)(((g->slot / gl->slot_cols) * s->ch * 65535L + gl->atlas_h / 2) / gl->atlas_h);

    w = s->cw * g->cells;
    h = s->ch;
    memset(gl->mask, 0, (size_t)w * h);
    if (ghost) {
        /* Three levels, for the shader: unlit dots half way, lit ones
         * full, and nothing between them. */
        SDL_Rect d[8];
        int pass, j, x, y, n;
        for (pass = 0; pass < 2; pass++) {
            n = dots_px(pass ? (int)(cp & 0xff) : 0xff, s->cw, s->ch, d);
            for (j = 0; j < n; j++)
                for (y = d[j].y; y < d[j].y + d[j].h && y < h; y++)
                    for (x = d[j].x; x < d[j].x + d[j].w && x < w; x++)
                        gl->mask[y * w + x] = pass ? 255 : 128;
        }
    } else if (tri) {
        int x, y;
        for (y = 0; y < h; y++)
            for (x = 0; x < w; x++)
                if (tri_in((int)(cp - TRIANGLE), x, y, s->cw, s->ch))
                    gl->mask[y * w + x] = 255;
    } else if (arc) {
        int x, y;
        for (y = 0; y < h; y++)
            for (x = 0; x < w; x++)
                if (arc_in((int)(cp - ARC), x, y, s->cw, s->ch, s->size))
                    gl->mask[y * w + x] = 255;
    } else if (idx >= 0) {
        int j, x, y;
        for (j = 0; j < s->pn[idx]; j++) {
            const SDL_Rect *r = &s->px[idx][j];
            for (y = r->y; y < r->y + r->h && y < h; y++)
                for (x = r->x; x < r->x + r->w && x < w; x++)
                    gl->mask[y * w + x] = 255;
        }
    } else if (t > 0) {
        memset(gl->mask, (int)(t * 255 + 0.5), (size_t)w * h);
    } else if (cp != ' ') {
        SDL_Surface *surf = TTF_RenderUTF8_Blended(s->font, u8, WHITE);
        if (surf) {
            const SDL_PixelFormat *f = surf->format;
            int x, y;
            SDL_LockSurface(surf);
            for (y = 0; y + s->ty < h && y < surf->h; y++) {
                const uint8_t *row = (const uint8_t *)surf->pixels + y * surf->pitch;
                for (x = 0; x + s->tx < w && x < surf->w; x++) {
                    uint32_t px = 0;
                    memcpy(&px, row + x * f->BytesPerPixel, f->BytesPerPixel);
                    gl->mask[(y + s->ty) * w + x + s->tx] =
                        (uint8_t)((px & f->Amask) >> f->Ashift);
                }
            }
            SDL_UnlockSurface(surf);
            SDL_FreeSurface(surf);
        }
    }
    gl_use(s);
    glBindTexture(GL_TEXTURE_2D, gl->atlas);
    glTexSubImage2D(GL_TEXTURE_2D, 0, (g->slot % gl->slot_cols) * s->cw,
                    (g->slot / gl->slot_cols) * s->ch, w, h,
                    GL_ALPHA, GL_UNSIGNED_BYTE, gl->mask);
    return g;
}

/* The rendered form of one character for the software path, from the
 * cache or freshly made in its colours. */
static Glyph *glyph_get(Screen *s, uint32_t cp, Rgb fg, Rgb bg)
{
    int fresh;
    Glyph *g = glyph_slot(s, cp, pack(fg), pack(bg), &fresh);
    char u8[5];
    SDL_Surface *surf;
    SDL_Color f = {fg.r, fg.g, fg.b, 255}, b = {bg.r, bg.g, bg.b, 255};
    if (!fresh)
        return g;
    utf8_encode(cp, u8);
    g->cells = glyph_cells(s, u8);
    surf = TTF_RenderUTF8_Shaded(s->font, u8, f, b);
    if (!surf)
        return g;
    g->w = surf->w;
    g->h = surf->h;
    /* Converted to the glass's own format now so that every blit is a
     * straight copy rather than a palette lookup per pixel. */
    g->surf = SDL_ConvertSurface(surf, s->glass->format, 0);
    if (g->surf)
        SDL_SetSurfaceBlendMode(g->surf, SDL_BLENDMODE_NONE);
    SDL_FreeSurface(surf);
    return g;
}

/* Draw a rendered glyph at a pixel position, clipped to the cells it
 * owns so that a face with generous side bearings cannot leave ink in
 * a neighbouring cell that is painted later. */
static void glyph_draw(Screen *s, Glyph *g, int x, int y, int cells)
{
    SDL_Rect src, dst;
    if (!g->surf)
        return;
    src.x = 0;
    src.y = 0;
    src.w = g->w < cells * s->cw ? g->w : cells * s->cw;
    src.h = g->h < s->ch ? g->h : s->ch;
    dst.x = x + s->tx;
    dst.y = y + s->ty;
    dst.w = src.w;
    dst.h = src.h;
    SDL_BlitSurface(g->surf, &src, s->target, &dst);
}

/* A block character, as the rectangles it is made of, `n` cells across.
 * A shape that tiles is one fill the width of the run; one that does not
 * is the same rectangle once per cell, still in one call. */
static void shape_run(Screen *s, int idx, Rgb ink, int x, int y, int n)
{
    SDL_Rect rects[2 * RUN_CHUNK];
    int j, k, m = 0;
    for (j = 0; j < s->pn[idx]; j++) {
        SDL_Rect r = s->px[idx][j];
        r.y += y;
        if (s->ptile[idx]) {
            r.x += x;
            r.w *= n;
            rects[m++] = r;
        } else {
            for (k = 0; k < n; k++) {
                if (m == (int)(sizeof rects / sizeof rects[0])) {
                    fill(s, rects, m, ink);
                    m = 0;
                }
                rects[m] = r;
                rects[m].x += x + k * s->cw;
                m++;
            }
        }
    }
    fill(s, rects, m, ink);
}

/* A faint braille cell in software: the wall's black, every dot the
 * pattern leaves unlit in the cell's ground, and the lit ones in ink. */
static void ghost_draw(Screen *s, int bits, Rgb ink, Rgb ground, int x, int y)
{
    SDL_Rect cell = {x, y, s->cw, s->ch}, d[8];
    int n, j;
    fill(s, &cell, 1, GROUND);
    n = dots_px(0xff & ~bits, s->cw, s->ch, d);
    for (j = 0; j < n; j++) {
        d[j].x += x;
        d[j].y += y;
    }
    fill(s, d, n, ground);
    n = dots_px(bits, s->cw, s->ch, d);
    for (j = 0; j < n; j++) {
        d[j].x += x;
        d[j].y += y;
    }
    fill(s, d, n, ink);
}

/* A triangle in software, in ink over the ground already laid. */
static void tri_draw(Screen *s, int which, Rgb ink, int x, int y)
{
    SDL_Rect r[512];
    int n = tri_px(which, s->cw, s->ch, r, 512), j;
    for (j = 0; j < n; j++) {
        r[j].x += x;
        r[j].y += y;
    }
    fill(s, r, n, ink);
}

/* An arc in software, in ink over the ground already laid. */
static void arc_draw(Screen *s, int which, Rgb ink, int x, int y)
{
    SDL_Rect r[512];
    int n = arc_px(which, s->cw, s->ch, s->size, r, 512), j;
    for (j = 0; j < n; j++) {
        r[j].x += x;
        r[j].y += y;
    }
    fill(s, r, n, ink);
}

/* Draw `k` code points at the cursor on the GPU path: each becomes a
 * cell of the grid, and the cursor moves past them. */
static void paint_cells(Screen *s, const uint32_t *cps, int k)
{
    Rgb ink = s->pen.ink, ground = s->pen.ground;
    int i, x = s->x;
    for (i = 0; i < k && x < s->cols; i++) {
        int cells = 1;
        if (!s->nodraw) {
            const Glyph *m = mask_of(s, cps[i]);
            cells = m->cells;
            if (cells == 2 && x + 1 >= s->cols)
                cells = 1;
            cell_put(s, x, s->y, ink, ground, m, 0);
            if (cells == 2)
                cell_put(s, x + 1, s->y, ink, ground, m, 1);
        }
        x += cells;
    }
    s->drew = 1;
    s->x = x < s->cols ? x : s->cols;
}

/* Draw `k` code points at the cursor, and move the cursor past them. */
static void paint_run(Screen *s, const uint32_t *cps, int k)
{
    Rgb ink = s->pen.ink, ground = s->pen.ground;
    int cw = s->cw, ch = s->ch;
    int x0, y0, room, shown, cells, i, idx, same;
    SDL_Rect span;

    /* Off the grid nothing is drawn, and the cursor goes no further than
     * the right-hand edge: nothing past it could be drawn anyway, and a
     * line that never ends would otherwise count its way past INT_MAX. */
    if (s->x < 0 || s->y < 0 || s->y >= s->rows || s->x >= s->cols) {
        if (s->x >= s->cols)
            s->x = s->cols;
        return;
    }
    if (s->gpu) {
        paint_cells(s, cps, k);
        return;
    }
    x0 = s->ox + s->x * cw;
    y0 = s->oy + s->y * ch;
    room = s->cols - s->x;
    shown = k < room ? k : room;

    idx = shape_index(cps[0]);
    same = idx >= 0;
    for (i = 1; same && i < shown; i++)
        if (cps[i] != cps[0])
            same = 0;

    /* The run's ground, in one fill; the ink goes into it. A run of full
     * blocks covers every pixel of its ground, so that one fill is
     * skipped. */
    span.x = x0;
    span.y = y0;
    span.w = shown * cw;
    span.h = ch;
    if (!(same && cps[0] == 0x2588))
        fill(s, &span, 1, ground);

    if (same) {
        /* A run of one block character is the same rectangles drawn the
         * width of the run rather than once per cell, which is the
         * difference between a fill a cell and a fill a run. */
        shape_run(s, idx, ink, x0, y0, shown);
        cells = shown;
        goto done;
    }

    cells = 0;
    for (i = 0; i < shown && cells < room; i++) {
        uint32_t cp = cps[i];
        int x = x0 + cells * cw;
        double t;
        Glyph *g;
        int w;
        if (cp == ' ') {
            cells++;        /* the ground is already there */
            continue;
        }
        if (cp & FAINT) {
            ghost_draw(s, (int)(cp & 0xff), ink, ground, x, y0);
            cells++;
            continue;
        }
        if (cp >= TRIANGLE && cp < TRIANGLE + 4) {
            tri_draw(s, (int)(cp - TRIANGLE), ink, x, y0);
            cells++;
            continue;
        }
        if (cp >= ARC && cp < ARC + 4) {
            arc_draw(s, (int)(cp - ARC), ink, x, y0);
            cells++;
            continue;
        }
        idx = shape_index(cp);
        if (idx >= 0) {
            shape_run(s, idx, ink, x, y0, 1);
            cells++;
            continue;
        }
        t = shade_of(cp);
        if (t > 0) {
            Rgb mix;
            SDL_Rect r = {x, y0, cw, ch};
            mix.r = (uint8_t)(int)(ground.r + (ink.r - ground.r) * t);
            mix.g = (uint8_t)(int)(ground.g + (ink.g - ground.g) * t);
            mix.b = (uint8_t)(int)(ground.b + (ink.b - ground.b) * t);
            fill(s, &r, 1, mix);
            cells++;
            continue;
        }
        g = glyph_get(s, cp, ink, ground);
        w = g->cells;
        if (w > room - cells)
            w = room - cells;
        if (g->cells == 2 && w == 2) {
            /* A wide glyph's second cell lies past the ground laid for
             * the run, so it gets its own. */
            SDL_Rect r = {x + cw, y0, cw, ch};
            fill(s, &r, 1, ground);
        }
        if (!s->nodraw)
            glyph_draw(s, g, x, y0, w);
        cells += w;
    }
    if (cells > shown)
        span.w = (cells < room ? cells : room) * cw;

done:
    dirty_add(s, &span);
    s->drew = 1;
    s->x = cells < room ? s->x + cells : s->cols;
}

/* Make the atlas big enough for this cell: the grid's worth of masks
 * twice over, because a wide glyph takes two slots and may leave one
 * empty to keep its halves in a row, so no picture needs more. That is
 * what lets atlas_restart put back everything on screen. The sides are
 * powers of two, grown one at a time from ATLAS_MIN, and stop at what the
 * GPU allows; a GPU that cannot hold the picture keeps what it can. */
static void atlas_fit(Screen *s)
{
    Gl *g = &s->gl;
    long need = 2L * s->cols * s->rows;
    int w = ATLAS_MIN, h = ATLAS_MIN, max = g->max_texture;
    while ((long)(w / s->cw) * (h / s->ch) < need && (w < max || h < max)) {
        if ((w <= h && w < max) || h >= max)
            w *= 2;
        else
            h *= 2;
    }
    if (w > max)
        w = max;
    if (h > max)
        h = max;
    if (w != g->atlas_w || h != g->atlas_h) {
        gl_use(s);
        glBindTexture(GL_TEXTURE_2D, g->atlas);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_ALPHA, w, h, 0, GL_ALPHA, GL_UNSIGNED_BYTE, NULL);
        g->atlas_w = w;
        g->atlas_h = h;
    }
}

/* Take a new font size and report the grid it produces. The cell is the
 * font's own advance and line height rather than anything chosen here,
 * so the grid matches what the glyphs actually need and a caller is told
 * the truth rather than a request. A size the font cannot be opened at
 * leaves the grid standing, and saying so is the reply. */
/* A cell as the wall draws it: with BITWALL_EVEN_CELLS, two pixels in
 * whole numbers across and four down, so a half, a quadrant and every
 * braille dot fall on whole pixels at even spacing, the font's glyph in
 * the middle of it. What a font is chosen by as well as what it draws. */
static void cell_round(int *cw, int *ch)
{
    if (getenv("BITWALL_EVEN_CELLS")) {
        *cw = (*cw + 1) / 2 * 2;
        *ch = (*ch + 3) / 4 * 4;
    }
}

/* Room for a face's tables, on the screen or in a Face. Zero, or -1 if
 * memory ran out. */
static int face_tables(SDL_Rect (**px)[MAXRECTS], uint8_t **pn, uint8_t **ptile,
                       Glyph **glyphs)
{
    *px = calloc(NSHAPES, sizeof **px);
    *pn = calloc(NSHAPES, 1);
    *ptile = calloc(NSHAPES, 1);
    *glyphs = calloc(CACHE_SLOTS, sizeof **glyphs);
    return *px && *pn && *ptile && *glyphs ? 0 : -1;
}

static int face_alloc(Screen *s)
{
    return face_tables(&s->px, &s->pn, &s->ptile, &s->glyphs);
}

#define FSWAP(a, b) do { __typeof__(a) t_ = (a); (a) = (b); (b) = t_; } while (0)

/* Trade the screen's face for `f`: called once to put a layer's in, and
 * again to put the base's back. */
static void face_swap(Screen *s, Face *f)
{
    Gl *g = &s->gl;
    FSWAP(s->font, f->font);
    FSWAP(s->size, f->size);
    FSWAP(s->cw, f->cw);
    FSWAP(s->ch, f->ch);
    FSWAP(s->tx, f->tx);
    FSWAP(s->ty, f->ty);
    FSWAP(s->cols, f->cols);
    FSWAP(s->rows, f->rows);
    FSWAP(s->ox, f->ox);
    FSWAP(s->oy, f->oy);
    FSWAP(s->px, f->px);
    FSWAP(s->pn, f->pn);
    FSWAP(s->ptile, f->ptile);
    FSWAP(s->glyphs, f->glyphs);
    FSWAP(s->nglyphs, f->nglyphs);
    FSWAP(s->restarts, f->restarts);
    FSWAP(g->atlas, f->atlas);
    FSWAP(g->atlas_w, f->atlas_w);
    FSWAP(g->atlas_h, f->atlas_h);
    FSWAP(g->slot_cols, f->slot_cols);
    FSWAP(g->slot_rows, f->slot_rows);
    FSWAP(g->next_slot, f->next_slot);
    FSWAP(g->points, f->points);
    FSWAP(g->made, f->made);
    FSWAP(g->rebuilding, f->rebuilding);
    FSWAP(g->slot_u, f->slot_u);
    FSWAP(g->mask, f->mask);
    FSWAP(g->last, f->last);
    s->in_face = !s->in_face;
}

static int face_load(Screen *s, int size);

static void face_free(Screen *s, Face *f)
{
    if (!f)
        return;
    if (f->font)
        TTF_CloseFont(f->font);
    if (f->atlas && s->gpu) {
        gl_use(s);
        glDeleteTextures(1, &f->atlas);
    }
    free(f->px);
    free(f->pn);
    free(f->ptile);
    free(f->glyphs);
    free(f->mask);
    free(f);
}

/* A face of its own for a cells layer, at `size` on this glass: a GPU one,
 * with an atlas of its own. Null if the font will not open there, or
 * memory or the GPU ran out. */
static Face *face_new(Screen *s, int size)
{
    Face *f = calloc(1, sizeof *f);
    if (!f)
        return NULL;
    if (face_tables(&f->px, &f->pn, &f->ptile, &f->glyphs) < 0) {
        face_free(s, f);
        return NULL;
    }
    gl_use(s);
    glGenTextures(1, &f->atlas);
    glBindTexture(GL_TEXTURE_2D, f->atlas);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    face_swap(s, f);
    if (face_load(s, size) < 0) {
        face_swap(s, f);
        glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
        face_free(s, f);
        return NULL;
    }
    face_swap(s, f);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    return f;
}

/* The font at `size` and everything made from it, into the face the
 * screen holds now: the cell and the grid it makes on this glass, the
 * shapes drawn in that cell, and on the GPU the atlas fitted to it and
 * emptied. The base's face is the screen's own; a cells layer with a size
 * of its own has one swapped in while it is made and drawn (face_swap).
 * Zero, or -1 where the font will not open at that size, which leaves the
 * face as it was. */
static int face_load(Screen *s, int size)
{
    TTF_Font *f;
    int w = 0, h = 0, i, j;
    if (size < 4)
        size = 4;
    if (size > 400)
        size = 400;
    if (!s->glyphs && face_alloc(s) < 0) {
        fprintf(stderr, "bitwall-paint: out of memory\n");
        exit(1);
    }
    f = TTF_OpenFont(s->font_path, size);
    if (!f)
        return -1;
    if (s->font)
        TTF_CloseFont(s->font);
    s->font = f;
    s->size = size;
    TTF_SizeUTF8(f, "M", &w, &h);
    s->cw = w > 1 ? w : 1;
    h = TTF_FontLineSkip(f);
    s->ch = h > 1 ? h : 1;
    {
        int ew = s->cw, eh = s->ch;
        cell_round(&ew, &eh);
        s->tx = (ew - s->cw) / 2;
        s->ty = (eh - s->ch) / 2;
        s->cw = ew;
        s->ch = eh;
    }
    s->cols = s->width / s->cw > 1 ? s->width / s->cw : 1;
    s->rows = s->height / s->ch > 1 ? s->height / s->ch : 1;
    s->ox = s->width > s->cols * s->cw ? (s->width - s->cols * s->cw) / 2 : 0;
    s->oy = s->height > s->rows * s->ch ? (s->height - s->rows * s->ch) / 2 : 0;
    for (i = 0; i < NSHAPES; i++) {
        int n = 0, tile;
        if (i < NBOX)
            n = box_px(BOX[i], s->cw, s->ch, size, s->px[i]);
        else if (i >= SHAPE_DOTS && i < SHAPE_MORE)
            n = dots_px(i - SHAPE_DOTS, s->cw, s->ch, s->px[i]);
        else
            for (n = 0; n < SHAPES[i].n; n++)
                frac_px(&SHAPES[i].r[n], s->cw, s->ch, &s->px[i][n]);
        /* A shape may be drawn as one wide fill across a run only where
         * every rectangle in it spans the cell from edge to edge. A left
         * half or a quadrant widened that way becomes one long bar
         * instead of several short ones, so those are drawn per cell. */
        tile = n > 0;
        for (j = 0; j < n; j++)
            if (s->px[i][j].x != 0 || s->px[i][j].w != s->cw)
                tile = 0;
        s->pn[i] = (uint8_t)n;
        s->ptile[i] = (uint8_t)tile;
    }
    glyphs_clear(s);
    if (s->gpu) {
        Gl *g = &s->gl;
        int cells = s->cols * s->rows, big = s->cw > s->ch ? s->cw : s->ch;
        double screen = (double)s->width * s->height / 1e6;
        double over = (double)big * big / ((double)s->cw * s->ch);
        double quads = screen * MS_PER_MEGAPIXEL + cells * 4 * MS_PER_KVERTEX / 1e3;
        double points = screen * over * MS_PER_MEGAPIXEL + cells * MS_PER_KVERTEX / 1e3;
        /* Four corners cost vertices; one point costs the fragments
         * outside the cell that are drawn and thrown away. Which is
         * cheaper depends on how many cells there are and how far from
         * square they are, so it is worked out rather than fixed. */
        g->points = big <= g->max_point && points < quads;
        atlas_fit(s);
        g->slot_cols = g->atlas_w / s->cw;
        g->slot_rows = g->atlas_h / s->ch;
        g->slot_u = (uint16_t)((s->cw * 65535L + g->atlas_w / 2) / g->atlas_w);
        if (g->slot_cols < 2 || g->slot_rows < 1) {
            /* A cell too big for the atlas draws nothing: there are no
             * slots, every cell points at the corner, and the corner is
             * made empty so that nothing is what it shows. */
            uint8_t *zero = calloc((size_t)g->atlas_w * g->atlas_h, 1);
            g->slot_cols = g->slot_rows = 0;
            if (zero) {
                gl_use(s);
                glBindTexture(GL_TEXTURE_2D, g->atlas);
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, g->atlas_w, g->atlas_h,
                                GL_ALPHA, GL_UNSIGNED_BYTE, zero);
                free(zero);
            }
        }
        free(g->mask);
        g->mask = calloc((size_t)s->cw * 2 * s->ch, 1);
        if (!g->mask) {
            fprintf(stderr, "bitwall-paint: out of memory\n");
            exit(1);
        }
    }
    return 0;
}

/* The largest font whose grid on this glass holds `cols` by `rows`, or
 * zero where none does. */
static int grid_font(Screen *s, int cols, int rows)
{
    int lo = 4, hi = 400, best = 0, i;
    for (i = 0; i < 12 && lo <= hi; i++) {
        int mid = (lo + hi) / 2, cw = 0, ch = 0, tw = 0;
        TTF_Font *f = TTF_OpenFont(s->font_path, mid);
        if (!f)
            break;
        TTF_SizeUTF8(f, "M", &cw, &tw);
        ch = TTF_FontLineSkip(f);
        TTF_CloseFont(f);
        if (cw < 1 || ch < 1)
            break;
        cell_round(&cw, &ch);
        if (s->width / cw >= cols && s->height / ch >= rows) {
            best = mid;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return best;
}

static void layer_area(struct Wall *w, int z, int repaint);

/* A layer that asked a grid has exactly that grid, in the font that holds
 * it, centred on the glass by its own cell as the base's is by its. Called
 * with the layer's face swapped in. */
static void face_grid(Screen *s, int cols, int rows)
{
    if (cols < 1 || rows < 1 || cols > s->cols || rows > s->rows)
        return;
    s->cols = cols;
    s->rows = rows;
    s->ox = (s->width - cols * s->cw) / 2;
    s->oy = (s->height - rows * s->ch) / 2;
}

/* A layer's own face made again for the glass as it is now, keeping what
 * the layer asked: its font, or its grid with the font that fits it. */
static void layer_refit(Screen *s, LGrid *g)
{
    int size = g->face->size;
    if (g->own == 2) {
        int best = grid_font(s, g->want_cols, g->want_rows);
        if (best)
            size = best;
    }
    face_swap(s, g->face);
    face_load(s, size);
    if (g->own == 2)
        face_grid(s, g->want_cols, g->want_rows);
    face_swap(s, g->face);
    glBindTexture(GL_TEXTURE_2D, s->gl.atlas);
    g->fw = s->width;
    g->fh = s->height;
}

static int set_font(Screen *s, int size)
{
    int i, cols = s->cols, rows = s->rows;
    uint32_t *kept = NULL;
    uint8_t *kept_rgb = NULL;
    /* What every cell shows and in what colours, kept across the change:
     * a font that leaves the grid the same size leaves every cell meaning
     * what it did, so it is drawn again in the new face rather than wiped
     * and waited for. Only on the GPU path, where the cells are kept as
     * cells, and only for a sender's picture; the status page draws its
     * own. */
    if (s->gpu && s->gl.shows && s->gl.verts && s->wall && wall_holds_picture(s->wall)) {
        size_t n = (size_t)cols * rows;
        kept = malloc(n * sizeof *kept);
        kept_rgb = malloc(n * 6);
        if (kept && kept_rgb) {
            for (i = 0; i < cols * rows; i++) {
                const Vert *v = &s->gl.verts[s->gl.points ? i : i * 4];
                kept[i] = s->gl.shows[i];
                memcpy(kept_rgb + i * 6, v->ink, 3);
                memcpy(kept_rgb + i * 6 + 3, v->ground, 3);
            }
        } else {
            free(kept);
            free(kept_rgb);
            kept = NULL;
            kept_rgb = NULL;
        }
    }
    if (face_load(s, size) < 0) {
        free(kept);
        free(kept_rgb);
        if (s->font)
            announce(s);
        return -1;
    }
    if (s->gpu) {
        Gl *g = &s->gl;
        int cells = s->cols * s->rows;
        free(g->verts);
        free(g->shows);
        g->verts = calloc((size_t)cells * 4, sizeof(Vert));
        g->shows = calloc((size_t)cells, sizeof *g->shows);
        if (!g->verts || !g->mask || !g->shows) {
            fprintf(stderr, "bitwall-paint: out of memory\n");
            exit(1);
        }
        /* A cells layer is the wall's grid again: kept where its size is
         * the same, else wiped, and its sender told repaint with the base. */
        for (i = 0; i < LAYERS; i++) {
            LGrid *lg = s->cells[i];
            if (!lg)
                continue;
            if (lg->face && lg->fw == s->width && lg->fh == s->height)
                continue;       /* a grid of its own, on the glass it was made for */
            if (lg->face)
                layer_refit(s, lg);
            if (lgrid_fit(s, lg) < 0) {
                fprintf(stderr, "bitwall-paint: out of memory\n");
                exit(1);
            }
            if (lg->face && s->wall)
                layer_area(s->wall, i, 1);
        }
    }
    if (kept && s->cols == cols && s->rows == rows) {
        int x, y;
        mask_of(s, ' ');
        for (y = 0; y < rows; y++)
            for (x = 0; x < cols; x++) {
                int k = y * cols + x;
                const uint8_t *c = kept_rgb + k * 6;
                Rgb ink = {c[0], c[1], c[2]}, ground = {c[3], c[4], c[5]};
                cell_put(s, x, y, ink, ground, mask_of(s, kept[k] & ~TAIL),
                         (kept[k] & TAIL) != 0);
            }
        s->dirty_all = 1;
        s->drew = 1;
        announce(s);
        if (pics_moved(s) && s->wall)
            wall_above_repaint(s->wall);
    } else {
        int moved = pics_moved(s);
        clear(s);
        announce(s);
        if (moved && s->wall)
            wall_above_repaint(s->wall);
        /* The picture is wiped: a new size, or the same grid on the
         * software path, where the cells are pixels and cannot be drawn
         * again. Every session is told repaint after the area. */
        if (s->wall)
            wall_repaint(s->wall);
    }
    free(kept);
    free(kept_rgb);
    return 0;
}

/* -------------------------------------------------------------- reading */

/* One code point from the front of `p`, and how many bytes it took. A
 * malformed or truncated sequence is the replacement character, one byte
 * long, so bad input costs a cell rather than a frame. */
static size_t utf8_decode(const uint8_t *p, size_t n, uint32_t *out)
{
    uint8_t b = p[0];
    uint32_t cp;
    size_t need, i;
    if (b < 0x80) {
        *out = b;
        return 1;
    }
    if ((b & 0xE0) == 0xC0) {
        need = 1;
        cp = b & 0x1F;
    } else if ((b & 0xF0) == 0xE0) {
        need = 2;
        cp = b & 0x0F;
    } else if ((b & 0xF8) == 0xF0) {
        need = 3;
        cp = b & 0x07;
    } else {
        *out = 0xFFFD;
        return 1;
    }
    if (need >= n) {
        *out = 0xFFFD;
        return 1;
    }
    for (i = 1; i <= need; i++) {
        if ((p[i] & 0xC0) != 0x80) {
            *out = 0xFFFD;
            return 1;
        }
        cp = cp << 6 | (p[i] & 0x3F);
    }
    if (cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF))
        cp = 0xFFFD;
    *out = cp;
    return need + 1;
}

/* How many bytes at the end of `p` begin a character that has not all
 * arrived yet. Those wait in the buffer for the next read: a read ends
 * wherever the pipe filled, and a cell lost there is lost for good,
 * because a sender only sends a cell when it changes. */
static size_t utf8_incomplete(const uint8_t *p, size_t n)
{
    size_t k;
    for (k = 1; k <= 3 && k <= n; k++) {
        uint8_t b = p[n - k];
        size_t need;
        if ((b & 0xC0) == 0x80)
            continue;       /* a continuation; the lead byte is further back */
        need = b >= 0xF0 ? 4 : b >= 0xE0 ? 3 : b >= 0xC0 ? 2 : 1;
        return need > k ? k : 0;
    }
    return 0;
}

/* Paint a stretch of text, as runs of code points at the cursor. Control
 * characters are not cells and are stepped over. */
static void draw_text(Screen *s, const uint8_t *p, size_t n)
{
    uint32_t cps[RUN_CHUNK];
    int k = 0;
    size_t i = 0;
    while (i < n) {
        uint32_t cp;
        i += utf8_decode(p + i, n - i, &cp);
        if (cp < 0x20 || (cp >= 0x7F && cp < 0xA0))
            continue;
        if (s->pen.faint && cp >= BRAILLE && cp < BRAILLE + 0x100)
            cp |= FAINT;
        cps[k++] = cp;
        if (k == RUN_CHUNK) {
            paint_run(s, cps, k);
            k = 0;
        }
    }
    if (k)
        paint_run(s, cps, k);
}

/* One control sequence: the parameters, then the letter that says what it
 * is. Everything a wall sends is CUP or SGR; anything else is skipped
 * rather than refused, because a sender is allowed to be tidier than we
 * are and a wall should not go dark over a cursor-hide. */
static void csi(Screen *s, const uint8_t *p, size_t n, uint8_t fin)
{
    int v[MAX_PARAMS];
    int nv = 0, cur = 0, private = 0;
    size_t i;
    for (i = 0; i < n; i++) {
        uint8_t c = p[i];
        if (c >= '0' && c <= '9') {
            if (cur < 1000000)
                cur = cur * 10 + (c - '0');
        } else if (c == ';') {
            if (nv < MAX_PARAMS)
                v[nv++] = cur;
            cur = 0;
        } else {
            private = 1;    /* a private-mode or intermediate byte */
        }
    }
    if (n > 0 && nv < MAX_PARAMS)
        v[nv++] = cur;

    switch (fin) {
    case 'm':
        if (nv == 0 || (nv == 1 && v[0] == 0)) {
            /* A bare reset ends a frame. It is the one mark a sender
             * guarantees, and a frame larger than a pipe arrives in
             * pieces, so presenting on every read would show a band
             * sweeping down the wall. Presenting here shows whole
             * frames. */
            pen_reset(&s->pen);
            present(s);
        } else if (!private) {
            pen_take(&s->pen, v, nv);
        }
        break;
    case 'H':
    case 'f':
        s->y = (nv >= 1 && v[0] > 0 ? v[0] : 1) - 1;
        s->x = (nv >= 2 && v[1] > 0 ? v[1] : 1) - 1;
        break;
    case 'J':
        clear(s);
        break;
    default:
        break;
    }
}

/* One command string. Only ours; the words after the mark go to whoever
 * owns the conversation, and anything else is somebody else's business
 * and is let by. */
static void apc(Screen *s, const uint8_t *p, size_t n)
{
    char text[MAX_APC + 1], whole[MAX_APC + 1];
    char *w[MAX_PARAMS], *save = NULL, *tok;
    int nw = 0;
    if (n >= 1 && p[0] == 'G') {
        kitty_apc(s, p + 1, n - 1);
        return;
    }
    if (n > MAX_APC)
        return;
    memcpy(text, p, n);
    text[n] = 0;
    /* Kept as written, because a value is the rest of the line and the
     * words below have had their separators taken out. */
    memcpy(whole, text, n + 1);
    for (tok = strtok_r(text, " \t\r\n", &save); tok && nw < MAX_PARAMS;
         tok = strtok_r(NULL, " \t\r\n", &save))
        w[nw++] = tok;
    if (nw < 2 || strcmp(w[0], "bitwall") != 0)
        return;
    if (s->wall) {
        /* On the base's own `in`: these are the base session's commands,
         * as a layer at z=0. The rest of the conversation is per session
         * and handled where each session's bytes are read. */
        base_command(s->wall, w + 1, nw - 1, rest_after(whole, 1));
        return;
    }
    /* Alone on stdin there is no conversation, but a font change and a
     * blank are still things a stream may ask of the glass. */
    if (nw >= 3) {
        char *end;
        long v = strtol(w[2], &end, 10);
        if (end == w[2])
            return;
        if (strcmp(w[1], "blank") == 0)
            blank(s, v != 0);
        else if (strcmp(w[1], "font") == 0)
            set_font(s, (int)v);
    }
}

/* Paint everything complete in the buffer, presenting at each frame's
 * end, and keep whatever is still arriving for the next read. */
static void feed(Screen *s)
{
    uint8_t *b = s->buf;
    size_t len = s->len, p = 0;
    s->cut = 0;
    while (p < len) {
        uint8_t c;
        if (s->kitty.skip) {
            /* the rest of a kitty string too long to hold, up to its end */
            size_t q = p;
            while (q + 1 < len && !(b[q] == 0x1b && b[q + 1] == '\\'))
                q++;
            if (q + 1 < len) {
                s->kitty.skip = 0;
                p = q + 2;
                continue;
            }
            p = b[len - 1] == 0x1b ? len - 1 : len;
            break;
        }
        if (b[p] != 0x1b) {
            const uint8_t *esc = memchr(b + p, 0x1b, len - p);
            size_t end = esc ? (size_t)(esc - b)
                             : len - utf8_incomplete(b + p, len - p);
            if (end > p)
                draw_text(s, b + p, end - p);
            p = end;
            if (!esc)
                break;
            continue;
        }
        if (p + 1 >= len)
            break;          /* an escape whose kind has not arrived */
        c = b[p + 1];
        if (c == '[') {
            size_t q = p + 2;
            while (q < len && b[q] >= 0x20 && b[q] <= 0x3f)
                q++;
            if (q >= len) {
                if (len - p > MAX_CSI) {
                    p += 2;     /* never going to end; drop the introducer */
                    continue;
                }
                break;
            }
            if (b[q] < 0x40 || b[q] > 0x7e) {
                p = q;          /* malformed; whatever follows is read afresh */
                continue;
            }
            csi(s, b + p + 2, q - (p + 2), b[q]);
            p = q + 1;
            continue;
        }
        if (c == '_' || c == ']' || c == 'P' || c == '^' || c == 'X') {
            /* A string: ours if it is APC, and skipped whole otherwise,
             * because none of its bytes are cells. */
            size_t q = p + 2, after = 0;
            int found = 0;
            while (q < len) {
                if (b[q] == 0x1b) {
                    if (q + 1 >= len)
                        break;
                    if (b[q + 1] == '\\') {
                        found = 1;
                        after = q + 2;
                        break;
                    }
                } else if (b[q] == 0x07 && c == ']') {
                    found = 1;
                    after = q + 1;
                    break;
                }
                q++;
            }
            if (!found) {
                if (c == '_' && p + 2 < len && b[p + 2] == 'G') {
                    /* kitty's pictures are longer than a command */
                    if (len - p > KITTY_STRING) {
                        kitty_overlong(s);
                        p = q;
                        continue;
                    }
                } else if (len - p > MAX_APC) {
                    p += 2;
                    continue;
                }
                break;
            }
            if (c == '_' && b[p + 2] == 'G' && q - (p + 2) > KITTY_STRING) {
                /* Whole by the time its end is read, as one past the limit
                 * is when its last piece crosses it: refused all the same,
                 * as one still arriving is. */
                Kitty *kt = &s->kitty;
                kitty_say(s, kt->pending ? kt->pk.i : 0, "too big");
                if (kt->pending)
                    kt->refused = 1;
                p = after;
                continue;
            }
            if (c == '_') {
                apc(s, b + p + 2, q - (p + 2));
                if (s->cut) {
                    /* It closed its session, or was displaced: nothing
                     * after that is anybody's to paint. */
                    s->cut = 0;
                    s->len = 0;
                    return;
                }
            }
            p = after;
            continue;
        }
        p += 2;             /* a two-byte escape, which a wall never needs */
    }
    if (p < len)
        memmove(b, b + p, len - p);
    s->len = len - p;
}

/* --------------------------------------------------------------- inbox */

/* What the reader thread has taken off the pipe and the painter has not
 * yet parsed: a queue of chunks with a ceiling on the bytes in it. */
typedef struct Chunk {
    struct Chunk *next;
    size_t len, off;
    uint8_t data[];
} Chunk;

typedef struct {
    int fd;
    SDL_Thread *reader;
    SDL_mutex *mu;          /* the bell's: one lock for every inbox */
    SDL_cond *more;         /* the bell's: rung when anything arrives */
    SDL_cond *room;         /* the bell's: rung when a reader may have space */
    Chunk *head, *tail;
    size_t bytes;
    size_t max;             /* the ceiling on `bytes`; INBOX_MAX when zero */
    int eof;
    int stop;               /* the reader is to go, and hand nothing more over */
    int wake[2];            /* rung when `stop` is set, so the reader sees it at once */
    int held;               /* the main thread's: what waits here is not read
                             * yet, so it is no reason to wake */
} Inbox;

/* One read from the pipe, as a chunk. Returns the bytes read and the
 * chunk in `*out`; zero at end of file, and -1 with errno set on an error,
 * ENOMEM included. The read asks for plenty, but the chunk keeps only
 * what came: a wall sending small diffs would otherwise hold a quarter
 * of a megabyte for every few bytes queued, and the inbox's ceiling is
 * counted in bytes read, not bytes held. */
static ssize_t chunk_read(int fd, Chunk **out)
{
    Chunk *c = malloc(sizeof *c + READ_SIZE), *fit;
    ssize_t n;
    *out = NULL;
    if (!c) {
        errno = ENOMEM;
        return -1;
    }
    n = ipc_read(fd, c->data, READ_SIZE);
    if (n <= 0) {
        free(c);
        return n;
    }
    if (n < READ_SIZE && (fit = realloc(c, sizeof *c + (size_t)n)))
        c = fit;
    c->len = (size_t)n;
    c->off = 0;
    c->next = NULL;
    *out = c;
    return n;
}

/* The reader: wait for the pipe to have something, then take it, and hand
 * it over the moment it lands, however long the painter is busy painting.
 * It waits in select rather than a bare blocking read so that it hears
 * the waker `inbox_stop` rings and, on a fifo no writer has opened yet,
 * does not read a spurious end of file: a wait of one comes back with
 * nothing, not a readable zero, so the writer's absence is a wait rather
 * than a close. When bytes flow the wait returns at once, so nothing is
 * added per frame. */
static int inbox_reader(void *arg)
{
    Inbox *in = arg;
    for (;;) {
        Chunk *c;
        ssize_t n;
        int sr;
        if (in->stop)
            break;
        sr = wait_readable(in->fd, in->wake[0], 1000);
        if (sr == 0)
            continue;           /* idle, or rung: `stop` is looked at again */
        if (sr < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        n = chunk_read(in->fd, &c);
        if (n < 0 && errno == ENOMEM) {
            SDL_Delay(10);
            continue;
        }
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            break;              /* end of file: the writer has gone */
        SDL_LockMutex(in->mu);
        /* The painter is that far behind. A read bigger than the ceiling
         * waits only for the inbox to empty, so it is never shut out. */
        while (in->bytes && in->bytes + c->len > (in->max ? in->max : INBOX_MAX) && !in->stop)
            SDL_CondWait(in->room, in->mu);
        if (in->stop) {
            SDL_UnlockMutex(in->mu);
            free(c);
            return 0;
        }
        if (in->tail)
            in->tail->next = c;
        else
            in->head = c;
        in->tail = c;
        in->bytes += c->len;
        SDL_CondBroadcast(in->more);
        SDL_UnlockMutex(in->mu);
    }
    SDL_LockMutex(in->mu);
    in->eof = 1;
    SDL_CondBroadcast(in->more);
    SDL_UnlockMutex(in->mu);
    return 0;
}

/* What every reader rings when it hands something over. One lock and one
 * condition for the whole process, so that one loop can sleep until any
 * wall has something, rather than on one wall while another's bytes
 * wait. */
static struct {
    SDL_mutex *mu;
    SDL_cond *more;
    SDL_cond *room;         /* shared by every reader, so it is broadcast */
} bell;

/* The bell, made the first time anything rings or waits on it: a loop
 * with nothing to read yet sleeps on it as one with inboxes does. */
static int bell_open(void)
{
    if (!bell.mu) {
        bell.mu = SDL_CreateMutex();
        bell.more = SDL_CreateCond();
        bell.room = SDL_CreateCond();
    }
    return bell.mu && bell.more && bell.room ? 0 : -1;
}

/* Start reading `fd` into an inbox, empty and afresh whatever it held
 * before. -1, and no reader, when there is nothing to read. */
static int inbox_start(Inbox *in, int fd)
{
    in->head = in->tail = NULL;
    in->bytes = 0;
    in->eof = in->stop = 0;
    in->reader = NULL;
    in->wake[0] = in->wake[1] = -1;
    in->fd = fd;
    if (fd < 0)
        return -1;
    bell_open();
    in->fd = fd;
    in->mu = bell.mu;
    in->more = bell.more;
    in->room = bell.room;
    if (!in->mu || !in->more || !in->room || waker_open(in->wake) < 0)
        return -1;
    in->reader = SDL_CreateThread(inbox_reader, "reader", in);
    if (!in->reader)
        waker_close(in->wake);
    return in->reader ? 0 : -1;
}

/* Stop the reader and wait for it to go, so nothing is left writing into an
 * inbox about to be reused or freed. The waker ends its wait at once, so the
 * loop that calls this is held for no longer than the reader takes to see
 * `stop`. The fd is the reader's alone, and nothing reads it after, so
 * this closes it and leaves `fd` set to -1 for the caller. */
static void inbox_stop(Inbox *in)
{
    if (!in->reader)
        return;
    SDL_LockMutex(in->mu);
    in->stop = 1;
    SDL_CondBroadcast(in->room);
    SDL_UnlockMutex(in->mu);
    waker_ring(in->wake);
    SDL_WaitThread(in->reader, NULL);
    in->reader = NULL;
    waker_close(in->wake);
    while (in->head) {
        Chunk *c = in->head;
        in->head = c->next;
        free(c);
    }
    in->tail = NULL;
    in->bytes = 0;
    if (in->fd >= 0) {
        ipc_close(in->fd);
        in->fd = -1;
    }
}

/* Up to `max` bytes into `dst`, waiting at most `wait` ms for the first
 * of them, and not at all when `wait` is zero. Returns how many, zero
 * when nothing came, and -1 once the pipe has closed and everything from
 * it has been taken. The bell is shared, so a wait can end for another
 * inbox's bytes and come back with none. */
static ssize_t inbox_take(Inbox *in, uint8_t *dst, size_t max, int wait)
{
    size_t got = 0;
    SDL_LockMutex(in->mu);
    if (wait > 0 && !in->head && !in->eof)
        SDL_CondWaitTimeout(in->more, in->mu, (Uint32)wait);
    while (in->head && got < max) {
        Chunk *c = in->head;
        size_t n = c->len - c->off;
        if (n > max - got)
            n = max - got;
        memcpy(dst + got, c->data + c->off, n);
        got += n;
        c->off += n;
        in->bytes -= n;
        if (c->off == c->len) {
            in->head = c->next;
            if (!in->head)
                in->tail = NULL;
            free(c);
        }
    }
    if (got)
        SDL_CondBroadcast(in->room);
    if (!got && in->eof && !in->head) {
        SDL_UnlockMutex(in->mu);
        return -1;
    }
    SDL_UnlockMutex(in->mu);
    return (ssize_t)got;
}

/* The worker pool, which rings the same bell. */
#include "pool.c"

/* Sleep until any of these inboxes has something, or has closed, or a
 * job of the pool's is done, or `wait` ms have gone. A null inbox is one
 * already finished with, and a held one is not looked at. */
static void inbox_wait(Inbox **in, int n, int wait)
{
    int i, ready = 0;
    if (bell_open() < 0) {
        SDL_Delay((Uint32)wait);
        return;
    }
    SDL_LockMutex(bell.mu);
    ready = pool_ready();
    for (i = 0; i < n && !ready; i++)
        ready = in[i] && !in[i]->held && (in[i]->head || in[i]->eof);
    if (!ready)
        SDL_CondWaitTimeout(bell.more, bell.mu, (Uint32)wait);
    SDL_UnlockMutex(bell.mu);
}

/* ---------------------------------------------------- a stream on `data` */

/* What a video layer's `data` may carry. A stream is played out by the
 * wall: QOI is decoded here, and HEVC, H.264 and MJPEG by ffmpeg, a child
 * process a stream, using the board's own decoder where it has one. The
 * stream's codec name is also ffmpeg's name for the stream's format. */
enum { CODEC_QOI = 1, CODEC_HEVC, CODEC_H264, CODEC_MJPEG, NCODECS };
static const char *const CODEC_NAME[NCODECS] = {"", "qoi", "hevc", "h264", "mjpeg"};

/* What this wall takes on `data`, found when it starts: QOI always; HEVC
 * where the board decodes it in hardware, or where it is not a Pi and
 * ffmpeg decodes it at all; H.264 where ffmpeg decodes it, save on a Pi
 * with neither decoder, which is too slow for a film; MJPEG only where
 * the board decodes it and ffmpeg can hand it there, since a JPEG a frame
 * decoded in software costs more than the film it would be. `hw` is
 * whether the board's decoder does it, says how ffmpeg is asked, and is
 * said in `caps` as `hw=`. */
static struct {
    int ok[NCODECS];
    int hw[NCODECS];
    char ffmpeg[256];
} codecs = {{0, 1, 0, 0, 0}, {0, 0, 0, 0, 0}, "ffmpeg"};

/* Decoded frames held ready ahead of the one on the glass: enough that a
 * frame is there when it is due, few enough that a film's ahead is a few
 * megabytes. */
#define STREAM_AHEAD 3

/* The codecs this wall takes, as `caps` says them in `codecs=`; or, with
 * `hw`, those of them the board decodes, as `hw=` says them. */
static void codecs_said(char *out, size_t max, int hw)
{
    int c;
    size_t n = 0;
    out[0] = 0;
    for (c = 1; c < NCODECS && n < max; c++)
        if (codecs.ok[c] && (!hw || codecs.hw[c]))
            n += (size_t)snprintf(out + n, max - n, "%s%s", n ? "," : "", CODEC_NAME[c]);
}

static int codec_of(const char *name)
{
    int c;
    for (c = 1; c < NCODECS; c++)
        if (!strcmp(name, CODEC_NAME[c]))
            return c;
    return 0;
}

/* The wall's clock, as `start` is said in: Unix time in ms. */
static double unix_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

/* One decoded frame on its way to the glass, RGBA or YUV 4:2:0 as three
 * planes, numbered from the start of its stream; or the mark that a
 * stream has ended, `end` 1 when it ended well and 2 when it did not
 * decode. Kept and used again, since a film's frames are megabytes. */
typedef struct DFrame {
    struct DFrame *next;
    int end;
    int yuv, w, h;
    long index;
    double decode;          /* ms spent decoding it here */
    size_t cap;
    uint8_t *px;
} DFrame;

/* A video layer's `data`: a reader that takes each stream as a writer
 * opens the fifo and ends it when the last writer closes it, a decoder
 * where the codec wants one, and the frames it has made, waiting their
 * turn. Everything shared is under the bell's lock, whose `more` is rung
 * when a frame is ready and `room` when one is taken. */
typedef struct DataStream {
    int codec;
    double fps, start;      /* 0 when not said */
    char path[700];
    SDL_Thread *feeder, *drain;
    int stop;
    int wake[2];            /* rung when `stop` is set, so the feeder sees it at once */
    DFrame *head, *tail, *spare;
    int ready, nspare;
    double bytes;           /* read off `data`, every stream */
    Child child;            /* the stream's decoder, or 0 */
    char threads[12];       /* its -threads */
    int to_child, from_child;
    long made;              /* frames made in the stream under way */
    int bad;                /* it has not decoded */
    int over;               /* its end is on the queue already */
    /* the loop's own */
    double taken;           /* bytes already counted into the session's */
    double t0;              /* Unix ms of this stream's frame 0; 0 until known */
    int streams;            /* streams ended */
} DataStream;

/* A frame with room for `size` bytes, a spare one where there is one. */
static DFrame *dframe_get(DataStream *d, size_t size)
{
    DFrame *f;
    SDL_LockMutex(bell.mu);
    f = d->spare;
    if (f) {
        d->spare = f->next;
        d->nspare--;
    }
    SDL_UnlockMutex(bell.mu);
    if (!f && !(f = calloc(1, sizeof *f)))
        return NULL;
    if (f->cap < size) {
        uint8_t *px = realloc(f->px, size ? size : 1);
        if (!px) {
            free(f->px);
            free(f);
            return NULL;
        }
        f->px = px;
        f->cap = size;
    }
    f->next = NULL;
    f->end = f->yuv = 0;
    f->decode = 0;
    return f;
}

static void dframe_free(DFrame *f)
{
    if (f) {
        free(f->px);
        free(f);
    }
}

/* A frame done with, kept for the next unless enough are kept already.
 * Under the bell's lock. */
static void dframe_keep(DataStream *d, DFrame *f)
{
    if (d->nspare >= STREAM_AHEAD + 2) {
        dframe_free(f);
        return;
    }
    f->next = d->spare;
    d->spare = f;
    d->nspare++;
}

/* A frame, or an end, onto the queue, waiting for room while as many
 * frames as are held ahead are there already. -1, and the frame kept,
 * when the stream is being stopped. */
static int dframe_put(DataStream *d, DFrame *f)
{
    SDL_LockMutex(bell.mu);
    while (!f->end && d->ready >= STREAM_AHEAD && !d->stop)
        SDL_CondWait(bell.room, bell.mu);
    if (d->stop) {
        dframe_keep(d, f);
        SDL_UnlockMutex(bell.mu);
        return -1;
    }
    if (d->tail)
        d->tail->next = f;
    else
        d->head = f;
    d->tail = f;
    if (!f->end)
        d->ready++;
    SDL_CondBroadcast(bell.more);
    SDL_UnlockMutex(bell.mu);
    return 0;
}

/* The next frame off the queue. Under the bell's lock. */
static DFrame *dframe_pop(DataStream *d)
{
    DFrame *f = d->head;
    if (!f)
        return NULL;
    d->head = f->next;
    if (!d->head)
        d->tail = NULL;
    if (!f->end)
        d->ready--;
    f->next = NULL;
    SDL_CondBroadcast(bell.room);
    return f;
}

/* The stream under way ended, once: its end put on the queue, 2 when it
 * did not decode, so a stream found not to decode is said at once and
 * the rest of it, until its writer leaves, is let go. */
static void stream_over(DataStream *d, int bad)
{
    DFrame *end;
    SDL_LockMutex(bell.mu);
    if (bad)
        d->bad = 1;
    if (d->over || d->stop) {
        SDL_UnlockMutex(bell.mu);
        return;
    }
    d->over = 1;
    SDL_UnlockMutex(bell.mu);
    if (!(end = dframe_get(d, 0)))
        return;
    end->end = d->bad ? 2 : 1;
    dframe_put(d, end);
}

/* How many bytes the QOI image at the front of `p` takes, found by walking
 * its ops to the last pixel, so a stream of them needs nothing between
 * them: 0 while it is not all there yet, -1 when it is not QOI. */
static long qoi_length(const uint8_t *p, size_t n)
{
    unsigned long w, h, px, total;
    size_t i = 14;
    static const uint8_t pad[8] = {0, 0, 0, 0, 0, 0, 0, 1};
    if (n < 4)
        return memcmp(p, "qoif", n) ? -1 : 0;
    if (memcmp(p, "qoif", 4))
        return -1;
    if (n < 14)
        return 0;
    w = (unsigned long)p[4] << 24 | (unsigned long)p[5] << 16 | (unsigned long)p[6] << 8 | p[7];
    h = (unsigned long)p[8] << 24 | (unsigned long)p[9] << 16 | (unsigned long)p[10] << 8 | p[11];
    if (!w || !h || w > FRAME_SIDE || h > FRAME_SIDE || p[12] < 3 || p[12] > 4)
        return -1;
    total = w * h;
    for (px = 0; px < total;) {
        uint8_t b;
        if (i >= n)
            return 0;
        b = p[i];
        if (b == 0xFE)
            i += 4;
        else if (b == 0xFF)
            i += 5;
        else if ((b & 0xC0) == 0x80)
            i += 2;
        else
            i += 1;
        px += (b & 0xC0) == 0xC0 && b < 0xFE ? (unsigned long)(b & 0x3F) + 1 : 1;
    }
    if (i + 8 > n)
        return 0;
    return memcmp(p + i, pad, 8) ? -1 : (long)(i + 8);
}

/* All of `n` bytes read, or fewer at the end of the pipe. */
static size_t read_full(int fd, uint8_t *p, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t k = read(fd, p + got, n - got);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0)
            break;
        got += (size_t)k;
    }
    return got;
}

/* One line of the decoder's, without its newline: -1 at the end. */
static int read_line(int fd, char *out, size_t max)
{
    size_t n = 0;
    for (;;) {
        char c;
        ssize_t k = read(fd, &c, 1);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0)
            return -1;
        if (c == '\n')
            break;
        if (n + 1 < max)
            out[n++] = c;
    }
    out[n] = 0;
    return (int)n;
}

/* The decoder's frames, read as they come: YUV4MPEG2, a header saying the
 * size and then each frame a line and its three planes. */
static int data_drain(void *arg)
{
    DataStream *d = arg;
    char line[512], *tok, *save = NULL;
    int w = 0, h = 0, ok = 1;
    if (read_line(d->from_child, line, sizeof line) < 0 || strncmp(line, "YUV4MPEG2", 9)) {
        stream_over(d, 1);      /* the decoder made nothing of it */
        return 0;
    }
    for (tok = strtok_r(line, " ", &save); tok; tok = strtok_r(NULL, " ", &save)) {
        if (tok[0] == 'W')
            w = atoi(tok + 1);
        else if (tok[0] == 'H')
            h = atoi(tok + 1);
        else if (tok[0] == 'C' && strncmp(tok + 1, "420", 3))
            ok = 0;
    }
    if (!ok || w < 1 || h < 1 || w > FRAME_SIDE || h > FRAME_SIDE) {
        stream_over(d, 1);
        return 0;
    }
    for (;;) {
        size_t size = (size_t)w * h + 2 * (size_t)((w + 1) / 2) * ((h + 1) / 2);
        DFrame *f;
        if (read_line(d->from_child, line, sizeof line) < 0 || strncmp(line, "FRAME", 5))
            break;
        if (!(f = dframe_get(d, size)))
            break;
        if (read_full(d->from_child, f->px, size) != size) {
            SDL_LockMutex(bell.mu);
            dframe_keep(d, f);
            SDL_UnlockMutex(bell.mu);
            break;
        }
        f->yuv = 1;
        f->w = w;
        f->h = h;
        f->index = d->made++;
        if (dframe_put(d, f) < 0)
            break;
    }
    return 0;
}

/* The decoder for a stream: ffmpeg reading the stream on its stdin and
 * writing YUV4MPEG2 on its stdout, every frame once, as fast as it is
 * read. Its command line, into `argv` with room for 40, and how many
 * words it is. */
static int decoder_argv(const DataStream *d, const char **argv)
{
    int n = 0;
    argv[n++] = codecs.ffmpeg;
    argv[n++] = "-hide_banner";
    argv[n++] = "-loglevel";
    argv[n++] = "error";
    argv[n++] = "-nostats";
    n += decoder_hw(d->codec, argv + n);
    /* As many threads as the knob says for the way it is decoded: ffmpeg's
     * own decoder, or one through a hwaccel, runs a frame on each, and a
     * board's V4L2 decoder takes the option and has no use for it. */
    argv[n++] = "-threads";
    argv[n++] = d->threads;
    /* Frames from the first bytes, not after a second of them has been
     * read to guess what the stream is: the codec is said, and the
     * stream's own headers say the rest. */
    argv[n++] = "-probesize";
    argv[n++] = "32";
    argv[n++] = "-analyzeduration";
    argv[n++] = "0";
    argv[n++] = "-f";
    argv[n++] = CODEC_NAME[d->codec];
    argv[n++] = "-i";
    argv[n++] = "pipe:0";
    argv[n++] = "-an";
    argv[n++] = "-f";
    argv[n++] = "yuv4mpegpipe";
    argv[n++] = "-pix_fmt";
    argv[n++] = "yuv420p";
    argv[n++] = "-fps_mode";
    argv[n++] = "passthrough";
    argv[n++] = "pipe:1";
    argv[n] = NULL;
    return n;
}

/* The decoder started. It gets stdin, stdout and the painter's stderr,
 * where anything it says goes to the painter's log, and no other
 * descriptor of the painter's. 0 when it could not be started. */
static Child decoder_start(DataStream *d)
{
    const char *argv[40];
    knobs_settle();
    snprintf(d->threads, sizeof d->threads, "%d",
             codecs.hw[d->codec] ? decoder_threads : decoder_soft);
    decoder_argv(d, argv);
    return child_start(argv, decoder_nice, &d->to_child, &d->from_child);
}

/* A writer has opened `data`: a stream begins, numbered from 0. */
static void stream_begin(DataStream *d)
{
    Child pid = 0;
    d->made = 0;
    d->bad = d->over = 0;
    d->to_child = d->from_child = -1;
    if (d->codec != CODEC_QOI) {
        pid = decoder_start(d);
        if (pid > 0 && !(d->drain = SDL_CreateThread(data_drain, "drain", d))) {
            child_kill(pid);
            child_wait(pid, NULL);
            pid = 0;
        }
        SDL_LockMutex(bell.mu);
        d->child = pid;
        SDL_UnlockMutex(bell.mu);
        if (!pid)
            stream_over(d, 1);
    }
}

/* Bytes of the stream under way: QOI gathered into whole images and
 * decoded here, anything else handed to its decoder. */
static void stream_bytes(DataStream *d, const uint8_t *p, size_t n, uint8_t **img,
                         size_t *len, size_t *cap)
{
    if (d->bad)
        return;             /* the rest of a stream that does not decode */
    if (d->codec != CODEC_QOI) {
        size_t done = 0;
        while (done < n && d->to_child >= 0) {
            ssize_t k = write(d->to_child, p + done, n - done);
            if (k < 0 && errno == EINTR)
                continue;
            if (k <= 0)
                break;      /* the decoder has gone; its end says why */
            done += (size_t)k;
        }
        return;
    }
    if (*len + n > *cap) {
        size_t c = *cap ? *cap : READ_SIZE;
        uint8_t *nb;
        while (c < *len + n)
            c *= 2;
        if (!(nb = realloc(*img, c))) {
            d->bad = 1;
            return;
        }
        *img = nb;
        *cap = c;
    }
    memcpy(*img + *len, p, n);
    *len += n;
    while (*len) {
        long whole = qoi_length(*img, *len);
        qoi_desc desc;
        double t0 = now_ms();
        uint8_t *px;
        DFrame *f;
        if (whole == 0)
            break;
        px = whole > 0 ? qoi_decode(*img, (int)whole, &desc, 4) : NULL;
        if (!px) {
            *len = 0;
            stream_over(d, 1);
            return;
        }
        f = dframe_get(d, (size_t)desc.width * desc.height * 4);
        if (f) {
            memcpy(f->px, px, (size_t)desc.width * desc.height * 4);
            f->w = (int)desc.width;
            f->h = (int)desc.height;
            f->index = d->made++;
            f->decode = now_ms() - t0;
        }
        free(px);
        memmove(*img, *img + whole, *len - (size_t)whole);
        *len -= (size_t)whole;
        if (!f || dframe_put(d, f) < 0)
            return;
    }
}

/* The last writer has closed `data`: what is decoded by now is played
 * out, and then the stream's end. A decoder is let finish, and has not
 * decoded when it failed or made nothing of what it was given. */
static void stream_finish(DataStream *d, size_t left)
{
    if (d->codec == CODEC_QOI && left)
        d->bad = 1;         /* an image cut short */
    if (d->to_child >= 0) {
        close(d->to_child);
        d->to_child = -1;
    }
    if (d->drain) {
        SDL_WaitThread(d->drain, NULL);
        d->drain = NULL;
    }
    if (d->from_child >= 0) {
        close(d->from_child);
        d->from_child = -1;
    }
    if (d->child > 0) {
        int code = 0, exited = child_wait(d->child, &code);
        SDL_LockMutex(bell.mu);
        d->child = 0;
        SDL_UnlockMutex(bell.mu);
        if (!exited || code || !d->made) {
            if (!d->stop)
                fprintf(stderr, "bitwall-paint: %s: the %s decoder %s %d after %ld frames\n",
                        d->path, CODEC_NAME[d->codec],
                        exited ? "exited" : "was killed by signal", code, d->made);
            d->bad = 1;
        }
    }
    stream_over(d, d->bad);
}

/* `data`, read a stream at a time for as long as the session lasts. It
 * is opened without waiting for a writer and waited on with a short
 * select, so that `stop` is seen; a fifo nobody has opened to write is
 * not readable, so a stream begins with its first bytes and ends when a
 * read after them says the last writer has gone. It is then opened
 * afresh, for the next writer. */
static int data_feeder(void *arg)
{
    DataStream *d = arg;
    uint8_t *buf = malloc(READ_SIZE), *img = NULL;
    size_t len = 0, cap = 0;
    while (buf && !d->stop) {
        int fd = ipc_data_open(d->path), live = 0;
        if (fd < 0)
            break;
        while (!d->stop) {
            ssize_t n;
            int sr = wait_readable(fd, d->wake[0], 1000);
            if (sr < 0 && errno != EINTR)
                break;
            if (sr <= 0)
                continue;
            n = ipc_read(fd, buf, READ_SIZE);
            if (n < 0 && (errno == EAGAIN || errno == EINTR))
                continue;
            if (n <= 0) {
                if (!live)
                    wait_readable(-1, d->wake[0], 50);  /* no writer yet; open it afresh */
                break;
            }
            if (!live) {
                live = 1;
                len = 0;
                stream_begin(d);
            }
            SDL_LockMutex(bell.mu);
            d->bytes += (double)n;
            SDL_UnlockMutex(bell.mu);
            stream_bytes(d, buf, (size_t)n, &img, &len, &cap);
        }
        ipc_close(fd);
        if (live)
            stream_finish(d, len);
    }
    free(buf);
    free(img);
    return 0;
}

/* A stream on `data` set going: the fifo made, and its reader started. */
static DataStream *stream_start(const char *dir, int codec, double fps, double start)
{
    DataStream *d = calloc(1, sizeof *d);
    if (!d)
        return NULL;
    d->codec = codec;
    d->fps = fps;
    d->start = start;
    d->to_child = d->from_child = -1;
    snprintf(d->path, sizeof d->path, "%s/data", dir);
    if (ipc_data_make(d->path) < 0) {
        free(d);
        return NULL;
    }
    if (waker_open(d->wake) < 0) {
        free(d);
        return NULL;
    }
    if (!(d->feeder = SDL_CreateThread(data_feeder, "data", d))) {
        waker_close(d->wake);
        free(d);
        return NULL;
    }
    return d;
}

/* The stream stopped and let go: its decoder killed, its reader waited
 * for, and every frame freed. */
static void stream_stop(DataStream *d)
{
    DFrame *f;
    if (!d)
        return;
    SDL_LockMutex(bell.mu);
    d->stop = 1;
    SDL_CondBroadcast(bell.room);
    if (d->child > 0)
        child_kill(d->child);
    SDL_UnlockMutex(bell.mu);
    waker_ring(d->wake);
    SDL_WaitThread(d->feeder, NULL);
    waker_close(d->wake);
    while ((f = d->head)) {
        d->head = f->next;
        dframe_free(f);
    }
    while ((f = d->spare)) {
        d->spare = f->next;
        dframe_free(f);
    }
    free(d);
}

/* --------------------------------------------------------- walls, doors */

/* A wall is a door and the sessions opened through it. The door only takes
 * `open` requests; every answer comes back on the caller's own session,
 * two fifos in `s/<name>/` that the caller writes its frames and commands
 * into and reads what the wall says on. Layer 0 is the base, the sender;
 * a controller sends commands and hears answers. Each session is one
 * ordered stream of its own. */

/* The code points drawn as geometry that no font is asked about, said in
 * `screen` so a sender knows it can use them whatever the face has. */
#define DRAWN "U+1CD00-1CDE5,U+1CEA0-1CEAF,U+1FB00-1FB3B,U+1FB70-1FB8B,U+1FBE4-1FBE7"

/* How long a caller's session is kept once it has ended, for a reader that
 * has not yet taken the `closed` line off its `out`. */
#define KEEP_S 10.0
/* A session nobody writes to within this of `opened` is removed. */
#define UNWRITTEN 10.0

/* What a session's `out` holds before a picture or a mirror frame in it is
 * refused: the frame, and the lines said after it. */
#define OUT_QUEUE (8 << 20)

/* The role a session asked for at the door. */
enum { ROLE_LAYER, ROLE_CONTROLLER };

/* Where a session is in its life. */
typedef enum { S_OPENING, S_OPEN, S_REFUSED, S_CLOSED } SState;

/* What a line waiting in a session's `out` is, which decides what happens
 * to it when the reader falls behind: a `keep` is never dropped, a `state`
 * line is merged into the latest of its kind, an `event` is dropped and
 * counted, and a `blob` (a picture or a mirror frame) waits its turn. A
 * `note` is where lines were dropped, said as `dropped <n>`. */
enum { OUT_KEEP, OUT_STATE, OUT_EVENT, OUT_BLOB, OUT_NOTE };

typedef struct OutItem {
    struct OutItem *next;
    int kind;
    char key[24];           /* which state a `state` line carries */
    size_t off, len;        /* bytes written so far, and in all */
    struct Job *job;        /* a picture's place, held until the pool has
                             * made it: nothing behind it is written */
    unsigned long drops;    /* a note: how many lines went here */
    char data[];
} OutItem;

/* One thing a session has said about itself. Per session: the base's
 * describes the wall, a controller's is its own. */
typedef struct {
    char key[META_KEY];
    char value[META_VALUE];
    int hidden;
    double set;
} Meta;

struct Wall;

/* A mirror a session has asked for: of what, how often, and what its
 * reader already has, which is what the next frame is worked out against. */
typedef struct {
    char target[33];            /* "glass", or a session's name; empty when off */
    double every, due;
    int w, h;                   /* the glass: the size asked, zero for its own */
    int quality;                /* the glass: 0 for QOI, else JPEG at this */
    int mcols, mrows;           /* a cells layer: the grid as the reader has it */
    uint32_t *mshows;
    uint8_t *mcolours;
    uint32_t sum;               /* the glass: the last look sent, summed */
    int sent;
} Mirror;

/* One session as the wall holds it: its fifos, its place, its clocks, its
 * out queue, and what it has asked to hear. */
typedef struct Session {
    struct Wall *w;
    char name[33];
    int role;                   /* ROLE_LAYER or ROLE_CONTROLLER */
    int is_base;                /* the layer at z=0 */
    int is_video;               /* a video layer: above the base, or the base */
    int from_top;               /* placed in the stack from the highest slot down */
    int is_cells;               /* and of those, a cells layer above the base,
                                 * read as one is and drawn from its grid */
    int z;                      /* 0 for the base, -1 for a controller */
    char type[8];               /* "cells", "video", or "" */
    char instance[65];          /* `instance=` on its open, or "" */

    char dir[640];              /* s/<name>/ */
    int in_fd;                  /* a controller's `in`; the base reads its
                                 * own through the wall's inbox */
    int out_fd;                 /* held O_RDWR so a write never blocks and
                                 * the `closed` line waits for a reader */
    SState state;
    const char *reason;         /* the `closed` reason, once ended */
    double opened_at, ended_at;
    int connected;              /* a byte has arrived on `in` */

    /* the ping clock, for a controller and for the base */
    double last_in, pinged_at;
    int awaiting, answered;
    long pings;
    double pong_ms;

    /* the out queue */
    OutItem *qhead, *qtail;
    size_t queued;
    int stuck;              /* the last flush could write nothing: the
                             * reader is not reading */

    /* what it has cost, counted as it goes: bytes read from its `in` and
     * written to its `out`, frames it put up and ms spent decoding them,
     * lines let go; and the rates over the last whole second worked out
     * from them, -1 before there is one, for its measure line and its row
     * on the overlay */
    double n_in, n_out, n_decode, n_upload;
    long n_frames;
    unsigned long n_dropped;
    double r_at, p_in, p_out, p_decode, p_upload;
    long p_frames;
    double r_in, r_out, r_fps, r_decode, r_upload;

    /* a controller's parse buffer for the commands on its `in` */
    uint8_t cbuf[MAX_APC + 8];
    size_t clen;

    /* what a controller has asked to hear */
    int follow;                 /* 0 off, 1 said, 2 in */
    double measures, measure_due;
    Sample seen;
    Mirror mir[MIRRORS];

    /* this session's metadata */
    Meta meta[META_KEYS];
    int nmeta;

    /* a video layer's `in`, drained by a thread of its own, and the frame
     * it is reading: the bytes still to come after its `frame` line,
     * whether they are to be let go, and what the line said */
    Inbox vin;
    uint8_t *vbuf;
    size_t vlen, vcap;
    unsigned long long want;
    int skip;
    int fcodec, fw, fh, fx, fy, fsw, fsh, fhold;   /* fsw 0: its own size; fhold -1: still */
    char fid[33];
    struct Job *jhead, *jtail;  /* its frames with the pool, and answers to
                                 * their lines, in the order they go out */
    int frames_out;             /* how many of those are frames */
    int say_later;              /* what is said now joins that queue */
    /* the frame it shows, as it was sent, for a picture of it */
    uint8_t *vlast;
    size_t vlastlen;
    int lcodec, lw, lh, lx, ly, lsw, lsh;

    /* a video layer's stream on `data`, as `open` asked for it, and the
     * stream itself once it is opened */
    int data_codec;             /* 0 when it has no `data` */
    double data_fps, data_start;
    int data_align;             /* where its frames leave the box spare:
                                 * -1 all below, 0 even, 1 all above */
    int data_box[4];            /* the box its frames are fitted to, x, y,
                                 * w, h on the glass; w 0 for the glass */
    struct DataStream *ds;
    struct DFrame *shown;       /* the stream's frame on the glass, for a picture */

    struct Session *next;
} Session;

typedef struct Wall {
    Screen *s;
    Inbox in;                   /* the base's `in`, drained by a thread */

    char run[512];              /* the host's run directory */
    char dir[600];              /* <run>/bitwall/<name> */
    char name[33];              /* the wall's name: 0, 1, ... or a word */
    char last_file[600];        /* the last-seen note, outside bitwall/ */

    int door_fd;                /* the door, held O_RDWR so it never ends */
    char door_rest[4096];       /* a request split across reads */
    size_t door_nrest;

    Session *sessions;          /* every session, live or ending */
    Session *base;              /* the one holding layer 0, or null */
    Session *above[LAYERS];     /* the video layer holding each layer, a video
                                 * base at 0 */
    Session *to;                /* the session whose command is being answered */
    char id[33];                /* that command's id, which wsay() puts on
                                 * what is said to `to` and to nobody else */
    char glass_to[33];          /* the controller that asked `glass`, by name,
                                 * answered once the glass has its size */
    char glass_id[33];          /* its id */
    double glass_at;            /* when it asked */

    int status_font, rate, font;   /* the status page's, the screen's, the base's */
    const char *colour;
    char glyphs[256];

    /* the base's connection, mirrored here for the measures and overlay */
    int connected, showing_status, sender_blank;
    int asked_font;
    int dozing;
    /* Set while a video base gives way to another: its picture stays on
     * the glass until the new one's first frame is drawn over it. */
    int handover;
    int absent;
    char presence_name[33];
    char presence_from[33];
    double presence_set;
    double last_data, disconnected_since, last_status;
    double pong_ms;
    char last_seen[64];

    /* what was last announced to the controllers, so it is said only on a
     * change */
    const char *display_said;
    const char *power_said;
    int watched_said;
} Wall;

/* -- forward declarations, so the pure helpers below can be in any order */
static const char *display_word(Wall *w);
static const char *power_word(Wall *w);
static int watched(Wall *w);
static int wall_holds_picture(Wall *w);
static void wall_area(Wall *w);
static void wall_repaint(Wall *w);
static void wsay(Session *s, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void wsay_line(Session *s, const char *line);
static const char *role_word(struct Session *s);
static void emit_keep(Session *s, const char *line);
static void put_item(Session *s, int kind, const char *key, const char *data, size_t len);
static void session_end(Session *s, const char *reason);
static void sender_gone(Wall *w, double now);
static const char *admit(Session *s, const char *role, char **opts, int nopts);
static const char *role_word(Session *s);
static int above_held(Wall *w, int z);
static void layer_move(Wall *w, int from, int to);
static void mirror_tick(Wall *w, Session *s, double now);
static void mirror_stop(Mirror *m);
static int digits(const char *t, size_t most);
static void video_base_frame(Wall *w);

/* === plain === */
/* A word from a command, fit to be said back: printable, and short. */
static const char *plain(const char *word, char *out, size_t max)
{
    size_t i;
    for (i = 0; word[i] && i + 1 < max; i++)
        out[i] = word[i] > 0x20 && word[i] < 0x7f ? word[i] : '?';
    out[i] = 0;
    return out;
}

/* === measure_names === */
/* The measures in a set, as a comma list, in the order they are shown. */
static void measure_names(unsigned show, char *out, size_t max)
{
    size_t at = 0;
    int i;
    out[0] = 0;
    for (i = 0; i < NMEASURES && at < max; i++)
        if (show & (1u << i))
            at += (size_t)snprintf(out + at, max - at, "%s%s", at ? "," : "", MEASURES[i]);
}

/* === measures_offered === */
/* The measures this wall can take at all. A board with no thermal zone
 * has no temperature to show, and is not offered one rather than shown a
 * dash for it for ever. */
static unsigned measures_offered(void)
{
    return access(THERMAL, R_OK) == 0 ? MEASURE_ALL : MEASURE_ALL & ~MEASURE_TEMP;
}

/* === human === */
/* A count as people read one: 812, 6.9K, 12.3M. */
static void human(double v, char *out, size_t max)
{
    if (v >= 1e6)
        snprintf(out, max, "%.1fMB", v / 1e6);
    else if (v >= 1e3)
        snprintf(out, max, "%.1fKB", v / 1e3);
    else
        snprintf(out, max, "%.0fB", v);
}

/* === sample_of === */
static Sample sample_of(Screen *s)
{
    Sample x;
    x.t = now_ms();
    x.bytes = s->bytes_in;
    x.out = s->bytes_out;
    x.parse = s->t_parse;
    x.flush = s->t_flush;
    x.swap = s->t_swap;
    x.cpu = cpu_seconds();
    x.gpu = gpu_seconds();
    x.frames = s->frames;
    x.refreshes = s->refreshes;
    x.painted = s->painted;
    return x;
}

/* === measure === */
/* One measure's value, or `-` where there is nothing to say yet. `old`
 * is the look a second ago, or null for a wall that has only just
 * started to look. */
static void measure(Wall *w, int m, const Sample *now, const Sample *old, char *out, size_t max)
{
    Screen *s = w->s;
    double dt = old ? (now->t - old->t) / 1e3 : 0;
    long painted = old ? (now->frames - now->refreshes) - (old->frames - old->refreshes) : 0;
    long based = old ? now->painted - old->painted : 0;
    long shown = old ? now->frames - old->frames : 0;
    char n[32];
    snprintf(out, max, "-");
    switch (m) {
    case 0:
        snprintf(out, max, "%d %s", s->index, s->output[0] ? s->output : "-");
        break;
    case 1:
        snprintf(out, max, "%dx%d @%d", s->cols, s->rows, s->size);
        break;
    case 2:
        if (dt > 0)
            snprintf(out, max, "%.1f", painted / dt);
        break;
    case 3:
        if (dt > 0) {
            human((now->bytes - old->bytes) / dt, n, sizeof n);
            snprintf(out, max, "%s/s", n);
        }
        break;
    case 4:
        if (dt > 0) {
            human((now->out - old->out) / dt, n, sizeof n);
            snprintf(out, max, "%s/s", n);
        }
        break;
    case 5:
        if (based > 0)
            snprintf(out, max, "%.2fms", (now->parse - old->parse) / based);
        break;
    case 6:
        if (shown > 0)
            snprintf(out, max, "%.2fms", (now->flush - old->flush) / shown);
        break;
    case 7:
        if (shown > 0)
            snprintf(out, max, "%.2fms", (now->swap - old->swap) / shown);
        break;
    case 8: {
        /* The restart count only matters once there is one: an atlas that
         * has never filled is the usual case, and says so by its absence. */
        int used = s->gpu ? s->gl.next_slot : s->nglyphs;
        int all = s->gpu ? s->gl.slot_cols * s->gl.slot_rows : CACHE;
        if (s->restarts)
            snprintf(out, max, "%d/%d, %ld restarts", used, all, s->restarts);
        else
            snprintf(out, max, "%d/%d", used, all);
        break;
    }
    case 9: {
        size_t queued = 0;
        if (w->in.mu) {
            SDL_LockMutex(w->in.mu);
            queued = w->in.bytes;
            SDL_UnlockMutex(w->in.mu);
        }
        human((double)queued, out, max);
        break;
    }
    case 10:
        snprintf(out, max, "%s %.0fs", w->connected ? "yes" : "no",
                 (now->t - w->last_data) / 1e3);
        break;
    case 11:
        if (w->pong_ms >= 0)
            snprintf(out, max, "%.0fms", w->pong_ms);
        break;
    case 12:
        snprintf(out, max, "%s", display_word(w));
        break;
    case 13:
        snprintf(out, max, "%s", power_word(w));
        break;
    case 14:
        if (dt > 0 && now->cpu >= 0 && old->cpu >= 0)
            snprintf(out, max, "%.0f%%", 100 * (now->cpu - old->cpu) / dt);
        break;
    case 15:
        /* How the wall is drawn, and on the GPU how busy it is kept where
         * the driver counts: a load is only a number worth reading once
         * it is known what is carrying it. */
        if (!s->gpu)
            snprintf(out, max, "software");
        else if (dt > 0 && now->gpu >= 0 && old->gpu >= 0)
            snprintf(out, max, "gpu %.0f%%", 100 * (now->gpu - old->gpu) / dt);
        else
            snprintf(out, max, "gpu");
        break;
    case 16: {
        FILE *f = fopen("/sys/class/thermal/thermal_zone0/temp", "r");
        long milli;
        if (f) {
            if (fscanf(f, "%ld", &milli) == 1)
                snprintf(out, max, "%.1f\xc2\xb0""C", milli / 1e3);
            fclose(f);
        }
        break;
    }
    }
}

/* === measure_line === */
/* Every measure as numbers, for a controller rather than a person: one
 * `key=value` each, in the units PROTOCOL.md gives, and `-` where there
 * is nothing to go on yet. A measure the overlay shows as two things is
 * two keys here. `old` is the look the last line was made from. */
static void measure_line(Wall *w, const Sample *now, const Sample *old, char *out, size_t max)
{
    Screen *s = w->s;
    double dt = old ? (now->t - old->t) / 1e3 : 0;
    long painted = old ? (now->frames - now->refreshes) - (old->frames - old->refreshes) : 0;
    long based = old ? now->painted - old->painted : 0;
    long shown = old ? now->frames - old->frames : 0;
    char fps[16] = "-", got[24] = "-", sent[24] = "-", frame[16] = "-", draw[16] = "-";
    char swap[16] = "-", cpu[16] = "-", gpu[16] = "-", temp[16] = "-";
    FILE *f;
    if (dt > 0) {
        snprintf(fps, sizeof fps, "%.1f", painted / dt);
        snprintf(got, sizeof got, "%.0f", (now->bytes - old->bytes) / dt);
        snprintf(sent, sizeof sent, "%.0f", (now->out - old->out) / dt);
        if (now->cpu >= 0 && old->cpu >= 0)
            snprintf(cpu, sizeof cpu, "%.0f", 100 * (now->cpu - old->cpu) / dt);
        if (s->gpu && now->gpu >= 0 && old->gpu >= 0)
            snprintf(gpu, sizeof gpu, "%.0f", 100 * (now->gpu - old->gpu) / dt);
    }
    if (based > 0)
        snprintf(frame, sizeof frame, "%.2f", (now->parse - old->parse) / based);
    if (shown > 0) {
        snprintf(draw, sizeof draw, "%.2f", (now->flush - old->flush) / shown);
        snprintf(swap, sizeof swap, "%.2f", (now->swap - old->swap) / shown);
    }
    if ((f = fopen(THERMAL, "r"))) {
        long milli;
        if (fscanf(f, "%ld", &milli) == 1)
            snprintf(temp, sizeof temp, "%.1f", milli / 1e3);
        fclose(f);
    }
    {
        int n = snprintf(out, max,
             "wall=%d connector=%s grid=%dx%d font=%d fps=%s in=%s out=%s frame=%s draw=%s "
             "swap=%s atlas=%d atlas_of=%d restarts=%ld "
             "display=%s power=%s cpu=%s render=%s",
             s->index, s->output[0] ? s->output : "-", s->cols, s->rows, s->size, fps,
             got, sent, frame, draw, swap, s->gpu ? s->gl.next_slot : s->nglyphs,
             s->gpu ? s->gl.slot_cols * s->gl.slot_rows : CACHE, s->restarts,
             display_word(w), power_word(w),
             cpu, s->gpu ? "gpu" : "software");
        /* What this wall cannot know is left out rather than said as a
         * dash: a GPU's share where there is no GPU, or a driver that keeps
         * no count of it, and a temperature on a board with no sensor. */
        if (n > 0 && (size_t)n < max && s->gpu && now->gpu >= 0)
            n += snprintf(out + n, max - (size_t)n, " gpu=%s", gpu);
        if (n > 0 && (size_t)n < max && access(THERMAL, R_OK) == 0)
            snprintf(out + n, max - (size_t)n, " temp=%s", temp);
    }
}

/* === session_rates === */
/* A session's rates, worked out once a second from its counts, so its
 * measure line and its row on the overlay say the same. */
static void session_rates(Session *x, double now)
{
    double dt = (now - x->r_at) / 1e3;
    long frames = x->n_frames - x->p_frames;
    if (dt < 1.0)
        return;
    x->r_in = (x->n_in - x->p_in) / dt;
    x->r_out = (x->n_out - x->p_out) / dt;
    x->r_fps = frames / dt;
    x->r_decode = frames > 0 ? (x->n_decode - x->p_decode) / frames : -1;
    x->r_upload = frames > 0 ? (x->n_upload - x->p_upload) / frames : -1;
    x->p_upload = x->n_upload;
    x->p_in = x->n_in;
    x->p_out = x->n_out;
    x->p_frames = x->n_frames;
    x->p_decode = x->n_decode;
    x->r_at = now;
}

/* === session_measure === */
/* One session's measures, as numbers: its place, its frames a second, the
 * bytes a second each way, lines let go since it opened, bytes waiting on
 * its `out`, seconds since its last byte, its last ping's round trip, and
 * for a video layer the ms each frame took to decode and put up. `-` where
 * there is nothing to go on yet, or for a controller, which draws no
 * frames. */
static void session_measure(Session *x, double now, char *out, size_t max)
{
    char fps[16] = "-", in[24] = "-", sent[24] = "-", pong[16] = "-", place[40] = "";
    char decode[32] = "", stream[64] = "";
    if (x->role == ROLE_LAYER) {
        snprintf(place, sizeof place, " z=%d type=%s", x->z, x->type);
        if (x->r_fps >= 0)
            snprintf(fps, sizeof fps, "%.1f", x->r_fps);
    }
    if (x->r_in >= 0) {
        snprintf(in, sizeof in, "%.0f", x->r_in);
        snprintf(sent, sizeof sent, "%.0f", x->r_out);
    }
    if (x->pong_ms >= 0)
        snprintf(pong, sizeof pong, "%.0f", x->pong_ms);
    if (x->is_video) {
        if (x->r_decode >= 0)
            snprintf(decode, sizeof decode, " decode=%.2f upload=%.2f", x->r_decode,
                     x->r_upload);
        else
            snprintf(decode, sizeof decode, " decode=- upload=-");
    }
    outputs_measure(x->w->s, x, stream, sizeof stream);
    snprintf(out, max,
             "wall=%d session=%s role=%s%s fps=%s in=%s out=%s dropped=%lu queue=%zu "
             "quiet=%.1f pong=%s%s%s",
             x->w->s->index, x->name, role_word(x), place, fps, in, sent, x->n_dropped,
             x->queued, (now - x->last_in) / 1e3, pong, decode, stream);
}

/* === session_row === */
/* One session's row on the overlay, under the wall's panel: its name, its
 * place and type, frames a second, bytes in a second, lines let go. */
static void session_row(Session *x, char *out, size_t max)
{
    char place[24], fps[16] = "-", in[24] = "-";
    if (x->role == ROLE_LAYER)
        snprintf(place, sizeof place, "%d %s", x->z, x->type);
    else
        snprintf(place, sizeof place, "controller");
    if (x->role == ROLE_LAYER && x->r_fps >= 0)
        snprintf(fps, sizeof fps, "%.0ffps", x->r_fps);
    if (x->r_in >= 0) {
        human(x->r_in, in, sizeof in);
        snprintf(in + strlen(in), sizeof in - strlen(in), "/s");
    }
    snprintf(out, max, "%-18.18s %-10s %6s %9s %lu dropped", x->name, place, fps, in,
             x->n_dropped);
}

/* === id_ok === */
/* Whether an id is one: one to thirty-two of letters, digits, _ and -. */
static int id_ok(const char *t)
{
    size_t n = strlen(t), i;
    if (n < 1 || n > 32)
        return 0;
    for (i = 0; i < n; i++)
        if (!((t[i] >= 'a' && t[i] <= 'z') || (t[i] >= 'A' && t[i] <= 'Z')
              || (t[i] >= '0' && t[i] <= '9') || t[i] == '_' || t[i] == '-'))
            return 0;
    return 1;
}

/* === grow === */
/* Room for one more of something `n` bytes long, growing by doubling. */
static int grow(char **buf, size_t *cap, size_t len, size_t n)
{
    size_t c = *cap ? *cap : 1 << 16;
    char *b;
    if (len + n <= *cap)
        return 1;
    while (c < len + n)
        c *= 2;
    if (!(b = realloc(*buf, c)))
        return 0;
    *buf = b;
    *cap = c;
    return 1;
}

/* === grid_picture === */
/* The grid as a sender would have written it to make it: each row placed
 * with a cursor move, a colour set only where it changes, and the frame
 * ended with the bare reset. So whatever can paint a stream can show a
 * picture, and one pasted into a receiver makes the same wall. It is made
 * from what the GPU path keeps for every cell, the character and its two
 * colours, which is also why only that path offers it. Null if memory
 * runs out. */
static char *grid_picture(Screen *s, size_t *len)
{
    Gl *g = &s->gl;
    int per = g->points ? 1 : 4, x, y;
    char *buf = NULL;
    size_t cap = 0, n = 0;
    int have = 0, dim = 0;
    uint8_t ink[3] = {0}, ground[3] = {0};
    for (y = 0; y < s->rows; y++) {
        if (!grow(&buf, &cap, n, 16))
            goto fail;
        n += (size_t)snprintf(buf + n, cap - n, "\033[%d;1H", y + 1);
        for (x = 0; x < s->cols; x++) {
            int i = y * s->cols + x;
            const Vert *v = &g->verts[(size_t)i * per];
            uint32_t cp = g->shows ? g->shows[i] : ' ';
            if (cp & TAIL)
                continue;       /* the wide character before it covers it */
            if (!grow(&buf, &cap, n, 48))
                goto fail;
            if (!have || memcmp(ink, v->ink, 3) != 0) {
                n += (size_t)snprintf(buf + n, cap - n, "\033[38;2;%d;%d;%dm",
                                      v->ink[0], v->ink[1], v->ink[2]);
                memcpy(ink, v->ink, 3);
            }
            if (!have || memcmp(ground, v->ground, 3) != 0) {
                n += (size_t)snprintf(buf + n, cap - n, "\033[48;2;%d;%d;%dm",
                                      v->ground[0], v->ground[1], v->ground[2]);
                memcpy(ground, v->ground, 3);
            }
            if (!!(cp & FAINT) != dim) {
                dim = !dim;
                n += (size_t)snprintf(buf + n, cap - n, dim ? "\033[2m" : "\033[22m");
            }
            cp &= ~FAINT;
            have = 1;
            n += (size_t)utf8_encode(cp && cp < 0x110000 ? cp : ' ', buf + n);
        }
    }
    if (!grow(&buf, &cap, n, 8))
        goto fail;
    memcpy(buf + n, "\033[0m", 4);
    *len = n + 4;
    return buf;
fail:
    free(buf);
    return NULL;
}

/* A cells layer's grid as its sender would have written it: each cell it
 * has drawn placed with a cursor move where the last did not leave the
 * cursor, its colours where they change, an alpha short of whole said as
 * `38;6` or `48;6`, and a ground that is none as the default. The cells it
 * has not drawn are left out, as they are transparent. Null if memory runs
 * out. */
static char *lgrid_picture(const LGrid *g, size_t *len)
{
    int x, y, cx = -1, cy = -1, have = 0, dim = 0;
    char *buf = NULL;
    size_t cap = 0, n = 0;
    uint8_t last[8] = {0};
    for (y = 0; y < g->rows; y++)
        for (x = 0; x < g->cols; x++) {
            size_t i = (size_t)y * g->cols + x;
            const uint8_t *c = g->rgba + i * 8;
            uint32_t cp = g->cp[i];
            if (!cp || (cp & TAIL))
                continue;
            if (!grow(&buf, &cap, n, 96))
                goto fail;
            if (x != cx || y != cy)
                n += (size_t)snprintf(buf + n, cap - n, "\033[%d;%dH", y + 1, x + 1);
            if (!have || memcmp(last, c, 4)) {
                if (c[3] == 255)
                    n += (size_t)snprintf(buf + n, cap - n, "\033[38;2;%d;%d;%dm",
                                          c[0], c[1], c[2]);
                else
                    n += (size_t)snprintf(buf + n, cap - n, "\033[38;6;%d;%d;%d;%dm",
                                          c[0], c[1], c[2], c[3]);
            }
            if (!have || memcmp(last + 4, c + 4, 4)) {
                if (c[7] == 0)
                    n += (size_t)snprintf(buf + n, cap - n, "\033[49m");
                else if (c[7] == 255)
                    n += (size_t)snprintf(buf + n, cap - n, "\033[48;2;%d;%d;%dm",
                                          c[4], c[5], c[6]);
                else
                    n += (size_t)snprintf(buf + n, cap - n, "\033[48;6;%d;%d;%d;%dm",
                                          c[4], c[5], c[6], c[7]);
            }
            memcpy(last, c, 8);
            if (!!(cp & FAINT) != dim) {
                dim = !dim;
                n += (size_t)snprintf(buf + n, cap - n, dim ? "\033[2m" : "\033[22m");
            }
            have = 1;
            n += (size_t)utf8_encode((cp & ~FAINT) < 0x110000 ? cp & ~FAINT : ' ', buf + n);
            cx = x + (x + 1 < g->cols && (g->cp[i + 1] & TAIL) ? 2 : 1);
            cy = y;
        }
    if (!grow(&buf, &cap, n, 8))
        goto fail;
    memcpy(buf + n, "\033[0m", 4);
    *len = n + 4;
    return buf;
fail:
    free(buf);
    return NULL;
}

/* === mirror_frame === */
/* A mirror frame for one session: the cells that differ from what its reader
 * was last sent, written as a sender would write a frame, or the whole
 * grid when the reader has nothing yet or the grid has changed size.
 * `*full` says which. Null with `*len` 0 when nothing has changed, and
 * null with `*len` 1 when memory ran out. What is sent is remembered by
 * mirror_sent() only once it is certain to reach the reader, so a frame
 * that does not go costs a frame and never a wrong picture. */
static char *mirror_frame(Screen *s, Mirror *t, size_t *len, int *full)
{
    Gl *g = &s->gl;
    int per = g->points ? 1 : 4, x, y, cx = -1, cy = -1, have = 0, dim = 0;
    char *buf = NULL;
    size_t cap = 0, n = 0;
    uint8_t ink[3] = {0}, ground[3] = {0};
    *len = 0;
    *full = !t->mshows || t->mcols != s->cols || t->mrows != s->rows;
    for (y = 0; y < s->rows; y++)
        for (x = 0; x < s->cols; x++) {
            int i = y * s->cols + x;
            const Vert *v = &g->verts[(size_t)i * per];
            uint32_t cp = g->shows ? g->shows[i] : ' ';
            if (cp & TAIL)
                continue;       /* the wide character before it covers it */
            if (!*full && t->mshows[i] == cp && !memcmp(t->mcolours + i * 6, v->ink, 3)
                && !memcmp(t->mcolours + i * 6 + 3, v->ground, 3))
                continue;
            if (!grow(&buf, &cap, n, 64))
                goto fail;
            if (x != cx || y != cy)
                n += (size_t)snprintf(buf + n, cap - n, "\033[%d;%dH", y + 1, x + 1);
            if (!have || memcmp(ink, v->ink, 3) != 0) {
                n += (size_t)snprintf(buf + n, cap - n, "\033[38;2;%d;%d;%dm",
                                      v->ink[0], v->ink[1], v->ink[2]);
                memcpy(ink, v->ink, 3);
            }
            if (!have || memcmp(ground, v->ground, 3) != 0) {
                n += (size_t)snprintf(buf + n, cap - n, "\033[48;2;%d;%d;%dm",
                                      v->ground[0], v->ground[1], v->ground[2]);
                memcpy(ground, v->ground, 3);
            }
            if (!!(cp & FAINT) != dim) {
                dim = !dim;
                n += (size_t)snprintf(buf + n, cap - n, dim ? "\033[2m" : "\033[22m");
            }
            cp &= ~FAINT;
            have = 1;
            n += (size_t)utf8_encode(cp && cp < 0x110000 ? cp : ' ', buf + n);
            /* Where the reader's cursor is now, so the next cell along
             * needs no move: two on for a wide character. */
            cy = y;
            cx = x + (x + 1 < s->cols && g->shows && (g->shows[i + 1] & TAIL) ? 2 : 1);
        }
    if (!n)
        return NULL;
    if (!grow(&buf, &cap, n, 8))
        goto fail;
    memcpy(buf + n, "\033[0m", 4);
    *len = n + 4;
    return buf;
fail:
    free(buf);
    *len = 1;
    return NULL;
}

/* === mirror_sent === */
/* Remember the grid as the session's reader now has it. Memory running out
 * leaves the session remembering nothing, so its next frame is whole. */
static void mirror_sent(Screen *s, Mirror *t)
{
    Gl *g = &s->gl;
    int per = g->points ? 1 : 4, i, cells = s->cols * s->rows;
    if (!t->mshows || t->mcols != s->cols || t->mrows != s->rows) {
        free(t->mshows);
        free(t->mcolours);
        t->mshows = malloc((size_t)cells * sizeof *t->mshows);
        t->mcolours = malloc((size_t)cells * 6);
        t->mcols = s->cols;
        t->mrows = s->rows;
        if (!t->mshows || !t->mcolours) {
            free(t->mshows);
            free(t->mcolours);
            t->mshows = NULL;
            t->mcolours = NULL;
            return;
        }
    }
    for (i = 0; i < cells; i++) {
        const Vert *v = &g->verts[(size_t)i * per];
        t->mshows[i] = g->shows ? g->shows[i] : ' ';
        memcpy(t->mcolours + i * 6, v->ink, 3);
        memcpy(t->mcolours + i * 6 + 3, v->ground, 3);
    }
}

/* === key_ok === */
/* Whether a key is one: one to thirty-two of lower-case letters, digits,
 * dot, underscore and dash. */
static int key_ok(const char *k)
{
    size_t n = strlen(k), i;
    if (n < 1 || n >= META_KEY)
        return 0;
    for (i = 0; i < n; i++)
        if (!((k[i] >= 'a' && k[i] <= 'z') || (k[i] >= '0' && k[i] <= '9')
              || k[i] == '.' || k[i] == '_' || k[i] == '-'))
            return 0;
    return 1;
}

/* === rest_after === */
/* The words of `line` after the first `skip` of them, as they were
 * written. The value of a key is the rest of the line, so that a name
 * with spaces in it needs no quoting and nothing is escaped. */
static const char *rest_after(const char *line, int skip)
{
    int i;
    for (i = 0; i < skip && *line; i++) {
        while (*line && *line != ' ' && *line != '\t')
            line++;
        while (*line == ' ' || *line == '\t')
            line++;
    }
    return line;
}

/* === first_address === */
/* The first address this machine answers on, for the status page. */
static void first_address(char *out, size_t max)
{
    struct ifaddrs *list = NULL, *a;
    snprintf(out, max, "no address");
    if (getifaddrs(&list) != 0)
        return;
    for (a = list; a; a = a->ifa_next) {
        if (a->ifa_addr && a->ifa_addr->sa_family == AF_INET
            && !(a->ifa_flags & 0x8) /* IFF_LOOPBACK */) {
            inet_ntop(AF_INET, &((struct sockaddr_in *)a->ifa_addr)->sin_addr,
                      out, (socklen_t)max);
            break;
        }
    }
    freeifaddrs(list);
}

/* === uptime_text === */
static void uptime_text(char *out, size_t max)
{
    double up = uptime_seconds();
    long d, h, m;
    d = (long)up / 86400;
    h = (long)up % 86400 / 3600;
    m = (long)up % 3600 / 60;
    if (d)
        snprintf(out, max, "%ld day%s, %ld hour%s, %ld minute%s",
                 d, d == 1 ? "" : "s", h, h == 1 ? "" : "s", m, m == 1 ? "" : "s");
    else if (h)
        snprintf(out, max, "%ld hour%s, %ld minute%s", h, h == 1 ? "" : "s",
                 m, m == 1 ? "" : "s");
    else
        snprintf(out, max, "%ld minute%s", m, m == 1 ? "" : "s");
}

/* === wall_glyphs === */
/* Spans of CSS `unicode-range`, "U+2500-257F,U+2800", added to `got`,
 * which holds `most`; returns how many it holds now. */
static int spans_add(Uint32 (*got)[2], int n, int most, const char *spans)
{
    while (spans && *spans && n < most) {
        unsigned lo, hi;
        int used = 0;
        if (sscanf(spans, "U+%X-%X%n", &lo, &hi, &used) == 2 && used)
            ;
        else if (sscanf(spans, "U+%X%n", &lo, &used) == 1 && used)
            hi = lo;
        else
            break;
        got[n][0] = lo;
        got[n][1] = hi;
        n++;
        spans += used;
        if (*spans == ',')
            spans++;
    }
    return n;
}

static int span_cmp(const void *a, const void *b)
{
    Uint32 x = ((const Uint32 *)a)[0], y = ((const Uint32 *)b)[0];
    return x < y ? -1 : x > y;
}

/* What `screen` says this wall draws. A span is claimed where the font has
 * a character of it that the font itself must draw, asked as `cp`; where
 * it has not, `drawn` is the part of the span drawn here as geometry,
 * claimed whatever the font. A span with no `cp` is all geometry. The
 * Nerd Font icons are the sets Nerd Fonts patches in, at the code points
 * it gives them, each asked of one icon. The spans are said sorted, those
 * that meet joined and in the fewest digits, since a value on the wire is
 * at most 256 bytes and 32 ranges (PROTOCOL.md). */
static void wall_glyphs(Wall *w)
{
    static const struct { const char *span; Uint32 cp; const char *drawn; } ask[] = {
        {"U+0020-007E", 0x41, NULL},
        {"U+2190-21FF", 0x2192, NULL},
        /* box lines; the dashed and diagonal ones are the font's */
        {"U+2500-257F", 0x254C, "U+2500-2503,U+250C-254B,U+2550-2570,U+2574-257F"},
        {"U+2580-259F", 0, NULL},                   /* blocks and shades */
        /* geometric shapes; the leds emitter and diagonal halves are drawn */
        {"U+25A0-25FF", 0x25CF, "U+25AA,U+25E2-25E5"},
        {"U+2800-28FF", 0, NULL},                   /* braille */
        {"U+FF61-FF9F", 0xFF66, NULL},
        {"U+E0A0-E0A3,U+E0B0-E0D7", 0xE0B0, NULL},  /* Powerline */
        {"U+E300-E3E3", 0xE30D, NULL},              /* Weather */
        {"U+EA60-EC1E", 0xEA60, NULL},              /* Codicons */
        {"U+F000-F2E0", 0xF05A, NULL},              /* Font Awesome */
        {"U+F400-F533", 0xF400, NULL},              /* Octicons */
        {"U+F0001-F1AF0", 0xF0369, NULL},           /* Material Design */
        {DRAWN, 0, NULL}};
    Uint32 got[64][2];
    size_t i, at = 0;
    int n = 0, k;
    const char *env = getenv("BITWALL_GLYPHS");
    if (env) {
        snprintf(w->glyphs, sizeof w->glyphs, "%s", env);
        return;
    }
    for (i = 0; i < sizeof ask / sizeof ask[0]; i++) {
        const char *span = ask[i].span;
        if (ask[i].cp && !TTF_GlyphIsProvided32(w->s->font, ask[i].cp))
            span = ask[i].drawn;
        n = spans_add(got, n, 64, span);
    }
    qsort(got, (size_t)n, sizeof got[0], span_cmp);
    w->glyphs[0] = 0;
    for (k = 0; k < n; k++) {
        Uint32 lo = got[k][0], hi = got[k][1];
        while (k + 1 < n && got[k + 1][0] <= hi + 1) {
            if (got[k + 1][1] > hi)
                hi = got[k + 1][1];
            k++;
        }
        if (lo == hi)
            at += (size_t)snprintf(w->glyphs + at, sizeof w->glyphs - at, "%sU+%X",
                                   at ? "," : "", (unsigned)lo);
        else
            at += (size_t)snprintf(w->glyphs + at, sizeof w->glyphs - at, "%sU+%X-%X",
                                   at ? "," : "", (unsigned)lo, (unsigned)hi);
        if (at >= sizeof w->glyphs)
            return;
    }
}

/* === video_open === */
/* What the process does once, whatever the number of walls: the display
 * and the font library up, and the kind of GL context every window will
 * get. */
static int video_open(const char *driver)
{
    if (display_open(driver) < 0)
        return -1;
    if (TTF_Init() < 0) {
        fprintf(stderr, "bitwall-paint: TTF_Init: %s\n", TTF_GetError());
        return -1;
    }
    /* The GPU path first; a screen whose context will not come up is
     * painted in software. */
    gpu_attributes();
    return 0;
}

/* === video_close === */
static void video_close(void)
{
    TTF_Quit();
    display_close();
}

/* A file written whole and renamed into place, so a reader never sees
 * half of it. */
static void put_file(const char *path, const char *text)
{
    char tmp[700];
    FILE *f;
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    f = fopen(tmp, "w");
    if (!f)
        return;
    fputs(text, f);
    fclose(f);
    rename(tmp, path);
}

static void get_file(const char *path, char *out, size_t max)
{
    FILE *f = fopen(path, "r");
    size_t n = 0;
    out[0] = 0;
    if (!f)
        return;
    n = fread(out, 1, max - 1, f);
    fclose(f);
    out[n] = 0;
    while (n && (out[n - 1] == '\n' || out[n - 1] == ' '))
        out[--n] = 0;
}

static void remember(Wall *w)
{
    char text[64];
    time_t t = time(NULL);
    strftime(text, sizeof text, "%a %d %b %H:%M:%S", localtime(&t));
    put_file(w->last_file, text);
}

static void recall(Wall *w)
{
    get_file(w->last_file, w->last_seen, sizeof w->last_seen);
    if (!w->last_seen[0])
        snprintf(w->last_seen, sizeof w->last_seen, "never this boot");
}

/* Feed text through the parser as if a sender had written it. */
static void self_write(Wall *w, const char *text)
{
    Screen *s = w->s;
    size_t n = strlen(text);
    if (s->len + n > BUF_SIZE)
        return;
    memcpy(s->buf + s->len, text, n);
    s->len += n;
    feed(s);
}

/* The status page: what a person walking up to a wall with no sender
 * wants to know, read from across the room. Home and overwrite rather
 * than clear, or it strobes once a second; every line is padded to the
 * same width so a value that shrinks leaves nothing behind. Where one
 * machine drives several walls, each says which it is and the name a
 * sender reaches it by: the host for wall 0, as with a single wall, and
 * host:N for wall N. */
static void wall_status(Wall *w)
{
    static const char *DIM = "\033[38;5;240m", *LBL = "\033[38;5;244m",
                      *VAL = "\033[38;5;250m", *BAD = "\033[1;31m",
                      *HD = "\033[1;35m", *OFF = "\033[22;39;49m";
    char page[4096], host[128], addr[64], up[96], clock[64], font[64], area[64];
    char which[96], target[160];
    char lines[16][300];
    time_t t = time(NULL);
    int i, n = 0, width = w->s->cols < 60 ? w->s->cols : 60;
    size_t at = 0;

    if (gethostname(host, sizeof host) != 0)
        snprintf(host, sizeof host, "?");
    host[sizeof host - 1] = 0;
    first_address(addr, sizeof addr);
    uptime_text(up, sizeof up);
    strftime(clock, sizeof clock, "%a %d %b  %H:%M:%S", localtime(&t));
    snprintf(font, sizeof font, "%d", w->s->size);
    snprintf(area, sizeof area, "%dx%d cells", w->s->cols, w->s->rows);
    snprintf(which, sizeof which, "%d of %d, %s", w->s->index, w->s->count, w->s->output);
    if (w->s->index)
        snprintf(target, sizeof target, "%s:%d", host, w->s->index);
    else
        snprintf(target, sizeof target, "%s", host);

    snprintf(lines[n++], 300, "%s", "");
    snprintf(lines[n++], 300, "   %s  bitwall%s   %sdisconnected%s", HD, OFF, DIM, OFF);
    snprintf(lines[n++], 300, "%s", "");
    snprintf(lines[n++], 300, "   %s%-14s%s %s%s  %s%s", LBL, "this display", OFF, VAL, host, addr, OFF);
    if (w->s->count > 1) {
        snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "wall", OFF, VAL, which, OFF);
        snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "send to", OFF, VAL, target, OFF);
    }
    snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "font size", OFF, VAL, font, OFF);
    snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "area", OFF, VAL, area, OFF);
    snprintf(lines[n++], 300, "   %s%-14s%s %sno sender connected%s", LBL, "stream", OFF, BAD, OFF);
    snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "last seen", OFF, VAL, w->last_seen, OFF);
    snprintf(lines[n++], 300, "   %s%-14s%s %s%s%s", LBL, "uptime", OFF, VAL, up, OFF);
    snprintf(lines[n++], 300, "%s", "");
    snprintf(lines[n++], 300, "   %s%s%s", DIM, clock, OFF);

    for (i = 0; i < n && at + 400 < sizeof page; i++) {
        /* Visible width is the text less its escapes; pad to the line. */
        const char *p = lines[i];
        int seen = 0, k;
        at += (size_t)snprintf(page + at, sizeof page - at, "\033[%d;1H%s", i + 1, lines[i]);
        while (*p) {
            if (*p == 0x1b) {
                while (*p && *p != 'm')
                    p++;
                if (*p)
                    p++;
                continue;
            }
            seen++;
            p++;
        }
        for (k = seen; k < width && at + 2 < sizeof page; k++)
            page[at++] = ' ';
        page[at] = 0;
    }
    at += (size_t)snprintf(page + at, sizeof page - at, "\033[0m");
    self_write(w, page);
}

/* ------------------------------------------------------------- the wall */

/* How many controllers a wall takes at once, said in caps, against a
 * program that leaks them. */
#define CONTROLLERS 8

/* The conversation's clocks, scaled together for tests by
 * BITWALL_CLOCK_SCALE, so a test waits a tenth of a second where a real
 * wall waits a whole one. Set once at startup. */
static double CLK = 1.0;

/* -- forward declarations for this section -------------------------------- */
static void flush_out(Session *s);
static void put_line(Session *s, const char *line);
static void emit(Session *s, const char *line);
static void tell_controllers(Wall *w, const char *line, Session *but,
                             Session *cause, const char *tag);
static void tell_layers(Wall *w, const char *line, Session *cause, const char *tag);
static int own_grid(Wall *w, Session *x);
static void session_report(Session *from);
static void session_command(Wall *w, Session *from, char **words, int n, const char *line);
static void cmd_glass(Wall *w, Session *from, char **words, int n);
static void controller_feed(Session *s, const uint8_t *p, size_t n);
static void wall_data(Wall *w);
static void ping_clock(Session *s, double now, int watching);
static void session_reap(Wall *w, double now);
static const char *screen_line(Wall *w, char *out, size_t max);
static void say_presence(Session *to);
static void wall_stats(Wall *w);

/* -- the out queue ------------------------------------------------------- */

/* Which lines say a current value: merged into the latest when the reader
 * falls behind, never dropped. A line with an id= is an answer, an event. */
static int out_state(const char *first, const char *line)
{
    if (strstr(line, " id="))
        return 0;
    return !strcmp(first, "area") || !strcmp(first, "watching")
        || !strcmp(first, "accepts") || !strcmp(first, "display")
        || !strcmp(first, "power") || !strcmp(first, "presence")
        || !strcmp(first, "screen") || !strcmp(first, "stats");
}

/* Which lines are never dropped: the ones a session must hear. */
static int out_keep(const char *first, const char *line)
{
    return !strcmp(first, "opened") || !strcmp(first, "closed")
        || strncmp(line, "error open ", 11) == 0;
}

/* An event let go, counted on a note at the end of the queue: `dropped
 * <n>` is said where the lines went, after what was queued before them
 * and before the next line that fits. */
static void drop_one(Session *s)
{
    s->n_dropped++;
    if (!s->qtail || s->qtail->kind != OUT_NOTE) {
        OutItem *n = calloc(1, sizeof *n);
        if (!n)
            return;
        n->kind = OUT_NOTE;
        if (s->qtail)
            s->qtail->next = n;
        else
            s->qhead = n;
        s->qtail = n;
    }
    s->qtail->drops++;
}

/* One item onto a session's out queue, by the rules for its kind: a keep
 * always waits, a state merges into the latest of its kind, an event is
 * dropped and counted when the reader is behind, a blob waits its turn. */
static void put_item(Session *s, int kind, const char *key, const char *data, size_t len)
{
    OutItem *it;
    if (s->out_fd < 0)
        return;
    if (s->qhead) {
        int waiting = 0;
        OutItem *x;
        for (x = s->qhead; x; x = x->next)
            if (x->kind == OUT_BLOB) {
                waiting = 1;
                break;
            }
        if (kind == OUT_EVENT && !waiting) {
            drop_one(s);
            return;
        }
        if (kind == OUT_STATE && !waiting) {
            for (x = s->qhead; x; x = x->next)
                if (x->kind == OUT_STATE && !strcmp(x->key, key)) {
                    OutItem *n = malloc(sizeof *n + len);
                    if (!n)
                        return;
                    n->kind = OUT_STATE;
                    snprintf(n->key, sizeof n->key, "%s", key);
                    n->off = 0;
                    n->len = len;
                    n->job = NULL;
                    n->drops = 0;
                    memcpy(n->data, data, len);
                    n->next = x->next;
                    if (x == s->qtail)
                        s->qtail = n;
                    s->queued += len - x->len;
                    /* replace x in place */
                    if (x == s->qhead)
                        s->qhead = n;
                    else {
                        OutItem *p = s->qhead;
                        while (p->next != x)
                            p = p->next;
                        p->next = n;
                    }
                    free(x);
                    return;
                }
        }
        if (kind == OUT_EVENT && s->queued + len > OUT_QUEUE) {
            drop_one(s);
            return;
        }
    }
    it = malloc(sizeof *it + len);
    if (!it)
        return;
    it->kind = kind;
    snprintf(it->key, sizeof it->key, "%s", key ? key : "");
    it->off = 0;
    it->len = len;
    it->job = NULL;
    it->drops = 0;
    memcpy(it->data, data, len);
    it->next = NULL;
    if (s->qtail)
        s->qtail->next = it;
    else
        s->qhead = it;
    s->qtail = it;
    s->queued += len;
    flush_out(s);
}

/* As much of the queue as the pipe takes now; a `dropped <n>` goes before
 * the next line that fits. Never blocks. */
static void flush_out(Session *s)
{
    int wrote = 0;
    s->stuck = 0;
    while (s->qhead) {
        OutItem *it = s->qhead;
        ssize_t k;
        if (it->job)
            return;             /* a picture still being made */
        if (it->kind == OUT_NOTE) {
            char note[64];
            int m = snprintf(note, sizeof note, "dropped %lu\n", it->drops);
            k = ipc_write(s->out_fd, note, (size_t)m);
            if (k != m) {
                s->stuck = !wrote;
                return;         /* no room even for the note */
            }
            wrote = 1;
            s->n_out += m;
            if (s->w)
                s->w->s->bytes_out += m;
            s->qhead = it->next;
            if (!s->qhead)
                s->qtail = NULL;
            free(it);
            continue;
        }
        k = ipc_write(s->out_fd, it->data + it->off, it->len - it->off);
        if (k > 0) {
            wrote = 1;
            it->off += (size_t)k;
            s->queued -= (size_t)k;
            s->n_out += (double)k;
            if (s->w)
                s->w->s->bytes_out += (double)k;
        } else if (k < 0 && errno == EINTR) {
            continue;
        } else {
            s->stuck = !wrote;
            return;             /* EAGAIN: full, the rest waits for the reader */
        }
        if (it->off < it->len)
            return;
        s->qhead = it->next;
        if (!s->qhead)
            s->qtail = NULL;
        free(it);
    }
}

/* A session's out queue let go, and its jobs with the pool forgotten, so
 * they are let go when they come back: its frames, and the pictures
 * holding places in its `out`. */
static void free_queue(Session *s)
{
    OutItem *it = s->qhead, *n;
    Job *j = s->jhead, *after;
    for (; j; j = after) {
        after = j->after;
        if (j->kind == JOB_FRAME && !j->back)
            j->owner = NULL;    /* the pool lets it go when it is done */
        else
            job_free(j);
    }
    s->jhead = s->jtail = NULL;
    s->frames_out = 0;
    while (it) {
        n = it->next;
        if (it->job)
            it->job->owner = NULL;
        free(it);
        it = n;
    }
    s->qhead = s->qtail = NULL;
    s->queued = 0;
}

/* One line into a session's out, classified by its first word unless it is
 * a keep. A trailing newline is added. */
static void put_line(Session *s, const char *line)
{
    char first[24];
    size_t i, len = strlen(line);
    char *buf;
    int kind;
    for (i = 0; i < sizeof first - 1 && line[i] && line[i] != ' '; i++)
        first[i] = line[i];
    first[i] = 0;
    kind = out_keep(first, line) ? OUT_KEEP
         : out_state(first, line) ? OUT_STATE : OUT_EVENT;
    buf = malloc(len + 2);
    if (!buf)
        return;
    memcpy(buf, line, len);
    buf[len] = '\n';
    buf[len + 1] = 0;
    put_item(s, kind, kind == OUT_STATE ? first : "", buf, len + 1);
    free(buf);
}

/* A line to a session, and then, tagged, to every controller that follows
 * it. A followed line, a measure and a dropped note are not themselves
 * followed on to another, and a video layer's `frame` line is part of its
 * frame, which is never followed. */
static void echo(Wall *w, Session *from, char mark, const char *line)
{
    Session *c;
    char marked[MAX_APC + 96];
    if (mark == '>') {
        if (!strncmp(line, "measure ", 8) || !strncmp(line, "dropped ", 8)
            || !strncmp(line, "> ", 2) || !strncmp(line, "< ", 2))
            return;
    }
    if (mark == '<' && !strncmp(line, "frame ", 6))
        return;
    snprintf(marked, sizeof marked, "%c %s %s", mark, from->name, line);
    for (c = w->sessions; c; c = c->next) {
        if (c == from || c->state != S_OPEN || c->role != ROLE_CONTROLLER)
            continue;
        if (mark == '<' && c->follow != 2)
            continue;
        if (mark == '>' && c->follow < 1)
            continue;
        put_line(c, marked);
    }
}

static void emit(Session *s, const char *line)
{
    put_line(s, line);
    if (s->w)
        echo(s->w, s, '>', line);
}

/* The opening `hello` on an `out`: a keep, never dropped, whatever else is
 * waiting, so a session that opened is always greeted. */
static void emit_keep(Session *s, const char *line)
{
    size_t len = strlen(line);
    char *b = malloc(len + 2);
    if (!b)
        return;
    memcpy(b, line, len);
    b[len] = '\n';
    b[len + 1] = 0;
    put_item(s, OUT_KEEP, "", b, len + 1);
    free(b);
    if (s->w)
        echo(s->w, s, '>', line);
}

/* A picture or a mirror frame: a header line and its bytes, which wait in
 * the queue rather than being dropped. Not followed to a controller. */
static int put_blob(Session *s, const char *header, const char *data, size_t len)
{
    size_t hl = strlen(header);
    char *buf = malloc(hl + 1 + len);
    if (!buf)
        return 0;
    memcpy(buf, header, hl);
    buf[hl] = '\n';
    memcpy(buf + hl + 1, data, len);
    /* A blob may pass OUT_QUEUE only if there is room; caller checks. */
    if (s->queued + hl + 1 + len > OUT_QUEUE) {
        free(buf);
        return 0;
    }
    put_item(s, OUT_BLOB, "", buf, hl + 1 + len);
    free(buf);
    return 1;
}

/* A place in a session's `out` held for a picture the pool is making, and
 * the job handed over: everything said to the session after it waits for
 * it. The job is the pool's from here. */
static void put_later(Session *s, Job *j)
{
    OutItem *it;
    if (s->out_fd < 0 || !(it = calloc(1, sizeof *it))) {
        job_free(j);
        return;
    }
    it->kind = OUT_BLOB;
    it->job = j;
    j->owner = s;
    j->item = it;
    if (s->qtail)
        s->qtail->next = it;
    else
        s->qhead = it;
    s->qtail = it;
    pool_submit(j);
}

/* A held place filled: with `data`, a blob or a line, or with nothing,
 * when it is let go. */
static void put_filled(Session *s, OutItem *hold, const char *data, size_t len)
{
    OutItem *prev = NULL, *x, *it = NULL;
    for (x = s->qhead; x && x != hold; x = x->next)
        prev = x;
    if (!x)
        return;
    if (data && (it = malloc(sizeof *it + len))) {
        it->kind = OUT_BLOB;
        it->key[0] = 0;
        it->off = 0;
        it->len = len;
        it->job = NULL;
        it->drops = 0;
        memcpy(it->data, data, len);
        it->next = hold->next;
        s->queued += len;
    }
    if (prev)
        prev->next = it ? it : hold->next;
    else
        s->qhead = it ? it : hold->next;
    if (s->qtail == hold)
        s->qtail = it ? it : prev;
    free(hold);
    flush_out(s);
}

/* A line to a session, with the id of the command it answers when it goes
 * to the session that asked: a line a command causes on another session
 * is not that session's answer, and carries no id of anyone's. */
static void wsay(Session *s, const char *fmt, ...)
{
    char line[MAX_APC + 64];
    va_list ap;
    int n;
    va_start(ap, fmt);
    n = vsnprintf(line, MAX_APC + 1, fmt, ap);
    va_end(ap);
    if (n < 0 || n > MAX_APC)
        return;
    if (s->w && s->w->id[0] && s == s->w->to)
        snprintf(line + n, sizeof line - (size_t)n, " id=%s", s->w->id);
    if (s->say_later) {
        /* An answer to a frame's line read past frames still with the pool:
         * said in its turn, after them. */
        Job *j = calloc(1, sizeof *j);
        if (j && (j->say = strdup(line))) {
            j->kind = JOB_SAY;
            j->owner = s;
            j->back = 1;
            if (s->jtail)
                s->jtail->after = j;
            else
                s->jhead = j;
            s->jtail = j;
            return;
        }
        free(j);
    }
    emit(s, line);
}

/* A line as written, no id appended, for measures and other lines that are
 * not answers. */
static void wsay_line(Session *s, const char *line)
{
    put_line(s, line);
}

static void tell_controllers(Wall *w, const char *line, Session *but,
                             Session *cause, const char *tag)
{
    Session *s;
    char buf[MAX_APC + 64];
    for (s = w->sessions; s; s = s->next) {
        if (s->state != S_OPEN || s->role != ROLE_CONTROLLER || s == but)
            continue;
        if (s == cause && tag && tag[0]) {
            snprintf(buf, sizeof buf, "%s%s", line, tag);
            emit(s, buf);
        } else {
            emit(s, line);
        }
    }
}

static void tell_layers(Wall *w, const char *line, Session *cause, const char *tag)
{
    Session *s;
    char buf[MAX_APC + 64];
    for (s = w->sessions; s; s = s->next) {
        if (s->state != S_OPEN || s->role != ROLE_LAYER)
            continue;
        if (s == cause && tag && tag[0]) {
            snprintf(buf, sizeof buf, "%s%s", line, tag);
            emit(s, buf);
        } else {
            emit(s, line);
        }
    }
}

/* -- what the wall shows, and why --------------------------------------- */

static const char *display_word(Wall *w)
{
    Screen *s = w->s;
    return w->sender_blank ? "blank" : w->absent ? "away"
         : w->dozing ? "doze" : s->blanked ? "blank" : "lit";
}

static const char *power_word(Wall *w)
{
    return w->s->lit ? "on" : "standby";
}

static int dark_asked(Wall *w)
{
    return w->sender_blank || w->absent;
}

static int watched(Wall *w)
{
    return !dark_asked(w);
}

static int wall_holds_picture(Wall *w)
{
    return w->connected && !w->showing_status;
}

/* The screen line: what a cell can be, and how big the glass and the cell
 * are. Said again after every area change, since the cell moves with the
 * font. */
static const char *screen_line(Wall *w, char *out, size_t max)
{
    Screen *s = w->s;
    int n = snprintf(out, max, "screen colour=%s rate=%d pixels=%dx%d cell=%dx%d",
                     w->colour, w->rate, s->width, s->height, s->cw, s->ch);
    if (w->glyphs[0] && n > 0 && (size_t)n < max)
        snprintf(out + n, max - (size_t)n, " glyphs=%s", w->glyphs);
    return out;
}

/* -- the area, and repaint ---------------------------------------------- */

/* The area, the screen and `repaint` go to every session but a video
 * layer: they are about cells, and a video layer is pixels over pixels. */
static void tell_cells(Wall *w, const char *line, Session *cause, const char *tag)
{
    Session *s;
    char buf[MAX_APC + 64];
    for (s = w->sessions; s; s = s->next) {
        if (s->state != S_OPEN || (s->is_video && !s->is_cells) || own_grid(w, s))
            continue;
        if (s == cause && tag && tag[0]) {
            snprintf(buf, sizeof buf, "%s%s", line, tag);
            emit(s, buf);
        } else {
            emit(s, line);
        }
    }
}

static void wall_area(Wall *w)
{
    char area[64], screen[320];
    const char *tag = w->id[0] ? NULL : NULL;
    char tagbuf[40] = "";
    if (w->id[0])
        snprintf(tagbuf, sizeof tagbuf, " id=%s", w->id);
    tag = tagbuf;
    snprintf(area, sizeof area, "area %dx%d %d", w->s->cols, w->s->rows, w->s->size);
    tell_cells(w, area, w->to, tag);
    /* The cell moved with the font, so the screen line is said again; it is
     * state, not an answer, so it carries no id. */
    screen_line(w, screen, sizeof screen);
    tell_cells(w, screen, NULL, "");
}

static void wall_repaint(Wall *w)
{
    tell_cells(w, "repaint", NULL, "");
}

/* Whether a session is a cells layer with a grid of its own, which hears
 * its own area rather than the base's. */
static int own_grid(Wall *w, Session *x)
{
    return x->is_cells && x->z > 0 && w->s->cells[x->z] && w->s->cells[x->z]->face;
}

/* A cells layer told its area and its screen, the answer to its own
 * `font` or `grid` carrying the id; then `repaint`. Its own grid's, or the
 * base's where it follows the base, as after `font base`. */
static void layer_area(Wall *w, int z, int repaint)
{
    Session *x = w->above[z];
    LGrid *g = w->s->cells[z];
    char screen[320];
    int size;
    if (!x || x->state != S_OPEN || !g)
        return;
    size = g->face ? g->face->size : w->s->size;
    if (x == w->to) {
        wsay(x, "area %dx%d %d", g->cols, g->rows, size);
    } else {
        char area[64];
        snprintf(area, sizeof area, "area %dx%d %d", g->cols, g->rows, size);
        emit(x, area);
    }
    if (g->face)
        face_swap(w->s, g->face);
    screen_line(w, screen, sizeof screen);
    if (g->face)
        face_swap(w->s, g->face);
    emit(x, screen);
    if (repaint)
        emit(x, "repaint");
}

/* -- presence, blank, power --------------------------------------------- */

static void say_presence(Session *to)
{
    Wall *w = to->w;
    char line[96], name[48] = "";
    if (w->presence_name[0])
        snprintf(name, sizeof name, " name=%s", w->presence_name);
    if (w->presence_from[0])
        snprintf(line, sizeof line, "presence %d%s from=%s age=%.0f", !w->absent, name,
                 w->presence_from, (now_ms() - w->presence_set) / 1e3);
    else
        snprintf(line, sizeof line, "presence %d%s", !w->absent, name);
    wsay(to, "%s", line);
}

/* Say the display and the power to the controllers, and the watching count
 * to the layers, whenever any has changed, whatever changed it. */
static void wall_power(Wall *w)
{
    const char *now = display_word(w), *power = power_word(w);
    int seen = watched(w);
    char line[32];
    if (now != w->display_said) {
        w->display_said = now;
        snprintf(line, sizeof line, "display %s", now);
        tell_controllers(w, line, NULL, NULL, "");
    }
    if (power != w->power_said) {
        w->power_said = power;
        snprintf(line, sizeof line, "power %s", power);
        tell_controllers(w, line, NULL, NULL, "");
    }
    if (seen != w->watched_said) {
        /* Coming back into view: a base told nobody was watching stopped
         * drawing and is about to start again, so its silence and a ping
         * put during it are not held against it. */
        if (seen && w->watched_said == 0 && w->connected && w->base) {
            w->base->last_in = now_ms();
            w->base->awaiting = 0;
        }
        w->watched_said = seen;
        snprintf(line, sizeof line, "watching %d", seen);
        tell_layers(w, line, NULL, "");
    }
}

/* -- a session's metadata ------------------------------------------------ */

static Meta *smeta_find(Session *s, const char *key)
{
    int i;
    for (i = 0; i < s->nmeta; i++)
        if (!strcmp(s->meta[i].key, key))
            return &s->meta[i];
    return NULL;
}

static size_t smeta_bytes(Session *s)
{
    size_t n = 0;
    int i;
    for (i = 0; i < s->nmeta; i++)
        n += strlen(s->meta[i].key) + strlen(s->meta[i].value) + 2;
    return n;
}

static void smeta_drop(Session *s, int i)
{
    s->meta[i] = s->meta[--s->nmeta];
}

/* One session's metadata, said to `to`, as the statements that would set it
 * again, each with how long ago. `tag` is the session's name after `meta`
 * for a layer above the base, and empty for the base's own. */
static void smeta_say(Session *from, Session *to, int ages, const char *tag)
{
    double now = now_ms();
    const char *sp = tag[0] ? " " : "";
    int i;
    for (i = 0; i < from->nmeta; i++) {
        wsay(to, "meta%s%s set %s %s", sp, tag, from->meta[i].key, from->meta[i].value);
        if (from->meta[i].hidden)
            wsay(to, "meta%s%s hide %s", sp, tag, from->meta[i].key);
        if (ages)
            wsay(to, "meta%s%s age %s %.0f", sp, tag, from->meta[i].key,
                 (now - from->meta[i].set) / 1e3);
    }
}

static void session_meta(Wall *w, Session *from, char **words, int n, const char *line)
{
    char said[64];
    char *sw[MAX_PARAMS];
    int nsw = 0, i, bad_id = 0;
    const char *verb, *key;
    Meta *m;
    (void)line;
    /* The id may be any word, and is taken out before the verb, the key and
     * the value are read; the value is what is left, its words a single
     * space apart. */
    w->id[0] = 0;
    for (i = 0; i < n; i++) {
        if (i > 0 && strncmp(words[i], "id=", 3) == 0) {
            if (id_ok(words[i] + 3))
                snprintf(w->id, sizeof w->id, "%s", words[i] + 3);
            else
                bad_id = 1;
        } else if (nsw < MAX_PARAMS) {
            sw[nsw++] = words[i];
        }
    }
    if (bad_id) {
        w->id[0] = 0;
        wsay(from, "error meta id: not a token");
        return;
    }
    verb = nsw >= 2 ? sw[1] : NULL;
    key = nsw >= 3 ? sw[2] : NULL;
    if (verb && !strcmp(verb, "report")) {
        smeta_say(from, from, 1, "");
        return;
    }
    if (verb && !strcmp(verb, "clear")) {
        if (!key) {
            from->nmeta = 0;
            wsay(from, "meta clear");
            return;
        }
        if ((m = smeta_find(from, key))) {
            smeta_drop(from, (int)(m - from->meta));
            wsay(from, "meta clear %s", key);
        }
        return;
    }
    if (!verb || (strcmp(verb, "set") && strcmp(verb, "hide") && strcmp(verb, "show"))) {
        wsay(from, "error meta %s: unknown verb", verb ? plain(verb, said, sizeof said) : "no verb");
        return;
    }
    if (!key) {
        wsay(from, "error meta %s no key", verb);
        return;
    }
    if (!key_ok(key)) {
        wsay(from, "error meta %s %s: not a key", verb, plain(key, said, sizeof said));
        return;
    }
    if (!strcmp(verb, "hide") || !strcmp(verb, "show")) {
        if (!(m = smeta_find(from, key))) {
            wsay(from, "error meta %s %s: not set", verb, key);
            return;
        }
        m->hidden = verb[0] == 'h';
        wsay(from, "meta %s %s", verb, key);
        return;
    }
    {
        char value[META_VALUE + 64];
        size_t at = 0, len;
        for (i = 3; i < nsw; i++)
            at += (size_t)snprintf(value + at, sizeof value - at, "%s%s", i > 3 ? " " : "", sw[i]);
        len = strlen(value);
        if (len == 0) {
            wsay(from, "error meta set %s: no value", key);
            return;
        }
        if (len >= META_VALUE) {
            wsay(from, "error meta set %s: too long", key);
            return;
        }
        m = smeta_find(from, key);
        if (!m && from->nmeta >= META_KEYS) {
            wsay(from, "error meta set %s: too many keys", key);
            return;
        }
        if (smeta_bytes(from) + strlen(key) + len + 2
            > META_TOTAL + (m ? strlen(m->value) : 0)) {
            wsay(from, "error meta set %s: too much", key);
            return;
        }
        if (!m) {
            m = &from->meta[from->nmeta++];
            m->hidden = 0;
            snprintf(m->key, sizeof m->key, "%s", key);
        }
        snprintf(m->value, sizeof m->value, "%s", value);
        m->set = now_ms();
        wsay(from, "meta set %s %s", m->key, m->value);
    }
}

/* -- the overlay's own state -------------------------------------------- */

static int stats_shown(const Screen *s)
{
    return s->st.on && (!s->wall || s->wall->connected);
}

static void wall_stats(Wall *w)
{
    Screen *s = w->s;
    Stats *st = &s->st;
    Sample now = sample_of(s);
    char lines[STATS_LINES][96];
    int i, n = 0;
    Session *x;
    if (st->nseen == 3) {
        memmove(st->seen, st->seen + 1, sizeof st->seen[0] * 2);
        st->nseen--;
    }
    st->seen[st->nseen++] = now;
    st->last = now.t;
    if (s->blanked || !stats_shown(s))
        return;
    for (i = 0; i < NMEASURES; i++) {
        char value[80];
        if (!(st->show & (1u << i)))
            continue;
        measure(w, i, &now, st->nseen > 1 ? &st->seen[0] : NULL, value, sizeof value);
        snprintf(lines[n++], sizeof lines[0], "%-6s %s", MEASURES[i], value);
    }
    lines[n++][0] = 0;
    /* As they stack: the top layer first and down to the base, as the
     * glass shows them, then the controllers, which draw nothing. */
    for (i = LAYERS - 1; i >= -1; i--)
        for (x = w->sessions; x && n < STATS_LINES; x = x->next)
            if (x->state == S_OPEN && (i >= 0 ? x->role == ROLE_LAYER && x->z == i
                                              : x->role != ROLE_LAYER))
                session_row(x, lines[n++], sizeof lines[0]);
    if (!lines[n - 1][0])
        n--;
    stats_render(s, lines, n);
    refresh(s);
}

static void stats_line(Wall *w, char *line, size_t len)
{
    Stats *st = &w->s->st;
    char names[160];
    measure_names(st->show, names, sizeof names);
    snprintf(line, len, "stats %d at=%s size=%d show=%s", st->on, POINTS[st->at], st->size,
             names);
}

static void say_stats(Session *to)
{
    char line[256];
    stats_line(to->w, line, sizeof line);
    wsay(to, "%s", line);
}

/* -- the command handlers ----------------------------------------------- */

static void cmd_pong(Session *from, double now)
{
    if (from->awaiting) {
        from->awaiting = 0;
        from->answered = 1;
        from->pong_ms = now - from->pinged_at;      /* both ms */
        if (from->is_base)
            from->w->pong_ms = from->pong_ms;
    }
}

static void cmd_font(Wall *w, Session *from, char **words, int n)
{
    long want = 0;
    int i;
    for (i = 1; i < n; i++) {
        const char *a = words[i];
        char *end;
        if (strncmp(a, "n=", 2) == 0)
            a += 2;
        want = strtol(a, &end, 10);
        if (end != a && *end == 0)
            break;
        want = 0;
    }
    if (want >= 1 && want <= 400) {
        w->showing_status = 0;
        w->asked_font = (int)want;
        set_font(w->s, (int)want);
    } else if (w->id[0]) {
        wsay(from, "error font no size");
    }
}

static void cmd_grid(Wall *w, Session *from, char **words, int n)
{
    Screen *s = w->s;
    char said[48];
    int cols = 0, rows = 0, best;
    if (n < 2 || sscanf(words[1], "%dx%d", &cols, &rows) != 2 || cols < 1 || rows < 1) {
        wsay(from, "error grid %s: not <cols>x<rows>",
             n >= 2 ? plain(words[1], said, sizeof said) : "");
        return;
    }
    best = grid_font(s, cols, rows);
    if (!best) {
        wsay(from, "error grid %dx%d: too many cells for this screen", cols, rows);
        return;
    }
    w->showing_status = 0;
    w->asked_font = best;
    set_font(s, best);
}

/* A cells layer's `font` or `grid`: a grid of its own at that font, or the
 * font that fits that grid, kept through a change of glass; or `base`, to
 * follow the base's grid again. Answered with the layer's own area. */
static void layer_font(Wall *w, Session *from, char **words, int n, int grid)
{
    Screen *s = w->s;
    LGrid *g = s->cells[from->z];
    char said[48];
    int size = 0, cols = 0, rows = 0, i;
    Face *f;
    if (!g)
        return;
    if (n >= 2 && !strcmp(words[1], "base")) {
        face_free(s, g->face);
        g->face = NULL;
        g->own = 0;
    } else if (grid) {
        if (n < 2 || sscanf(words[1], "%dx%d", &cols, &rows) != 2 || cols < 1 || rows < 1) {
            wsay(from, "error grid %s: not <cols>x<rows>",
                 n >= 2 ? plain(words[1], said, sizeof said) : "");
            return;
        }
        size = grid_font(s, cols, rows);
        if (!size) {
            wsay(from, "error grid %dx%d: too many cells for this screen", cols, rows);
            return;
        }
    } else {
        for (i = 1; i < n; i++) {
            const char *a = words[i];
            char *end;
            long v;
            if (strncmp(a, "n=", 2) == 0)
                a += 2;
            v = strtol(a, &end, 10);
            if (end != a && *end == 0 && v >= 1 && v <= 400) {
                size = (int)v;
                break;
            }
        }
        if (!size) {
            wsay(from, "error font no size");
            return;
        }
    }
    if (size) {
        if (!(f = face_new(s, size))) {
            wsay(from, "error %s %s: not offered", grid ? "grid" : "font",
                 plain(words[1], said, sizeof said));
            return;
        }
        if (grid) {
            face_swap(s, f);
            face_grid(s, cols, rows);
            face_swap(s, f);
        }
        face_free(s, g->face);
        g->face = f;
        g->own = grid ? 2 : 1;
        g->want_cols = cols;
        g->want_rows = rows;
        g->fw = s->width;
        g->fh = s->height;
    }
    if (lgrid_fit(s, g) < 0) {
        fprintf(stderr, "bitwall-paint: out of memory\n");
        exit(1);
    }
    layer_area(w, from->z, 1);
    redraw(s);
}

/* Say the display and the watching count that a change has made, to the
 * controllers and layers, with the causer's copy carrying its id. */
static void dark_changed(Wall *w, const char *was_disp, int was_watch,
                         Session *cause, const char *tag)
{
    const char *now = display_word(w);
    int seen = watched(w);
    char line[32];
    if (strcmp(now, was_disp) != 0) {
        snprintf(line, sizeof line, "display %s", now);
        tell_controllers(w, line, NULL, cause, tag);
        w->display_said = now;
    }
    if (seen != was_watch) {
        if (seen && was_watch == 0 && w->connected && w->base) {
            w->base->last_in = now_ms();
            w->base->awaiting = 0;
        }
        snprintf(line, sizeof line, "watching %d", seen);
        tell_layers(w, line, cause, tag);
        w->watched_said = seen;
    }
}

static void cmd_blank(Wall *w, Session *from, char **words, int n)
{
    const char *was_disp = display_word(w);
    int was_watch = watched(w), on;
    char tag[40] = "";
    if (n < 2) {
        wsay(from, "error blank no value");
        return;
    }
    if (strcmp(words[1], "0") != 0 && strcmp(words[1], "1") != 0) {
        char said[16];
        wsay(from, "error blank %s: not 0 or 1", plain(words[1], said, sizeof said));
        return;
    }
    on = strcmp(words[1], "1") == 0;
    w->sender_blank = on;
    if (!on)
        w->dozing = 0;
    blank(w->s, on || w->absent);
    if (w->id[0])
        snprintf(tag, sizeof tag, " id=%s", w->id);
    dark_changed(w, was_disp, was_watch, from, tag);
}

static void cmd_want(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    int i;
    (void)w;
    for (i = 1; i < n; i++) {
        const char *a = words[i], *v = strchr(a, '=');
        if (!v) {
            wsay(from, "error want %s: unknown option", plain(a, said, sizeof said));
            continue;
        }
        if (strncmp(a, "touch=", 6) && strncmp(a, "keyboard=", 9)
            && strncmp(a, "buttons=", 8))
            wsay(from, "error want %s: unknown option", plain(a, said, sizeof said));
        else if (strcmp(v + 1, "0") && strcmp(v + 1, "1"))
            wsay(from, "error want %s: not 0 or 1", plain(a, said, sizeof said));
        /* v1 accepts nothing to pass on, so a well-formed want has no
         * effect but its errors. */
    }
}

/* Where a video layer's stream sits on a glass of another shape: `align
 * top`, `centre` or `bottom`, from its next frame. */
static void cmd_align(Session *from, char **words, int n)
{
    char said[48];
    const char *v = n > 1 ? words[1] : "";
    if (!strcmp(v, "top"))
        from->data_align = -1;
    else if (!strcmp(v, "centre"))
        from->data_align = 0;
    else if (!strcmp(v, "bottom"))
        from->data_align = 1;
    else
        wsay(from, "error align %s: not top, centre or bottom", plain(v, said, sizeof said));
}

static void cmd_presence(Wall *w, Session *from, char **words, int n)
{
    char said[48], tag[40] = "";
    const char *was_disp = display_word(w);
    int was_watch = watched(w), i, was = w->absent;
    const char *fromtok = NULL;
    if (w->id[0])
        snprintf(tag, sizeof tag, " id=%s", w->id);
    if (n < 2) {
        wsay(from, "error presence no value");
        return;
    }
    if (strcmp(words[1], "0") != 0 && strcmp(words[1], "1") != 0) {
        wsay(from, "error presence %s: not 0 or 1", plain(words[1], said, sizeof said));
        return;
    }
    for (i = 2; i < n; i++) {
        if (strncmp(words[i], "from=", 5) == 0 && id_ok(words[i] + 5))
            fromtok = words[i] + 5;
        else if (strncmp(words[i], "from=", 5) == 0) {
            wsay(from, "error presence %s: not a token", plain(words[i], said, sizeof said));
            return;
        } else {
            wsay(from, "error presence %s: unknown option", plain(words[i], said, sizeof said));
            return;
        }
    }
    if (!fromtok) {
        wsay(from, "error presence no from");
        return;
    }
    w->absent = words[1][0] == '0';
    snprintf(w->presence_from, sizeof w->presence_from, "%s", fromtok);
    w->presence_set = now_ms();
    if (w->absent != was) {
        if (w->absent)
            blank(w->s, 1);
        else if (!w->connected) {
            /* No sender: the status page shows until wall_tick dozes it. */
            if (!w->sender_blank) {
                w->dozing = 0;
                w->last_data = now_ms();
                blank(w->s, 0);
            }
        } else if (!w->sender_blank && !w->dozing)
            blank(w->s, 0);
    }
    say_presence(from);
    /* The real presence line to the controllers, on a change. */
    if (w->absent != was) {
        char line[96], name[48] = "";
        if (w->presence_name[0])
            snprintf(name, sizeof name, " name=%s", w->presence_name);
        if (w->presence_from[0])
            snprintf(line, sizeof line, "presence %d%s from=%s age=%.0f", !w->absent, name,
                     w->presence_from, (now_ms() - w->presence_set) / 1e3);
        else
            snprintf(line, sizeof line, "presence %d%s", !w->absent, name);
        tell_controllers(w, line, from, NULL, "");
    }
    dark_changed(w, was_disp, was_watch, from, tag);
}

static void cmd_stats(Wall *w, Session *from, char **words, int n)
{
    Stats *st = &w->s->st;
    char said[48], before[256], after[256];
    int i, was = st->on;
    stats_line(w, before, sizeof before);
    if (n < 2 || (strcmp(words[1], "0") != 0 && strcmp(words[1], "1") != 0)) {
        if (n < 2)
            wsay(from, "error stats not 0 or 1");
        else
            wsay(from, "error stats %s: not 0 or 1", plain(words[1], said, sizeof said));
        return;
    }
    for (i = 2; i < n; i++) {
        const char *a = words[i], *v = strchr(a, '=');
        plain(a, said, sizeof said);
        if (v && strncmp(a, "at=", 3) == 0) {
            int k;
            for (k = 0; k < NPOINTS && strcmp(v + 1, POINTS[k]) != 0; k++)
                ;
            if (k == NPOINTS)
                wsay(from, "error stats %s: unknown point", said);
            else
                st->at = k;
        } else if (v && strncmp(a, "size=", 5) == 0) {
            char *end;
            long z = strtol(v + 1, &end, 10);
            if (end == v + 1 || *end)
                wsay(from, "error stats %s: not a number", said);
            else
                st->size = z < 8 ? 8 : z > 40 ? 40 : (int)z;
        } else if (v && strncmp(a, "show=", 5) == 0) {
            char list[MAX_APC + 1], *save = NULL, *name;
            unsigned want = 0;
            int k;
            snprintf(list, sizeof list, "%s", v + 1);
            for (name = strtok_r(list, ",", &save); name; name = strtok_r(NULL, ",", &save)) {
                if (strcmp(name, "all") == 0)
                    want = MEASURE_ALL;
                for (k = 0; k < NMEASURES; k++)
                    if (strcmp(name, MEASURES[k]) == 0)
                        want |= 1u << k;
            }
            want &= measures_offered();
            if (!want)
                wsay(from, "error stats %s: nothing to show", said);
            else
                st->show = want;
        } else {
            wsay(from, "error stats %s: unknown option", said);
        }
    }
    st->on = words[1][0] == '1';
    if (st->on) {
        if (!was)
            st->nseen = 0;
        wall_stats(w);
    } else if (was) {
        refresh(w->s);
    }
    say_stats(from);
    /* Every other controller hears the overlay change, as it hears the
     * display change. */
    stats_line(w, after, sizeof after);
    if (strcmp(before, after) != 0)
        tell_controllers(w, after, from, NULL, "");
}

/* The glass's size fitted inside w by h with its shape kept, and never
 * larger than the glass. */
static void glass_fit(Screen *s, int *w, int *h)
{
    long gw = s->width, gh = s->height;
    if (*w >= gw && *h >= gh) {
        *w = (int)gw;
        *h = (int)gh;
    } else if (gw * *h <= gh * *w) {
        *w = (int)((gw * *h + gh / 2) / gh);
    } else {
        *h = (int)((gh * *w + gw / 2) / gw);
    }
    if (*w < 1)
        *w = 1;
    if (*h < 1)
        *h = 1;
}

/* What a picture or a mirror is of, and how it is asked for: the target
 * and its options, each checked, and the session the target names, null
 * for the glass. `quality` is 0 for QOI, else the JPEG's, 1 to 100,
 * JPEG_QUALITY where `format=jpeg` says none. 0, and the error said, when
 * anything is wrong. `every` and `off` are a mirror's only. */
static int capture_args(Wall *w, Session *from, const char *cmd, char **words, int n,
                        Session **who, int *cw, int *ch, int *quality, double *every, int *off)
{
    char said[48], said2[48];
    const char *target = n >= 2 ? words[1] : NULL;
    const char *sized = NULL, *formed = NULL, *qualified = NULL;
    Session *x = NULL;
    int i, jpeg = 0, q = JPEG_QUALITY;
    *who = NULL;
    *cw = w->s->width;
    *ch = w->s->height;
    *quality = 0;
    if (!target) {
        wsay(from, "error %s no target", cmd);
        return 0;
    }
    if (off && n == 3 && !strcmp(words[2], "off")) {
        *off = 1;           /* whether or not what it names is still there */
        return 1;
    }
    if (strcmp(target, "glass")) {
        for (x = w->sessions; x; x = x->next)
            if (x->state == S_OPEN && !strcmp(x->name, target))
                break;
        if (!x) {
            wsay(from, "error %s %s: no such session", cmd, plain(target, said, sizeof said));
            return 0;
        }
        if (x->role != ROLE_LAYER) {
            wsay(from, "error %s %s: not a layer", cmd, plain(target, said, sizeof said));
            return 0;
        }
    }
    for (i = 2; i < n; i++) {
        const char *v = words[i];
        if (every && !strncmp(v, "every=", 6)) {
            char *end;
            long ms = strtol(v + 6, &end, 10);
            if (end == v + 6 || *end) {
                wsay(from, "error %s %s: not a number", cmd, plain(v, said, sizeof said));
                return 0;
            }
            *every = ms < MIRROR_FASTEST ? MIRROR_FASTEST : ms > 60000 ? 60000 : (double)ms;
        } else if (!strncmp(v, "size=", 5)) {
            const char *xp = strchr(v + 5, 'x');
            int sw = 0, sh = 0;
            if (xp && xp > v + 5 && xp - (v + 5) <= 4 && digits(xp + 1, 4)) {
                char num[5];
                memcpy(num, v + 5, (size_t)(xp - (v + 5)));
                num[xp - (v + 5)] = 0;
                if (digits(num, 4)) {
                    sw = atoi(num);
                    sh = atoi(xp + 1);
                }
            }
            if (sw < 1 || sh < 1 || sw > FRAME_SIDE || sh > FRAME_SIDE) {
                wsay(from, "error %s %s: bad size", cmd, plain(v, said, sizeof said));
                return 0;
            }
            *cw = sw;
            *ch = sh;
            sized = v;
        } else if (!strncmp(v, "format=", 7)) {
            if (!strcmp(v + 7, "qoi"))
                jpeg = 0;
#ifdef BITWALL_JPEG
            else if (!strcmp(v + 7, "jpeg"))
                jpeg = 1;
#endif
            else {
                wsay(from, "error %s %s: not offered", cmd, plain(v, said, sizeof said));
                return 0;
            }
            formed = v;
        } else if (!strncmp(v, "quality=", 8)) {
            q = digits(v + 8, 3) ? atoi(v + 8) : 0;
            if (q < 1 || q > 100) {
                wsay(from, "error %s %s: bad quality", cmd, plain(v, said, sizeof said));
                return 0;
            }
            qualified = v;
        } else {
            wsay(from, "error %s %s: unknown option", cmd, plain(v, said, sizeof said));
            return 0;
        }
    }
    if (x && (sized || formed || qualified)) {
        wsay(from, "error %s %s %s: only for the glass", cmd, plain(target, said, sizeof said),
             plain(sized ? sized : formed ? formed : qualified, said2, sizeof said2));
        return 0;
    }
    if (qualified && !jpeg) {
        wsay(from, "error %s %s: only for jpeg", cmd, plain(qualified, said, sizeof said));
        return 0;
    }
    *quality = jpeg ? q : 0;
    if ((x && x->is_base && !x->is_video && !w->s->gpu) || (!x && !w->s->gpu
                                            && w->s->target->format->BytesPerPixel != 4)
        || (x && x->is_cells && strcmp(cmd, "picture"))) {
        wsay(from, "error %s %s: not offered", cmd, plain(target, said, sizeof said));
        return 0;
    }
    *who = x;
    return 1;
}

/* A job for the pool: pixels read back, or a frame's own, to be made a
 * QOI picture or mirror frame of `name`, said under the job's `head` (all
 * but the count) and `tag`. `px` is the job's from here. */
static Job *qoi_job(uint8_t *px, int w, int h, int yuv, const char *name, const char *tag)
{
    Job *j = calloc(1, sizeof *j);
    if (!j) {
        free(px);
        return NULL;
    }
    j->kind = JOB_QOI;
    j->px = px;
    j->w = w;
    j->h = h;
    j->yuv = yuv;
    j->mirror = -1;
    snprintf(j->name, sizeof j->name, "%s", name);
    snprintf(j->tag, sizeof j->tag, "%s", tag);
    return j;
}

/* A picture of a stream's frame on the glass: the frame as it was decoded,
 * as QOI, where its top left went, and the size it was drawn at where it
 * was scaled. The frame is copied, since the stream uses its frames again,
 * and made QOI by the pool. */
static void data_picture(Wall *w, Session *from, Session *x, const char *tag)
{
    DFrame *f = x->shown;
    const Pic *p = &w->s->pics[x->z];
    char size[32] = "";
    size_t n = f->yuv ? (size_t)f->w * f->h + 2 * (size_t)((f->w + 1) / 2) * ((f->h + 1) / 2)
                      : (size_t)f->w * f->h * 4;
    uint8_t *px = malloc(n);
    Job *j;
    if (!px || !(j = qoi_job(px, f->w, f->h, f->yuv, x->name, tag))) {
        free(px);
        wsay(from, "error picture %s: not offered", x->name);
        return;
    }
    memcpy(px, f->px, n);
    if (p->w != f->w || p->h != f->h)
        snprintf(size, sizeof size, " size=%dx%d", p->w, p->h);
    snprintf(j->head, sizeof j->head, "picture %s qoi %dx%d at=%d,%d%s ", x->name, f->w, f->h,
             p->x, p->y, size);
    put_later(from, j);
}

/* -- a video layer: the pictures a frame may be ------------------------- */

/* What a frame on a video layer's `in` may be, as `frame <codec>` names
 * it and `caps` says it in `images=`. */
enum { IMG_QOI, IMG_PNG, IMG_JPEG, IMG_COUNT };
static const char *const IMAGE_NAME[IMG_COUNT] = {"qoi", "png", "jpeg"};
#ifdef BITWALL_JPEG
#define IMAGES_TAKEN "qoi,png,jpeg"
#else
#define IMAGES_TAKEN "qoi,png"
#endif

/* The codec a frame line names, or -1 for one this wall does not take. */
static int image_codec(const char *name)
{
    int i;
    for (i = 0; i < IMG_COUNT; i++) {
#ifndef BITWALL_JPEG
        if (i == IMG_JPEG)
            continue;
#endif
        if (!strcmp(name, IMAGE_NAME[i]))
            return i;
    }
    return -1;
}

/* A PNG as RGBA, through libpng's simplified API: 16 bits taken to 8, a
 * palette or grey expanded, alpha kept. NULL when it does not decode or is
 * bigger than a frame may be. */
static uint8_t *png_rgba(const uint8_t *data, size_t len, int *w, int *h)
{
    png_image png;
    uint8_t *px;
    memset(&png, 0, sizeof png);
    png.version = PNG_IMAGE_VERSION;
    if (!png_image_begin_read_from_memory(&png, data, len)) {
        png_image_free(&png);
        return NULL;
    }
    if (png.width < 1 || png.height < 1 || png.width > FRAME_SIDE || png.height > FRAME_SIDE) {
        png_image_free(&png);
        return NULL;
    }
    png.format = PNG_FORMAT_RGBA;
    px = malloc(PNG_IMAGE_SIZE(png));
    if (!px || !png_image_finish_read(&png, NULL, px, 0, NULL)) {
        png_image_free(&png);
        free(px);
        return NULL;
    }
    *w = (int)png.width;
    *h = (int)png.height;
    return px;
}

#ifdef BITWALL_JPEG
/* libjpeg's errors, which would otherwise end the process, come back here,
 * and its warnings are not printed. */
typedef struct {
    struct jpeg_error_mgr pub;
    jmp_buf at;
} JpegErr;

static void jpeg_fail(j_common_ptr c)
{
    longjmp(((JpegErr *)c->err)->at, 1);
}

static void jpeg_quiet(j_common_ptr c)
{
    (void)c;
}

/* A JPEG as RGBA, opaque: baseline or progressive, grey or colour. NULL
 * when it does not decode, is CMYK, or is bigger than a frame may be. */
static uint8_t *jpeg_rgba(const uint8_t *data, size_t len, int *w, int *h)
{
    struct jpeg_decompress_struct c;
    JpegErr e;
    uint8_t *volatile px = NULL;
    size_t n, i;
    memset(&c, 0, sizeof c);
    c.err = jpeg_std_error(&e.pub);
    e.pub.error_exit = jpeg_fail;
    e.pub.output_message = jpeg_quiet;
    if (setjmp(e.at)) {
        jpeg_destroy_decompress(&c);
        free(px);
        return NULL;
    }
    jpeg_create_decompress(&c);
    jpeg_mem_src(&c, (unsigned char *)data, (unsigned long)len);
    if (jpeg_read_header(&c, TRUE) != JPEG_HEADER_OK
        || (c.num_components != 1 && c.num_components != 3) || c.image_width < 1
        || c.image_height < 1 || c.image_width > FRAME_SIDE || c.image_height > FRAME_SIDE) {
        jpeg_destroy_decompress(&c);
        return NULL;
    }
#ifdef JCS_EXTENSIONS
    c.out_color_space = JCS_EXT_RGBA;     /* libjpeg-turbo's, straight to RGBA */
#else
    c.out_color_space = JCS_RGB;
#endif
    jpeg_start_decompress(&c);
    n = (size_t)c.output_width * c.output_height;
    px = malloc(n * 4);
    if (!px) {
        jpeg_destroy_decompress(&c);
        return NULL;
    }
    /* Each row is read into the start of its own RGBA row: as RGBA where
     * libjpeg-turbo makes it, and otherwise as RGB, the whole picture
     * spread to RGBA after, from the end, so no pixel is written over
     * before it is read. */
    while (c.output_scanline < c.output_height) {
        JSAMPROW row = px + (size_t)c.output_scanline * c.output_width * 4;
        jpeg_read_scanlines(&c, &row, 1);
    }
    for (i = c.out_color_space == JCS_RGB ? n : 0; i-- > 0;) {
        size_t r = i / c.output_width, x = i % c.output_width;
        const uint8_t *q = px + (r * c.output_width) * 4 + x * 3;
        uint8_t *o = px + i * 4, rgb[3] = {q[0], q[1], q[2]};
        o[0] = rgb[0];
        o[1] = rgb[1];
        o[2] = rgb[2];
        o[3] = 255;
    }
    *w = (int)c.output_width;
    *h = (int)c.output_height;
    jpeg_finish_decompress(&c);
    jpeg_destroy_decompress(&c);
    return px;
}

/* RGBA, w by h, made a JPEG at `quality`, 1 to 100: baseline, 4:2:0, its
 * alpha left out, in a buffer of libjpeg's own, which is free()'d as a QOI
 * is. NULL when it cannot be made. */
static void *jpeg_make(const uint8_t *rgba, int w, int h, int quality, int *len)
{
    struct jpeg_compress_struct c;
    JpegErr e;
    unsigned char *out = NULL;
    unsigned long n = 0;
    uint8_t *volatile rgb = NULL;
    memset(&c, 0, sizeof c);
    c.err = jpeg_std_error(&e.pub);
    e.pub.error_exit = jpeg_fail;
    e.pub.output_message = jpeg_quiet;
    if (setjmp(e.at)) {
        /* `out` is left: where libjpeg grew its buffer it has freed the
         * one `out` names and holds the new one where nobody can reach. */
        jpeg_destroy_compress(&c);
        free(rgb);
        return NULL;
    }
    jpeg_create_compress(&c);
    jpeg_mem_dest(&c, &out, &n);
    c.image_width = (JDIMENSION)w;
    c.image_height = (JDIMENSION)h;
#ifdef JCS_EXTENSIONS
    c.input_components = 4;
    c.in_color_space = JCS_EXT_RGBA;      /* libjpeg-turbo's, straight from RGBA */
#else
    c.input_components = 3;
    c.in_color_space = JCS_RGB;
    if (!(rgb = malloc((size_t)w * 3)))
        longjmp(e.at, 1);
#endif
    jpeg_set_defaults(&c);
    jpeg_set_quality(&c, quality, TRUE);
    jpeg_start_compress(&c, TRUE);
    while (c.next_scanline < c.image_height) {
        const uint8_t *src = rgba + (size_t)c.next_scanline * w * 4;
        JSAMPROW row = (JSAMPROW)src;
        if (rgb) {
            int x;
            for (x = 0; x < w; x++) {
                rgb[x * 3] = src[x * 4];
                rgb[x * 3 + 1] = src[x * 4 + 1];
                rgb[x * 3 + 2] = src[x * 4 + 2];
            }
            row = rgb;
        }
        jpeg_write_scanlines(&c, &row, 1);
    }
    jpeg_finish_compress(&c);
    jpeg_destroy_compress(&c);
    free(rgb);
    if (n > INT_MAX) {
        free(out);
        return NULL;
    }
    *len = (int)n;
    return out;
}
#endif

/* A frame's picture as RGBA, w by h, or NULL when it does not decode. */
static uint8_t *image_rgba(int codec, const uint8_t *data, size_t len, int *w, int *h)
{
    if (codec == IMG_QOI) {
        qoi_desc d;
        uint8_t *px = qoi_decode(data, (int)len, &d, 4);
        if (px) {
            *w = (int)d.width;
            *h = (int)d.height;
        }
        return px;
    }
    if (codec == IMG_PNG)
        return png_rgba(data, len, w, h);
#ifdef BITWALL_JPEG
    if (codec == IMG_JPEG)
        return jpeg_rgba(data, len, w, h);
#endif
    return NULL;
}

/* How a frame is said by a picture or a mirror of it: its codec, its own
 * size, where it went, and the box it was drawn to where it was scaled. */
static void frame_words(const Session *v, char *out, size_t cap)
{
    int n = snprintf(out, cap, "%s %dx%d at=%d,%d", IMAGE_NAME[v->lcodec], v->lw, v->lh,
                     v->lx, v->ly);
    if (v->lsw && n > 0 && (size_t)n < cap)
        snprintf(out + n, cap - (size_t)n, " size=%dx%d", v->lsw, v->lsh);
}

/* `picture <target>`: the glass as QOI, a cells layer as its grid, a video
 * layer as the frame it shows, on the asker's `out`, the line saying what
 * follows and then the bytes. An answer, so it waits its turn rather than
 * being dropped. */
static void cmd_picture(Wall *w, Session *from, char **words, int n)
{
    char head[160], tag[40] = "";
    Session *x;
    int cw, ch, quality;
    void *pic = NULL;
    size_t len = 0;
    const char *name;
    if (!capture_args(w, from, "picture", words, n, &x, &cw, &ch, &quality, NULL, NULL))
        return;
    name = x ? x->name : "glass";
    if (w->id[0])
        snprintf(tag, sizeof tag, " id=%s", w->id);
    if (!x) {
        /* Read back here, where the GPU is, and made QOI or JPEG by the
         * pool. */
        uint8_t *px;
        Job *j;
        glass_fit(w->s, &cw, &ch);
        px = glass_rgba(w->s, cw, ch);
        if (!px || !(j = qoi_job(px, cw, ch, 0, "glass", tag))) {
            wsay(from, "error picture glass: not offered");
            return;
        }
        j->quality = quality;
        snprintf(j->head, sizeof j->head, "picture glass %s %dx%d ", quality ? "jpeg" : "qoi",
                 cw, ch);
        put_later(from, j);
        return;
    } else if (x->is_cells) {
        LGrid *g = w->s->cells[x->z];
        pic = g ? lgrid_picture(g, &len) : NULL;
        snprintf(head, sizeof head, "picture %s grid %dx%d %zu%s", name,
                 g ? g->cols : w->s->cols, g ? g->rows : w->s->rows, len, tag);
    } else if (x->is_video) {
        if (x->shown && !x->vlast && w->above[x->z] == x && w->s->pics[x->z].on) {
            data_picture(w, from, x, tag);
            return;
        }
        if (!x->vlast || w->above[x->z] != x || !w->s->pics[x->z].on) {
            wsay(from, "error picture %s: nothing shown", name);
            return;
        }
        len = x->vlastlen;
        {
            char fwords[80];
            frame_words(x, fwords, sizeof fwords);
            snprintf(head, sizeof head, "picture %s %s %zu%s", name, fwords, len, tag);
        }
        if (!put_blob(from, head, (const char *)x->vlast, len))
            wsay(from, "error picture %s: too far behind", name);
        return;
    } else {
        pic = grid_picture(w->s, &len);
        snprintf(head, sizeof head, "picture %s grid %dx%d %zu%s", name, w->s->cols,
                 w->s->rows, len, tag);
    }
    if (!pic) {
        wsay(from, "error picture %s: not offered", name);
        return;
    }
    if (!put_blob(from, head, pic, len))
        wsay(from, "error picture %s: too far behind", name);
    free(pic);
}

static void mirror_stop(Mirror *m)
{
    free(m->mshows);
    free(m->mcolours);
    memset(m, 0, sizeof *m);
}

/* A session's mirrors that are due: the glass looked at again and sent if
 * it changed, a cells layer's grid or what changed in it. A mirror whose
 * reader still has a frame waiting is not sent the next, and a mirror of a
 * session that has gone stops. A video layer's are sent as its frames
 * come, by video_mirror. */
static void mirror_tick(Wall *w, Session *s, double now)
{
    int k;
    for (k = 0; k < MIRRORS; k++) {
        Mirror *m = &s->mir[k];
        Session *x = NULL;
        char head[160];
        if (!m->target[0])
            continue;
        if (strcmp(m->target, "glass")) {
            for (x = w->sessions; x; x = x->next)
                if (x->state == S_OPEN && !strcmp(x->name, m->target))
                    break;
            if (!x) {
                mirror_stop(m);
                continue;
            }
            if (x->is_video)
                continue;
        }
        if (now < m->due || s->out_fd < 0 || s->qhead)
            continue;
        m->due = now - m->due < m->every ? m->due + m->every : now + m->every;
        if (!x) {
            /* Read back here; summed, and sent only if it changed, by the
             * pool, whose place in the queue holds the next look back. */
            int cw = m->w ? m->w : w->s->width, ch = m->h ? m->h : w->s->height;
            uint8_t *px;
            Job *j;
            glass_fit(w->s, &cw, &ch);
            px = glass_rgba(w->s, cw, ch);
            if (!px || !(j = qoi_job(px, cw, ch, 0, "glass", "")))
                continue;
            j->mirror = k;
            j->sum_it = 1;
            j->quality = m->quality;
            snprintf(j->head, sizeof j->head, "mirror glass %s %dx%d ",
                     m->quality ? "jpeg" : "qoi", cw, ch);
            put_later(s, j);
        } else {
            size_t len;
            int full;
            char *frame = mirror_frame(w->s, m, &len, &full);
            if (!frame)
                continue;
            if (full)
                snprintf(head, sizeof head, "mirror %s grid %dx%d %zu", x->name, w->s->cols,
                         w->s->rows, len);
            else
                snprintf(head, sizeof head, "mirror %s diff %zu", x->name, len);
            if (put_blob(s, head, frame, len))
                mirror_sent(w->s, m);
            free(frame);
        }
    }
}

/* A frame a video layer has just taken, passed on as it came to every
 * mirror of it whose reader has nothing waiting and whose `every` has
 * run. */
static void video_mirror(Session *v, const uint8_t *data, size_t len, double now)
{
    Session *s;
    int k;
    char head[200], fwords[80], hold[16];
    frame_words(v, fwords, sizeof fwords);
    if (v->fhold < 0)
        snprintf(hold, sizeof hold, "still");
    else
        snprintf(hold, sizeof hold, "%d", v->fhold);
    snprintf(head, sizeof head, "mirror %s %s hold=%s %zu", v->name, fwords, hold, len);
    for (s = v->w->sessions; s; s = s->next) {
        if (s->state != S_OPEN || s->out_fd < 0)
            continue;
        for (k = 0; k < MIRRORS; k++) {
            Mirror *m = &s->mir[k];
            if (strcmp(m->target, v->name) || s->qhead || now < m->due)
                continue;
            m->due = now + m->every;
            put_blob(s, head, (const char *)data, len);
        }
    }
}

/* The glass streamed raw, and the rest of what is made of it. */
#include "outputs.c"

/* `mirror <target> [every=] [size=]`, and `mirror <target> off`. */
static void cmd_mirror(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    double every = -1;
    int cw, ch, quality, off = 0, k, slot = -1, running = 0;
    Session *x, *y;
    Mirror *m;
    for (k = 2; k < n; k++)
        if (!strncmp(words[k], "stream=", 7)) {
            cmd_stream(w, from, words, n);
            return;
        }
    if (cmd_stream_off(w, from, words, n))
        return;
    if (!capture_args(w, from, "mirror", words, n, &x, &cw, &ch, &quality, &every, &off))
        return;
    for (k = 0; k < MIRRORS; k++)
        if (!strcmp(from->mir[k].target, words[1]))
            slot = k;
    if (off) {
        if (slot >= 0)
            mirror_stop(&from->mir[slot]);
        if (!strcmp(words[1], "glass"))
            outputs_stop(w->s, from, NULL);
        return;
    }
    for (y = w->sessions; y; y = y->next)
        for (k = 0; y->state == S_OPEN && k < MIRRORS; k++)
            running += y->mir[k].target[0] != 0;
    for (k = 0; slot < 0 && k < MIRRORS; k++)
        if (!from->mir[k].target[0])
            slot = k;
    if (slot < 0 || (!from->mir[slot].target[0] && running >= MAX_MIRRORS)) {
        wsay(from, "error mirror %s: %d mirrors already", plain(words[1], said, sizeof said),
             slot < 0 ? MIRRORS : MAX_MIRRORS);
        return;
    }
    m = &from->mir[slot];
    mirror_stop(m);
    snprintf(m->target, sizeof m->target, "%s", words[1]);
    m->every = every >= 0 ? every : x && x->is_video ? 0 : MIRROR_EVERY;
    m->due = now_ms();
    if (!x) {
        m->w = cw;
        m->h = ch;
        m->quality = quality;
    }
}

static void cmd_follow(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    (void)w;
    if (n < 2) {
        wsay(from, "error follow no value");
        return;
    }
    if (!strcmp(words[1], "said"))
        from->follow = 1;
    else if (!strcmp(words[1], "in"))
        from->follow = 2;
    else if (!strcmp(words[1], "off"))
        from->follow = 0;
    else
        wsay(from, "error follow %s: unknown option", plain(words[1], said, sizeof said));
}

static void cmd_measures(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    char *end;
    long ms;
    if (n < 2) {
        wsay(from, "error measures no value");
        return;
    }
    ms = strtol(words[1], &end, 10);
    if (end == words[1] || *end) {
        wsay(from, "error measures %s: not a number", plain(words[1], said, sizeof said));
        return;
    }
    if (ms == 0) {
        from->measures = 0;
        from->measure_due = 0;
        return;
    }
    from->measures = (ms < 200 ? 200 : ms > 60000 ? 60000 : (double)ms) / 1e3;
    from->measure_due = now_ms() + from->measures * 1e3;
    from->seen = sample_of(w->s);
}

/* -- report and sessions ------------------------------------------------- */

/* -- a video layer: its frame lines, and taking it down ------------------ */

/* Take layer `z` down, and show the wall without it. */
/* Every mirror of video layer `v` told it has been taken down, so what
 * shows it goes transparent too. Kept, never dropped, and after every
 * frame already queued for it. */
static void video_mirror_clear(Wall *w, Session *v)
{
    Session *s;
    int k;
    char line[64];
    snprintf(line, sizeof line, "mirror %s clear", v->name);
    for (s = w->sessions; s; s = s->next)
        for (k = 0; s->state == S_OPEN && k < MIRRORS; k++)
            if (!strcmp(s->mir[k].target, v->name))
                emit_keep(s, line);
}

static void video_clear(Wall *w, int z)
{
    Screen *s = w->s;
    if (!s->pics[z].on)
        return;
    s->pics[z].on = 0;
    if (w->above[z])
        video_mirror_clear(w, w->above[z]);
    redraw(s);
}

/* Every session above the base told the new glass and `repaint`, their
 * layers having been taken down under them. */
static void wall_above_repaint(Wall *w)
{
    Session *s;
    char screen[320];
    screen_line(w, screen, sizeof screen);
    for (s = w->sessions; s; s = s->next)
        if (s->state == S_OPEN && s->is_video) {
            emit(s, screen);
            emit(s, "repaint");
            video_mirror_clear(w, s);
        }
}

/* A whole number of at most `most` digits, as a count or a size is. */
static int digits(const char *t, size_t most)
{
    size_t i;
    for (i = 0; t[i]; i++)
        if (t[i] < '0' || t[i] > '9' || i >= most)
            return 0;
    return i > 0;
}

/* `frame <codec> <w>x<h> [at=<x>,<y>] [hold=<ms>] <bytes>`: what follows,
 * and whether it is taken. The bytes are read next whatever is wrong with
 * the rest of the line, so long as it says how many, and are let go when
 * the frame is refused. */
/* `<w>x<h>`, each side 1..FRAME_SIDE, or 0 when it is not one. */
static int side_pair(char *word, int *w, int *h)
{
    char *x = strchr(word, 'x');
    *w = *h = 0;
    if (x) {
        *x = 0;
        if (digits(word, 4) && digits(x + 1, 4)) {
            *w = atoi(word);
            *h = atoi(x + 1);
        }
        *x = 'x';
    }
    return *w >= 1 && *h >= 1 && *w <= FRAME_SIDE && *h <= FRAME_SIDE;
}

/* `<x>,<y>`, each an optional `-` and ASCII digits, within 65535 of 0
 * either way, into x and y; 0, and neither set, when it is not one. */
static int at_pair(const char *word, int *x, int *y)
{
    const char *c = word;
    long v[2];
    int i;
    for (i = 0; i < 2; i++) {
        int neg = *c == '-', n = 0;
        long a = 0;
        c += neg;
        for (; *c >= '0' && *c <= '9' && n < 6; c++, n++)
            a = a * 10 + (*c - '0');
        if (!n || a > 65535)
            return 0;
        v[i] = neg ? -a : a;
        if (!i && *c++ != ',')
            return 0;
    }
    if (*c)
        return 0;
    *x = (int)v[0];
    *y = (int)v[1];
    return 1;
}

static void cmd_frame(Wall *w, Session *from, char **words, int n)
{
    char said[48];
    int i, codec = IMG_QOI, fw = 0, fh = 0, fx = 0, fy = 0, sw = 0, sh = 0, hold = HOLD_MS;
    from->want = 0;
    from->skip = 1;
    snprintf(from->fid, sizeof from->fid, "%s", w->id);
    if (n < 2 || !digits(words[n - 1], 18)) {
        wsay(from, "error frame no count");
        return;
    }
    from->want = strtoull(words[n - 1], NULL, 10);
    if (from->want > FRAME_MAX) {
        wsay(from, "error frame %s: too big", plain(words[n - 1], said, sizeof said));
        return;
    }
    if (n >= 3 && (codec = image_codec(words[1])) < 0) {
        wsay(from, "error frame %s: not offered", plain(words[1], said, sizeof said));
        return;
    }
    if (n < 4) {
        wsay(from, "error frame no size");
        return;
    }
    if (!side_pair(words[2], &fw, &fh)) {
        wsay(from, "error frame %s: bad size", plain(words[2], said, sizeof said));
        return;
    }
    for (i = 3; i < n - 1; i++) {
        const char *v = strchr(words[i], '=');
        int ok = 0;
        if (v && !strncmp(words[i], "at=", 3)) {
            ok = at_pair(v + 1, &fx, &fy);
        } else if (v && !strncmp(words[i], "size=", 5)) {
            ok = side_pair(words[i] + 5, &sw, &sh);
        } else if (v && !strncmp(words[i], "hold=", 5)) {
            ok = !strcmp(v + 1, "still") || (digits(v + 1, 8) && atoi(v + 1) > 0);
            hold = strcmp(v + 1, "still") ? atoi(v + 1) : -1;
        } else {
            wsay(from, "error frame %s: unknown option", plain(words[i], said, sizeof said));
            return;
        }
        if (!ok) {
            wsay(from, "error frame %s: bad value", plain(words[i], said, sizeof said));
            return;
        }
    }
    from->skip = 0;
    from->fcodec = codec;
    from->fw = fw;
    from->fh = fh;
    from->fx = fx;
    from->fy = fy;
    from->fsw = sw;
    from->fsh = sh;
    from->fhold = hold;
}

/* The box a video layer's stream is fitted to, from its next frame: `box
 * at=<x>,<y> size=<w>x<h>` in the glass's pixels, `at` 0,0 when left out,
 * or `box` alone for the whole glass again. It is kept through a change
 * of glass, and what falls off the glass is not drawn. */
static void cmd_box(Session *from, char **words, int n)
{
    char said[48];
    int i, x = 0, y = 0, bw = 0, bh = 0;
    for (i = 1; i < n; i++) {
        int ok;
        if (!strncmp(words[i], "at=", 3)) {
            ok = at_pair(words[i] + 3, &x, &y);
        } else if (!strncmp(words[i], "size=", 5)) {
            ok = side_pair(words[i] + 5, &bw, &bh);
        } else {
            wsay(from, "error box %s: unknown option", plain(words[i], said, sizeof said));
            return;
        }
        if (!ok) {
            wsay(from, "error box %s: bad value", plain(words[i], said, sizeof said));
            return;
        }
    }
    if (n > 1 && !bw) {
        wsay(from, "error box no size");
        return;
    }
    from->data_box[0] = x;
    from->data_box[1] = y;
    from->data_box[2] = bw;
    from->data_box[3] = bh;
}

/* When the painter started, in Unix seconds. */
static time_t painter_started;

static void session_report(Session *from)
{
    Wall *w = from->w;
    char caps[512], names[200], screen[320], taken[64], hw[64], hws[72] = "", up[32] = "";
    char streams[24] = "";
    long booted = machine_booted();
    wsay(from, "hello v1 line");
    if (booted > 0)
        snprintf(up, sizeof up, " booted=%ld", booted);
    wsay(from, "receiver bitwall-paint version=%s started=%ld%s",
         BITWALL_VERSION, (long)painter_started, up);
    codecs_said(taken, sizeof taken, 0);
    codecs_said(hw, sizeof hw, 1);
    if (hw[0])
        snprintf(hws, sizeof hws, " hw=%s", hw);
    if (w->s->gpu)
        snprintf(streams, sizeof streams, " streams=%d", STREAMS);
    snprintf(caps, sizeof caps,
             "caps area font grid meta blank want presence repaint watching ping "
             "detach close screen sessions follow measures stats picture mirror%s frame clear "
             "data align box kitty%s layers=%d types=cells,video above=%s codecs=%s%s images=%s "
             "controllers=%d%s",
             w->s->gpu ? " stream" : "", glass_offered(w->s) ? " glass" : "", LAYERS,
             w->s->gpu ? "video,cells" : "video", taken, hws, IMAGES_TAKEN, CONTROLLERS, streams);
    wsay(from, "%s", caps);
    measure_names(measures_offered(), names, sizeof names);
    {
        size_t i;
        for (i = 0; names[i]; i++)
            if (names[i] == ',')
                names[i] = ' ';
    }
    wsay(from, "measures %s", names);
    wsay(from, "%s", w->s->gpu ? "pictures " PICTURES_MADE " grid" : "pictures " PICTURES_MADE);
    if (own_grid(w, from)) {
        face_swap(w->s, w->s->cells[from->z]->face);
        screen_line(w, screen, sizeof screen);
        face_swap(w->s, w->s->cells[from->z]->face);
        wsay(from, "%s", screen);
    } else {
        wsay(from, "%s", screen_line(w, screen, sizeof screen));
    }
    wsay(from, "accepts");
    if (w->base && w->base->state == S_OPEN)
        smeta_say(w->base, from, 1, "");
    {
        /* Each layer above the base says its own, tagged with its session. */
        Session *x;
        for (x = w->sessions; x; x = x->next)
            if (x->state == S_OPEN && x->role == ROLE_LAYER && x != w->base)
                smeta_say(x, from, 1, x->name);
    }
    say_stats(from);
    say_presence(from);
    knobs_settle();
    wsay(from, "threads cores=%d workers=%d decoder=%d software=%d nice=%d", machine_cores(),
         pool.started ? pool.size : pool_want, decoder_threads, decoder_soft, decoder_nice);
    wsay(from, "display %s", display_word(w));
    wsay(from, "power %s", power_word(w));
    wsay(from, "watching %d", watched(w));
    if (own_grid(w, from))
        wsay(from, "area %dx%d %d", w->s->cells[from->z]->cols, w->s->cells[from->z]->rows,
             w->s->cells[from->z]->face->size);
    else
        wsay(from, "area %dx%d %d", w->s->cols, w->s->rows, w->s->size);
}

static const char *role_word(Session *s)
{
    return s->role == ROLE_CONTROLLER ? "controller" : "layer";
}

/* Whether a value is an instance id: one to sixty-four lower case
 * letters, digits and `-`, starting with a letter or a digit. */
static int instance_ok(const char *v)
{
    size_t n = strlen(v), i;
    if (n < 1 || n > 64 || v[0] == '-')
        return 0;
    for (i = 0; i < n; i++)
        if (!((v[i] >= 'a' && v[i] <= 'z') || (v[i] >= '0' && v[i] <= '9') || v[i] == '-'))
            return 0;
    return 1;
}

static void session_sessions(Session *from)
{
    Wall *w = from->w;
    Session *s;
    int count = 0;
    char place[12], type[8];
    for (s = w->sessions; s; s = s->next) {
        if (s->state != S_OPEN)
            continue;
        count++;
        if (s->role == ROLE_LAYER)
            snprintf(place, sizeof place, "%d", s->z);
        else
            snprintf(place, sizeof place, "-");
        snprintf(type, sizeof type, "%s", s->type[0] ? s->type : "-");
        /* The instance a controller started it under, from its open, so a
         * controller finds what it started without reading every meta. */
        if (s->instance[0])
            wsay(from, "session %s %s %s %s instance=%s", s->name, role_word(s), place, type,
                 s->instance);
        else
            wsay(from, "session %s %s %s %s", s->name, role_word(s), place, type);
    }
    wsay(from, "sessions %d", count);
}

/* -- the command table --------------------------------------------------- */

static void echo_in(Wall *w, Session *from, char **words, int n)
{
    char raw[MAX_APC + 2];
    size_t at = 0;
    int i;
    for (i = 0; i < n && at < MAX_APC; i++)
        at += (size_t)snprintf(raw + at, sizeof raw - at, "%s%s", i ? " " : "", words[i]);
    echo(w, from, '<', raw);
}

static void session_command(Wall *w, Session *from, char **words, int n, const char *line)
{
    const char *name;
    char said[48];
    int i, k, bad_id = 0, is_ctl, is_layer, is_base;
    if (n < 1)
        return;
    name = words[0];
    echo_in(w, from, words, n);
    w->id[0] = 0;
    w->to = from;
    if (!strcmp(name, "meta")) {
        session_meta(w, from, words, n, line);
        w->id[0] = 0;
        w->to = NULL;
        return;
    }
    for (i = 1; i < n; i++) {
        if (strncmp(words[i], "id=", 3) != 0)
            continue;
        if (id_ok(words[i] + 3))
            snprintf(w->id, sizeof w->id, "%s", words[i] + 3);
        else
            bad_id = 1;
    }
    k = 1;
    for (i = 1; i < n; i++)
        if (strncmp(words[i], "id=", 3) != 0)
            words[k++] = words[i];
    n = k;
    if (bad_id) {
        w->id[0] = 0;
        wsay(from, "error %s id: not a token", plain(name, said, sizeof said));
        w->to = NULL;
        return;
    }
    is_ctl = from->role == ROLE_CONTROLLER;
    is_layer = from->role == ROLE_LAYER;
    is_base = from->is_base;
#define NEEDROLE(cond) do { if (!(cond)) { \
        wsay(from, "error %s not-for-role", plain(name, said, sizeof said)); \
        goto done; } } while (0)
    if (!strcmp(name, "report"))
        session_report(from);
    else if (!strcmp(name, "sessions"))
        session_sessions(from);
    else if (!strcmp(name, "ping"))
        wsay(from, "pong");
    else if (!strcmp(name, "pong"))
        cmd_pong(from, now_ms());
    else if (!strcmp(name, "close") || !strcmp(name, "detach")) {
        w->id[0] = 0;
        session_end(from, "asked");
    } else if (!strcmp(name, "font")) {
        NEEDROLE((is_base && !from->is_video) || is_ctl || from->is_cells);
        if (from->is_cells)
            layer_font(w, from, words, n, 0);
        else
            cmd_font(w, from, words, n);
    } else if (!strcmp(name, "grid")) {
        NEEDROLE((is_base && !from->is_video) || is_ctl || from->is_cells);
        if (from->is_cells)
            layer_font(w, from, words, n, 1);
        else
            cmd_grid(w, from, words, n);
    } else if (!strcmp(name, "blank")) {
        NEEDROLE(is_base || is_ctl);
        cmd_blank(w, from, words, n);
    } else if (!strcmp(name, "want")) {
        NEEDROLE(is_layer);
        cmd_want(w, from, words, n);
    } else if (!strcmp(name, "frame")) {
        NEEDROLE(from->is_video && !from->is_cells);
        cmd_frame(w, from, words, n);
    } else if (!strcmp(name, "align")) {
        NEEDROLE(from->is_video && !from->is_cells);
        cmd_align(from, words, n);
    } else if (!strcmp(name, "box")) {
        NEEDROLE(from->is_video && !from->is_cells);
        cmd_box(from, words, n);
    } else if (!strcmp(name, "clear")) {
        NEEDROLE(from->is_video);
        if (w->above[from->z] == from && from->is_cells && w->s->cells[from->z]) {
            lgrid_wipe(w->s->cells[from->z]);
            redraw(w->s);
        } else if (w->above[from->z] == from) {
            video_clear(w, from->z);
        }
    } else if (!strcmp(name, "glass")) {
        NEEDROLE(is_ctl);
        cmd_glass(w, from, words, n);
    } else if (!strcmp(name, "presence")) {
        NEEDROLE(is_ctl);
        cmd_presence(w, from, words, n);
    } else if (!strcmp(name, "stats")) {
        NEEDROLE(is_ctl);
        cmd_stats(w, from, words, n);
    } else if (!strcmp(name, "picture")) {
        cmd_picture(w, from, words, n);
    } else if (!strcmp(name, "mirror")) {
        cmd_mirror(w, from, words, n);
    } else if (!strcmp(name, "follow")) {
        NEEDROLE(is_ctl);
        cmd_follow(w, from, words, n);
    } else if (!strcmp(name, "measures")) {
        NEEDROLE(is_ctl);
        cmd_measures(w, from, words, n);
    } else if (w->id[0]) {
        wsay(from, "error %s unknown command", plain(name, said, sizeof said));
    }
#undef NEEDROLE
done:
    w->id[0] = 0;
    w->to = NULL;
}

/* What a kitty string came to, said on the base's `out`: a refusal, or
 * with no reason the answer to a=q. Alone on stdin there is nobody to
 * tell. */
static void kitty_say(Screen *s, uint32_t id, const char *why)
{
    Wall *w = s->wall;
    char line[96];
    if (!w || !w->base || w->base->state != S_OPEN)
        return;
    if (why)
        snprintf(line, sizeof line, "error kitty i=%u: %s", (unsigned)id, why);
    else
        snprintf(line, sizeof line, "kitty i=%u OK", (unsigned)id);
    emit(w->base, line);
}

/* The base's own commands, from its `in` via feed()/apc(). */
static void base_command(Wall *w, char **words, int n, const char *line)
{
    if (w->base && w->base->state == S_OPEN)
        session_command(w, w->base, words, n, line);
}

/* One command string off a controller's or a video layer's `in`. Only ours
 * are acted on; anything else is not painted, and is let go. */
static void controller_apc(Session *s, const uint8_t *p, size_t n)
{
    char text[MAX_APC + 1], whole[MAX_APC + 1];
    char *w[MAX_PARAMS], *save = NULL, *tok;
    int nw = 0;
    if (n > MAX_APC)
        return;
    memcpy(text, p, n);
    text[n] = 0;
    memcpy(whole, text, n + 1);
    for (tok = strtok_r(text, " \t\r\n", &save); tok && nw < MAX_PARAMS;
         tok = strtok_r(NULL, " \t\r\n", &save))
        w[nw++] = tok;
    if (nw < 2 || strcmp(w[0], "bitwall") != 0)
        return;
    session_command(s->w, s, w + 1, nw - 1, rest_after(whole, 1));
}

/* A controller's bytes into commands: complete APC strings are acted on,
 * anything else is dropped, and the start of a command the read cut short
 * waits for the rest. */
static void controller_parse(Session *s)
{
    size_t i = 0;
    while (i < s->clen) {
        size_t j;
        int found = 0;
        if (s->cbuf[i] != 0x1b) {
            i++;                /* not the start of a string: dropped */
            continue;
        }
        if (i + 1 >= s->clen)
            break;              /* a lone ESC at the end; wait for more */
        if (s->cbuf[i + 1] != '_') {
            i += 2;
            continue;
        }
        for (j = i + 2; j + 1 < s->clen; j++)
            if (s->cbuf[j] == 0x1b && s->cbuf[j + 1] == '\\') {
                found = 1;
                break;
            }
        if (!found) {
            if (s->clen - i > MAX_APC + 2)
                i = s->clen;    /* too long to be a command; drop it */
            break;
        }
        controller_apc(s, s->cbuf + i + 2, j - (i + 2));
        memmove(s->cbuf, s->cbuf + j + 2, s->clen - (j + 2));
        s->clen -= j + 2;
        i = 0;
    }
    if (i > 0 && i <= s->clen) {
        memmove(s->cbuf, s->cbuf + i, s->clen - i);
        s->clen -= i;
    }
}

/* The read is taken in slices that fit cbuf. After each parse only an
 * unfinished command of at most MAX_APC + 2 bytes is left, so every slice
 * adds at least a few bytes. */
static void controller_feed(Session *s, const uint8_t *p, size_t n)
{
    while (n > 0) {
        size_t room = sizeof s->cbuf - s->clen;
        size_t take = n < room ? n : room;
        memcpy(s->cbuf + s->clen, p, take);
        s->clen += take;
        p += take;
        n -= take;
        controller_parse(s);
    }
}

/* -- a video layer: reading its `in` ------------------------------------ */

/* One frame's payload, whole: copied, with what its line said, and handed
 * to the pool to decode, at the end of the layer's queue; frames_flow puts
 * it up in its turn. The main thread never waits on a decode. */
static void video_frame(Session *s, const uint8_t *data, size_t len)
{
    Job *j = calloc(1, sizeof *j);
    if (j && !(j->data = malloc(len ? len : 1))) {
        free(j);
        j = NULL;
    }
    if (!j) {
        Wall *w = s->w;
        snprintf(w->id, sizeof w->id, "%s", s->fid);
        w->to = s;
        wsay(s, "error frame %s: does not decode", IMAGE_NAME[s->fcodec]);
        w->id[0] = 0;
        w->to = NULL;
        return;
    }
    memcpy(j->data, data, len);
    j->kind = JOB_FRAME;
    j->len = len;
    j->codec = s->fcodec;
    j->fw = s->fw;
    j->fh = s->fh;
    j->fx = s->fx;
    j->fy = s->fy;
    j->fsw = s->fsw;
    j->fsh = s->fsh;
    j->fhold = s->fhold;
    snprintf(j->fid, sizeof j->fid, "%s", s->fid);
    j->owner = s;
    if (s->jtail)
        s->jtail->after = j;
    else
        s->jhead = j;
    s->jtail = j;
    s->frames_out++;
    pool_submit(j);
}

static void video_feed(Session *s);

/* A frame back from the pool, in its turn, put up on its session's layer,
 * or refused with the id its line carried. */
static void frame_up(Session *s, Job *j)
{
    Wall *w = s->w;
    double t0 = now_ms();
    const char *why = NULL;
    char said[48];
    if (s->state != S_OPEN)
        return;
    if (!j->rgba || j->pw != j->fw || j->ph != j->fh)
        why = "does not decode";
    else if (pic_set(w->s, s->z, j->rgba, j->fw, j->fh, j->fx, j->fy, j->fsw ? j->fsw : j->fw,
                     j->fsw ? j->fsh : j->fh, j->fhold < 0 ? 1e300 : now_ms() + j->fhold) < 0)
        why = "too big for this glass";
    if (why) {
        snprintf(w->id, sizeof w->id, "%s", j->fid);
        w->to = s;
        snprintf(said, sizeof said, "%s: %s", IMAGE_NAME[j->codec], why);
        wsay(s, "error frame %s", said);
        w->id[0] = 0;
        w->to = NULL;
    } else {
        s->n_frames++;
        s->n_decode += j->ms + now_ms() - t0;
        s->n_upload += now_ms() - t0;
        redraw(w->s);
        if (s->is_base)
            video_base_frame(w);
        free(s->vlast);
        s->vlast = j->data;
        j->data = NULL;
        s->vlastlen = j->len;
        s->lcodec = j->codec;
        s->lw = j->fw;
        s->lh = j->fh;
        s->lx = j->fx;
        s->ly = j->fy;
        s->lsw = j->fsw;
        s->lsh = j->fsh;
        video_mirror(s, s->vlast, s->vlastlen, now_ms());
    }
}

/* A layer's queue let out from the front for as long as what is there is
 * back from the pool: frames put up and answers said, in the order sent.
 * Then the rest of what the layer sent is read on. */
static void frames_flow(Session *s)
{
    while (s->jhead && s->jhead->back) {
        Job *j = s->jhead;
        s->jhead = j->after;
        if (!s->jhead)
            s->jtail = NULL;
        if (j->kind == JOB_FRAME) {
            s->frames_out--;
            frame_up(s, j);
        } else if (s->state == S_OPEN) {
            emit(s, j->say);
        }
        job_free(j);
    }
    s->vin.held = 0;
    video_feed(s);
}

/* A picture or a mirror frame back from the pool, into the place it held
 * in its session's `out`: its bytes under their line; where it could not
 * be made or does not fit, a picture's error in its stead; and a mirror's
 * look that has not changed since the last sent, nothing. */
static void picture_done(Job *j)
{
    Session *s = j->owner;
    Mirror *m = j->mirror >= 0 ? &s->mir[j->mirror] : NULL;
    char head[300], line[160];
    size_t hl;
    char *buf;
    if (m && strcmp(m->target, "glass"))
        m = NULL;
    if (j->q && j->mirror >= 0 && (!m || (m->sent && j->sum == m->sum))) {
        put_filled(s, j->item, NULL, 0);
        return;
    }
    snprintf(head, sizeof head, "%s%d%s", j->head, j->qlen, j->tag);
    hl = strlen(head);
    if (j->q && s->queued + hl + 1 + (size_t)j->qlen <= OUT_QUEUE
        && (buf = malloc(hl + 1 + (size_t)j->qlen))) {
        memcpy(buf, head, hl);
        buf[hl] = '\n';
        memcpy(buf + hl + 1, j->q, (size_t)j->qlen);
        put_filled(s, j->item, buf, hl + 1 + (size_t)j->qlen);
        free(buf);
        if (m) {
            m->sum = j->sum;
            m->sent = 1;
        }
        return;
    }
    if (j->mirror >= 0) {
        put_filled(s, j->item, NULL, 0);
        return;
    }
    snprintf(line, sizeof line, "error picture %s: %s%s", j->name,
             j->q ? "too far behind" : "not offered", j->tag);
    hl = strlen(line);
    line[hl] = '\n';
    put_filled(s, j->item, line, hl + 1);
    line[hl] = 0;
    echo(s->w, s, '>', line);
}

/* Every job the pool has finished, taken back on the main thread: a
 * picture put in the place it held, a frame marked back and its layer's
 * queue let out as far as it goes; and one whose session has gone, let
 * go. */
static void pool_deliver(void)
{
    Job *j = pool_take();
    while (j) {
        Job *next = j->next;
        pool.out--;
        if (j->owner && j->kind == JOB_FRAME) {
            j->back = 1;
            frames_flow(j->owner);
        } else {
            if (j->owner)
                picture_done(j);
            job_free(j);
        }
        j = next;
    }
}

/* A video layer's bytes: commands acted on as they come, each frame's
 * payload gathered whole and put up, and anything else let go. */
static void video_feed(Session *s)
{
    uint8_t *b = s->vbuf;
    size_t p = 0, len = s->vlen;
    int held = 0;
    while (p < len && s->state == S_OPEN) {
        if (s->want) {
            size_t have = len - p;
            if (s->skip) {
                size_t k = have < s->want ? have : (size_t)s->want;
                p += k;
                s->want -= k;
                continue;
            }
            if (have < s->want)
                break;          /* the rest of the payload is still coming */
            video_frame(s, b + p, (size_t)s->want);
            p += (size_t)s->want;
            s->want = 0;
            continue;
        }
        if (b[p] != 0x1b) {
            const uint8_t *esc = memchr(b + p, 0x1b, len - p);
            p = esc ? (size_t)(esc - b) : len;
            continue;
        }
        if (p + 1 >= len)
            break;
        if (b[p + 1] != '_') {
            p++;
            continue;
        }
        {
            size_t j;
            int found = 0;
            for (j = p + 2; j + 1 < len; j++)
                if (b[j] == 0x1b && b[j + 1] == '\\') {
                    found = 1;
                    break;
                }
            if (!found) {
                if (len - p > MAX_APC + 2)
                    p += 2;     /* too long to be a command; let go */
                else
                    break;
                continue;
            }
            if (s->jhead) {
                /* Frames are with the pool: read on only to the next
                 * frame's line, while there is room for its frame, and
                 * keep what that line is answered for after them. */
                if (s->frames_out >= FRAMES_AHEAD || j - (p + 2) < 14
                    || memcmp(b + p + 2, "bitwall frame ", 14)) {
                    held = 1;
                    break;
                }
                s->say_later = 1;
                controller_apc(s, b + p + 2, j - (p + 2));
                s->say_later = 0;
            } else {
                controller_apc(s, b + p + 2, j - (p + 2));
            }
            p = j + 2;
        }
    }
    s->vin.held = held;
    if (p < len)
        memmove(b, b + p, len - p);
    s->vlen = len - p;
}

/* -- a cells layer: reading its `in` ------------------------------------ */

/* A cells layer's pen taking an SGR: the colours as the base's pen takes
 * them, and an alpha for each besides. `38;6;r;g;b;a` and `48;6;r;g;b;a`
 * are a colour with its alpha, which the base's pen is given as the
 * truecolour it is; every other colour is opaque; and a ground left unset,
 * or set back with `49` or a reset, is none, so the layer shows through. */
static void lpen_take(LGrid *g, const int *v, int n)
{
    int rest[MAX_PARAMS], nr = 0, i = 0, k, len;
    while (i < n && nr < MAX_PARAMS) {
        int p = v[i];
        if ((p == 38 || p == 48) && i + 5 < n && v[i + 1] == 6) {
            if (p == 38)
                g->ink_a = (uint8_t)(v[i + 5] & 255);
            else
                g->ground_a = (uint8_t)(v[i + 5] & 255);
            if (nr + 5 <= MAX_PARAMS) {
                rest[nr++] = p;
                rest[nr++] = 2;
                rest[nr++] = v[i + 2];
                rest[nr++] = v[i + 3];
                rest[nr++] = v[i + 4];
            }
            i += 6;
            continue;
        }
        len = 1;
        if (p == 0) {
            g->ink_a = 255;
            g->ground_a = 0;
        } else if (p == 39 || (p >= 30 && p <= 37) || (p >= 90 && p <= 97)) {
            g->ink_a = 255;
        } else if (p == 49) {
            g->ground_a = 0;
        } else if ((p >= 40 && p <= 47) || (p >= 100 && p <= 107)) {
            g->ground_a = 255;
        } else if ((p == 38 || p == 48) && i + 1 < n) {
            len = v[i + 1] == 5 ? 3 : v[i + 1] == 2 ? 5 : 2;
            if (p == 38)
                g->ink_a = 255;
            else
                g->ground_a = 255;
        }
        for (k = 0; k < len && i < n && nr < MAX_PARAMS; k++)
            rest[nr++] = v[i++];
    }
    pen_take(&g->pen, rest, nr);
}

/* The alphas a cell is drawn with now: reversed, the ink is the ground's. */
static void lgrid_alphas(const LGrid *g, uint8_t *ink, uint8_t *ground)
{
    uint8_t fa = g->ink_a, ba = g->pen.bg_set ? g->ground_a : 0;
    *ink = g->pen.reverse ? ba : fa;
    *ground = g->pen.reverse ? fa : ba;
}

static void lgrid_put(LGrid *g, int x, int y, uint32_t cp, uint8_t ia, uint8_t ga)
{
    size_t i = (size_t)y * g->cols + x;
    uint8_t *c = g->rgba + i * 8;
    g->cp[i] = cp;
    c[0] = g->pen.ink.r;
    c[1] = g->pen.ink.g;
    c[2] = g->pen.ink.b;
    c[3] = ia;
    c[4] = g->pen.ground.r;
    c[5] = g->pen.ground.g;
    c[6] = g->pen.ground.b;
    c[7] = ga;
}

/* Text at a cells layer's cursor, as the base's is painted: a character a
 * cell, a wide one two, and nothing past the grid's edges. */
static void lgrid_text(Screen *s, LGrid *g, const uint8_t *p, size_t n)
{
    uint8_t ia, ga;
    size_t i = 0;
    lgrid_alphas(g, &ia, &ga);
    while (i < n) {
        uint32_t cp;
        int cells;
        i += utf8_decode(p + i, n - i, &cp);
        if (cp < 0x20 || (cp >= 0x7F && cp < 0xA0))
            continue;
        if (g->pen.faint && cp >= BRAILLE && cp < BRAILLE + 0x100)
            cp |= FAINT;
        if (g->y < 0 || g->y >= g->rows || g->x < 0 || g->x >= g->cols) {
            if (g->x > g->cols)
                g->x = g->cols;
            continue;
        }
        cells = mask_of(s, cp)->cells;
        if (cells == 2 && g->x + 1 >= g->cols)
            cells = 1;
        lgrid_put(g, g->x, g->y, cp, ia, ga);
        if (cells == 2)
            lgrid_put(g, g->x + 1, g->y, cp | TAIL, ia, ga);
        g->x += cells;
    }
    g->stale = 1;
}

/* One control sequence on a cells layer: CUP, SGR and a clear, as on the
 * base, the bare reset ending a frame, which then goes up. */
static void lgrid_csi(Session *sess, LGrid *g, const uint8_t *p, size_t n, uint8_t fin)
{
    int v[MAX_PARAMS], nv = 0, cur = 0, private = 0;
    size_t i;
    for (i = 0; i < n; i++) {
        uint8_t c = p[i];
        if (c >= '0' && c <= '9') {
            if (cur < 1000000)
                cur = cur * 10 + (c - '0');
        } else if (c == ';') {
            if (nv < MAX_PARAMS)
                v[nv++] = cur;
            cur = 0;
        } else {
            private = 1;
        }
    }
    if (n > 0 && nv < MAX_PARAMS)
        v[nv++] = cur;
    switch (fin) {
    case 'm':
        if (nv == 0 || (nv == 1 && v[0] == 0)) {
            pen_reset(&g->pen);
            g->ink_a = 255;
            g->ground_a = 0;
            sess->n_frames++;
            redraw(sess->w->s);
        } else if (!private) {
            lpen_take(g, v, nv);
        }
        break;
    case 'H':
    case 'f':
        g->y = (nv >= 1 && v[0] > 0 ? v[0] : 1) - 1;
        g->x = (nv >= 2 && v[1] > 0 ? v[1] : 1) - 1;
        break;
    case 'J':
        lgrid_wipe(g);
        break;
    default:
        break;
    }
}

/* A cells layer's bytes: a sender's stream, as the base's is, onto the
 * layer's own grid. Commands are this session's, as a video layer's are;
 * kitty graphics are the base's alone and are let by. */
static void cells_feed(Session *s)
{
    LGrid *g = s->w->s->cells[s->z];
    uint8_t *b = s->vbuf;
    size_t p = 0, len = s->vlen;
    if (!g) {
        s->vlen = 0;
        return;
    }
    while (p < len && s->state == S_OPEN) {
        uint8_t c;
        if (g->skip) {
            size_t q = p;
            while (q + 1 < len && !(b[q] == 0x1b && b[q + 1] == '\\'))
                q++;
            if (q + 1 < len) {
                g->skip = 0;
                p = q + 2;
                continue;
            }
            p = b[len - 1] == 0x1b ? len - 1 : len;
            break;
        }
        if (b[p] != 0x1b) {
            const uint8_t *esc = memchr(b + p, 0x1b, len - p);
            size_t end = esc ? (size_t)(esc - b) : len - utf8_incomplete(b + p, len - p);
            if (end > p) {
                if (g->face)
                    face_swap(s->w->s, g->face);
                lgrid_text(s->w->s, g, b + p, end - p);
                if (g->face)
                    face_swap(s->w->s, g->face);
            }
            p = end;
            if (!esc)
                break;
            continue;
        }
        if (p + 1 >= len)
            break;
        c = b[p + 1];
        if (c == '[') {
            size_t q = p + 2;
            while (q < len && b[q] >= 0x20 && b[q] <= 0x3f)
                q++;
            if (q >= len)
                break;
            lgrid_csi(s, g, b + p + 2, q - (p + 2), b[q]);
            p = q + 1;
            continue;
        }
        if (c == '_') {
            size_t j;
            int found = 0;
            for (j = p + 2; j + 1 < len; j++)
                if (b[j] == 0x1b && b[j + 1] == '\\') {
                    found = 1;
                    break;
                }
            if (!found) {
                if (len - p > MAX_APC + 2) {
                    g->skip = 1;
                    p += 2;
                } else {
                    break;
                }
                continue;
            }
            if (j > p + 2 && b[p + 2] != 'G')
                controller_apc(s, b + p + 2, j - (p + 2));
            p = j + 2;
            continue;
        }
        p += 2;
    }
    if (p < len)
        memmove(b, b + p, len - p);
    s->vlen = len - p;
}

/* Take what a video layer's reader has, a few megabytes at most a turn so
 * the other sessions are served, and put the wall up again once if a frame
 * came. The end of its `in` ends the session. */
static void video_read(Session *s, double now)
{
    Screen *sc = s->w->s;
    long before = sc->pic_frames;
    size_t took = 0;
    while (s->state == S_OPEN && took < (8u << 20)) {
        ssize_t got;
        /* A layer whose frames are with the pool is read no further than
         * its next frame, and its end waits for what it sent to be up. */
        if (s->vin.held)
            break;
        if (s->vcap - s->vlen < READ_SIZE) {
            size_t cap = s->vcap ? s->vcap * 2 : 2 * READ_SIZE;
            uint8_t *nb;
            while (cap - s->vlen < READ_SIZE)
                cap *= 2;
            nb = realloc(s->vbuf, cap);
            if (!nb)
                break;
            s->vbuf = nb;
            s->vcap = cap;
        }
        got = inbox_take(&s->vin, s->vbuf + s->vlen, READ_SIZE, 0);
        if (got < 0 && s->jhead) {
            s->vin.held = 1;
            break;
        }
        if (got < 0) {
            session_end(s, "eof");
            break;
        }
        if (got == 0)
            break;
        if (!s->connected) {
            s->connected = 1;
            wsay(s, "repaint");
        }
        s->last_in = now;
        s->n_in += (double)got;
        sc->bytes_in += (double)got;
        s->vlen += (size_t)got;
        took += (size_t)got;
        if (s->is_cells)
            cells_feed(s);
        else
            video_feed(s);
    }
    if (sc->pic_frames != before)
        redraw(sc);
}

/* A video base has put a frame up: the wall has a sender, and the glass
 * under its frames is black, as a sender's first frame wakes a dozing
 * wall. */
static void video_base_frame(Wall *w)
{
    Screen *s = w->s;
    w->last_data = now_ms();
    if (w->dozing) {
        w->dozing = 0;
        if (!dark_asked(w))
            blank(s, 0);
    }
    if (!w->connected) {
        w->connected = 1;
        w->showing_status = 0;
        if (!s->blanked) {
            pen_reset(&s->pen);
            clear(s);
            present(s);
        }
        remember(w);
    }
}

/* Play a video layer's stream on: its bytes counted, frames that are late
 * let go, the one that is due put up, and a stream that has ended taken
 * down. With `fps` frame i is due at t0 + i/fps, t0 being `start` for the
 * first stream and otherwise the moment its first frame was ready; a
 * frame is late once the next one is due. Without it each frame goes up
 * as it is ready, one a pass. */
static void data_play(Session *s)
{
    Wall *w = s->w;
    Screen *sc = w->s;
    DataStream *d = s->ds;
    DFrame *f, *show = NULL;
    int ended = 0;
    unsigned long late = 0;
    double bytes, now = unix_ms();
    SDL_LockMutex(bell.mu);
    bytes = d->bytes;
    while ((f = d->head)) {
        if (f->end) {
            if (!show) {
                ended = f->end;
                dframe_keep(d, dframe_pop(d));
            }
            break;
        }
        if (d->fps > 0) {
            double per = 1e3 / d->fps, due;
            if (!d->t0)
                d->t0 = !d->streams && d->start > 0 ? d->start : now - f->index * per;
            due = d->t0 + f->index * per;
            if (now >= due + per) {
                dframe_keep(d, dframe_pop(d));
                late++;
                continue;
            }
            if (now < due)
                break;
        }
        show = dframe_pop(d);
        break;
    }
    SDL_UnlockMutex(bell.mu);
    if (bytes > d->taken) {
        s->n_in += bytes - d->taken;
        sc->bytes_in += bytes - d->taken;
        d->taken = bytes;
        s->connected = 1;
        s->last_in = now_ms();
    }
    s->n_dropped += late;
    if (show) {
        double t0 = now_ms();
        if (pic_frame(sc, s->z, show->px, show->w, show->h, show->yuv, s->data_align,
                      s->data_box) < 0) {
            wsay(s, "error data %s: too big for this glass", CODEC_NAME[d->codec]);
        } else {
            s->n_frames++;
            s->n_decode += now_ms() - t0 + show->decode;
            s->n_upload += now_ms() - t0;
            free(s->vlast);         /* a picture of the layer is this frame now */
            s->vlast = NULL;
            s->vlastlen = 0;
            redraw(sc);
            if (s->is_base)
                video_base_frame(w);
            f = s->shown;
            s->shown = show;
            show = f;
        }
        if (show) {
            SDL_LockMutex(bell.mu);
            dframe_keep(d, show);
            SDL_UnlockMutex(bell.mu);
        }
    }
    if (ended) {
        if (ended == 2)
            wsay(s, "error data %s: does not decode", CODEC_NAME[d->codec]);
        if (w->above[s->z] == s)
            video_clear(w, s->z);
        d->streams++;
        d->t0 = 0;
    }
}

/* How long until a video layer's stream has something for the glass, in
 * ms: 0 when a frame or an end is ready now. */
static int data_wait(DataStream *d, int wait)
{
    DFrame *f;
    SDL_LockMutex(bell.mu);
    f = d->head;
    if (f && (f->end || d->fps <= 0 || !d->t0))
        wait = 0;
    else if (f) {
        double left = d->t0 + f->index * 1e3 / d->fps - unix_ms();
        if (left < wait)
            wait = left <= 0 ? 0 : (int)left + 1;
    }
    SDL_UnlockMutex(bell.mu);
    return wait;
}

/* -- the ping clock ------------------------------------------------------ */

static void ping_clock(Session *s, double now, int watching)
{
    double quiet = QUIET * CLK * 1e3, ans = ANSWER * CLK * 1e3, un = DARK_ASK * CLK * 1e3;
    if (s->awaiting) {
        if (now - s->pinged_at >= ans) {
            if (s->answered || s->role == ROLE_CONTROLLER)
                session_end(s, "timeout");
            else
                s->awaiting = 0;    /* never answered: the question is forgotten */
        }
        return;
    }
    {
        double base = s->pinged_at > s->last_in ? s->pinged_at : s->last_in;
        double since = now - base;
        int due;
        if (watching)
            due = since >= quiet;
        else if (s->answered)
            due = since >= un;
        else
            due = s->pings == 0 && since >= quiet;
        if (due) {
            wsay(s, "ping");
            s->awaiting = 1;
            s->pinged_at = now;
            s->pings++;
        }
    }
}

/* -- the life of a session ----------------------------------------------- */

/* Remove a session that has ended and been read: its fifos closed, its
 * directory gone, its slot freed. */
static void session_remove(Wall *w, Session *s)
{
    Session **pp = &w->sessions;
    int i;
    while (*pp && *pp != s)
        pp = &(*pp)->next;
    if (*pp)
        *pp = s->next;
    free_queue(s);
    inbox_stop(&s->vin);
    stream_stop(s->ds);
    s->ds = NULL;
    dframe_free(s->shown);
    s->shown = NULL;
    free(s->vbuf);
    free(s->vlast);
    if (s->out_fd >= 0)
        ipc_close(s->out_fd);
    if (s->in_fd >= 0)
        ipc_close(s->in_fd);
    for (i = 0; i < MIRRORS; i++)
        mirror_stop(&s->mir[i]);
    outputs_stop(w->s, s, NULL);
    ipc_remove(s->dir);
    free(s);
}

static void session_end(Session *s, const char *reason)
{
    Wall *w = s->w;
    double now = now_ms();
    char sv[33];
    int i;
    if (s->state != S_OPEN)
        return;
    s->state = S_CLOSED;
    s->reason = reason;
    s->ended_at = now;
    snprintf(sv, sizeof sv, "%s", w->id);
    w->id[0] = 0;
    wsay(s, "closed %s %s", s->name, reason);
    snprintf(w->id, sizeof w->id, "%s", sv);
    for (i = 0; i < MIRRORS; i++)
        mirror_stop(&s->mir[i]);
    outputs_stop(w->s, s, NULL);
    s->measures = 0;
    s->measure_due = 0;
    if (s->is_video) {
        inbox_stop(&s->vin);
        stream_stop(s->ds);
        s->ds = NULL;
        if (w->above[s->z] == s) {
            if (!(s->z == 0 && w->handover))
                video_clear(w, s->z);
            if (s->is_cells && w->s->cells[s->z]) {
                lgrid_free(w->s, w->s->cells[s->z]);
                w->s->cells[s->z] = NULL;
                redraw(w->s);
            }
            w->above[s->z] = NULL;
            /* No gap is left: the rest of its stack closes up behind it. */
            if (s->z > 0) {
                int k;
                if (s->from_top)
                    for (k = s->z - 1; k >= 1 && above_held(w, k) && w->above[k]->from_top; k--)
                        layer_move(w, k, k + 1);
                else
                    for (k = s->z + 1; k < LAYERS && above_held(w, k)
                         && !w->above[k]->from_top; k++)
                        layer_move(w, k, k - 1);
            }
        }
    }
    if (s->is_base) {
        inbox_stop(&w->in);     /* closes and clears the base's `in` fd */
        w->base = NULL;
        sender_gone(w, now);
    } else if (!s->is_video && s->in_fd >= 0) {
        ipc_close(s->in_fd);
        s->in_fd = -1;
    }
    {
        char line[96];
        snprintf(line, sizeof line, "session closed %s %s", s->name, reason);
        tell_controllers(w, line, s, NULL, "");
    }
}

/* Sessions that have ended and been read, or waited their ten seconds, gone. */
static void session_reap(Wall *w, double now)
{
    Session *s = w->sessions, *next;
    while (s) {
        next = s->next;
        if (s->state == S_REFUSED || s->state == S_CLOSED) {
            int held = ipc_held(s->out_fd);
            if ((!s->qhead && !held) || now - s->ended_at >= KEEP_S * CLK * 1e3)
                session_remove(w, s);
        }
        s = next;
    }
}

/* -- claiming a wall, and its door --------------------------------------- */

static void remember(Wall *w);
static void recall(Wall *w);

static int wall_claim(Wall *w, Screen *s, const char *run, const char *name)
{
    const char *env;
    w->s = s;
    s->wall = w;
    snprintf(w->run, sizeof w->run, "%s", run);
    snprintf(w->name, sizeof w->name, "%s", name);
    snprintf(w->dir, sizeof w->dir, "%s/bitwall/%s", run, name);
    /* The last-seen note is the painter's own, so it lives outside
     * bitwall/, where a controller listing walls never meets it. */
    snprintf(w->last_file, sizeof w->last_file, "%s/bitwall-%s.last", ipc_notes(run), name);
    w->door_fd = -1;
    w->sessions = NULL;
    w->base = NULL;
    w->to = NULL;
    w->pong_ms = -1;
    w->last_data = now_ms();
    w->disconnected_since = now_ms();
    w->display_said = NULL;
    s->st.at = POINT_NE;
    s->st.size = 14;
    s->st.show = MEASURE_FPS;
    env = getenv("BITWALL_STATUS_FONT");
    w->status_font = env ? atoi(env) : 28;
    env = getenv("BITWALL_RATE");
    w->rate = env ? atoi(env) : 30;
    env = getenv("BITWALL_COLOUR");
    w->colour = env ? env : "true";
    wall_glyphs(w);
    recall(w);

    /* The door made and the wall claimed, as the machine's pipes do it
     * (ipc-posix.c, ipc-win.c). */
    if ((w->door_fd = ipc_claim(run, name, w->dir)) < 0)
        return -1;
    return 0;
}

/* One `open` request at the door: a session made, or the request dropped
 * with nothing said. */
static void wall_request(Wall *w, char *line, double now)
{
    char *words[MAX_PARAMS], *save = NULL, *tok;
    int nw = 0, i;
    const char *name, *role, *reason;
    char path[640], fifo[sizeof path + 8];
    Session *s;

    for (tok = strtok_r(line, " \t\r", &save); tok && nw < MAX_PARAMS;
         tok = strtok_r(NULL, " \t\r", &save))
        words[nw++] = tok;
    if (nw < 2 || strcmp(words[0], "open") != 0 || !id_ok(words[1])
        || !strcmp(words[1], "glass"))
        return;                 /* bad-name: dropped; `glass` names the glass */
    name = words[1];
    role = nw > 2 ? words[2] : NULL;
    for (i = 3; i < nw; i++) {
        char *eq = strchr(words[i], '=');
        if (!eq || eq == words[i] || !eq[1])
            return;             /* not key=value: dropped */
    }
    snprintf(path, sizeof path, "%s/s/%s", w->dir, name);
    if (ipc_session_make(path) < 0)
        return;                 /* name in use: dropped */

    s = calloc(1, sizeof *s);
    if (!s)
        return;
    s->w = w;
    snprintf(s->name, sizeof s->name, "%s", name);
    snprintf(s->dir, sizeof s->dir, "%s", path);
    s->in_fd = s->out_fd = -1;
    s->z = -1;
    s->state = S_OPENING;
    s->opened_at = s->last_in = now;
    s->pong_ms = -1;
    s->r_at = now;
    s->r_in = s->r_out = s->r_fps = s->r_decode = s->r_upload = -1;
    /* Appended, so a listing is in the order sessions were opened. */
    if (!w->sessions)
        w->sessions = s;
    else {
        Session *t = w->sessions;
        while (t->next)
            t = t->next;
        t->next = s;
    }
    snprintf(fifo, sizeof fifo, "%s/in", s->dir);
    ipc_in_make(fifo);
    snprintf(fifo, sizeof fifo, "%s/out", s->dir);
    /* made last, so a whole `out` means a whole session */
    s->out_fd = ipc_out_make(fifo);
    emit_keep(s, "hello v1 line");
    reason = admit(s, role, words + 3, nw - 3);
    if (reason) {
        s->state = S_REFUSED;
        s->reason = reason;
        s->ended_at = now;
        wsay(s, "error open %s %s", name, reason);
        return;
    }
    s->state = S_OPEN;
    if (s->role == ROLE_LAYER) {
        int fd;
        snprintf(fifo, sizeof fifo, "%s/in", s->dir);
        fd = ipc_in_open(fifo, 0);
        if (s->is_video) {
            /* A reader of its own, so its backpressure is its own. */
            s->vin.max = VIDEO_INBOX;
            inbox_start(&s->vin, fd);
            w->above[s->z] = s;
            if (s->is_cells) {
                lgrid_free(w->s, w->s->cells[s->z]);
                w->s->cells[s->z] = lgrid_new(w->s);
            }
            if (s->z == 0) {
                w->base = s;
                s->is_base = 1;
                w->connected = 0;
                w->showing_status = 0;
            }
            if (s->data_codec)
                s->ds = stream_start(s->dir, s->data_codec, s->data_fps, s->data_start);
        } else {
            inbox_start(&w->in, fd);
            w->base = s;
            s->is_base = 1;
            w->connected = 0;
            w->showing_status = 0;
        }
    } else {
        snprintf(fifo, sizeof fifo, "%s/in", s->dir);
        s->in_fd = ipc_in_open(fifo, 1);
    }
    if (s->role == ROLE_LAYER)
        wsay(s, "opened %s z=%d%s", name, s->z, s->ds ? " data" : "");
    else
        wsay(s, "opened %s", name);
    {
        char line2[192], place[12];
        snprintf(place, sizeof place, "%d", s->z);
        /* As `sessions` lists it, the instance from its open included. */
        snprintf(line2, sizeof line2, "session opened %s %s %s %s%s%s", name, role_word(s),
                 s->role == ROLE_LAYER ? place : "-", s->type[0] ? s->type : "-",
                 s->instance[0] ? " instance=" : "", s->instance);
        tell_controllers(w, line2, s, NULL, "");
    }
}

/* Whether layer `z` above the base is held by an open session. */
static int above_held(Wall *w, int z)
{
    return w->above[z] && w->above[z]->state == S_OPEN;
}

/* A layer moved from slot `from` to the free slot `to`, with what it
 * shows -- its picture, or its grid -- and told so, as every controller
 * is. Its name, session and pipes are as they were; only its place in the
 * stack, and so the order it is drawn in, has changed. */
static void layer_move(Wall *w, int from, int to)
{
    Screen *sc = w->s;
    Session *x = w->above[from];
    char line[96];
    if (!x || from == to || from < 1 || to < 1 || from >= LAYERS || to >= LAYERS)
        return;
    w->above[to] = x;
    w->above[from] = NULL;
    sc->pics[to] = sc->pics[from];
    memset(&sc->pics[from], 0, sizeof sc->pics[from]);
    sc->cells[to] = sc->cells[from];
    sc->cells[from] = NULL;
    x->z = to;
    snprintf(line, sizeof line, "moved %s z=%d", x->name, to);
    emit(x, line);
    snprintf(line, sizeof line, "session moved %s %d", x->name, to);
    tell_controllers(w, line, NULL, NULL, "");
    redraw(sc);
}

/* Take the request or the word that refuses it. The base takes cells or
 * video and is displaced by take=1; each layer above it takes video, and
 * is given by place: `above` the lowest free, `top` the highest free, a
 * number exactly that one. A layer above the base is never taken from its
 * holder. A video layer may have a stream on `data`, in a codec this wall
 * takes, played out at `fps` from `start`. */
static const char *admit(Session *s, const char *role, char **opts, int nopts)
{
    Wall *w = s->w;
    int z = 0, place = 0, take = 0, video = 0, i, held;   /* place: 1 above, 2 top */
    const char *data = NULL, *codec = NULL, *fps = NULL, *start = NULL;
    Session *x;
    if (!role || (strcmp(role, "layer") && strcmp(role, "controller")))
        return "no-such-role";
    if (!strcmp(role, "controller")) {
        /* A controller's one option is the instance a controller started
         * it under, as a layer's is, so `sessions` finds a sender that
         * only talks to the wall. */
        for (i = 0; i < nopts; i++) {
            if (strncmp(opts[i], "instance=", 9) || !instance_ok(opts[i] + 9))
                return "no-such-key";
            snprintf(s->instance, sizeof s->instance, "%s", opts[i] + 9);
        }
        held = 0;
        for (x = w->sessions; x; x = x->next)
            if (x != s && x->state == S_OPEN && x->role == ROLE_CONTROLLER)
                held++;
        if (held >= CONTROLLERS)
            return "full";
        s->role = ROLE_CONTROLLER;
        s->z = -1;
        return NULL;
    }
    s->role = ROLE_LAYER;
    for (i = 0; i < nopts; i++) {
        char *eq = strchr(opts[i], '=');
        char key[16];
        const char *v;
        size_t kl;
        if (!eq)
            return "no-such-key";
        kl = (size_t)(eq - opts[i]);
        if (kl >= sizeof key)
            return "no-such-key";
        memcpy(key, opts[i], kl);
        key[kl] = 0;
        v = eq + 1;
        if (!strcmp(key, "z")) {
            const char *p = v;
            if (!strcmp(v, "above") || !strcmp(v, "top")) {
                place = v[0] == 'a' ? 1 : 2;
                continue;
            }
            if (!*p)
                return "no-such-key";
            for (; *p; p++)
                if (*p < '0' || *p > '9')
                    return "no-such-key";
            z = strlen(v) > 3 ? 1000 : atoi(v);
            place = 0;
        } else if (!strcmp(key, "type")) {
            if (!strcmp(v, "video"))
                video = 1;
            else if (strcmp(v, "cells"))
                return "no-such-type";
        } else if (!strcmp(key, "take")) {
            if (strcmp(v, "1"))
                return "no-such-key";
            take = 1;
        } else if (!strcmp(key, "data")) {
            data = v;
        } else if (!strcmp(key, "codec")) {
            codec = v;
        } else if (!strcmp(key, "fps")) {
            fps = v;
        } else if (!strcmp(key, "start")) {
            start = v;
        } else if (!strcmp(key, "instance")) {
            if (!instance_ok(v))
                return "no-such-key";
            snprintf(s->instance, sizeof s->instance, "%s", v);
        } else {
            return "no-such-key";
        }
    }
    if (data || codec || fps || start) {
        char *end = NULL;
        double f = fps ? strtod(fps, &end) : 0;
        if (!data || strcmp(data, "1") || !video || (start && !fps))
            return "no-such-key";
        if (fps && (!*fps || *end || !(f > 0 && f <= 240) || !strchr("0123456789", fps[0])))
            return "no-such-key";
        if (start && !digits(start, 16))
            return "no-such-key";
        if (!codec || !codec_of(codec) || !codecs.ok[codec_of(codec)])
            return "no-such-codec";
        s->data_codec = codec_of(codec);
        s->data_fps = f;
        s->data_start = start ? strtod(start, NULL) : 0;
    }
    if (place && !video && !w->s->gpu)
        return "no-such-type";
    if (z >= LAYERS)
        return "full";
    if (!video && (place || z > 0) && !w->s->gpu)
        return "no-such-type";      /* a cells layer is drawn on the GPU */
    if (place || z > 0) {
        /* Two stacks with no gaps: one up from the base, one down from the
         * highest slot. `above` goes on top of the first, `top` under the
         * second, and a number in at that slot of the first, or on top of
         * it where it is not that high yet, the layers from there up
         * moving one to make room. */
        int lo = 0, hi = 0, k;
        if (take)
            return "no-such-key";
        for (k = 1; k < LAYERS; k++)
            if (above_held(w, k)) {
                if (w->above[k]->from_top)
                    hi++;
                else
                    lo++;
            }
        if (lo + hi >= LAYERS - 1)
            return "full";
        if (place == 2) {
            z = LAYERS - 1 - hi;
            s->from_top = 1;
        } else {
            if (place == 1 || z > lo + 1)
                z = lo + 1;
            for (k = lo; k >= z; k--)
                layer_move(w, k, k + 1);
        }
        s->z = z;
        snprintf(s->type, sizeof s->type, "%s", video ? "video" : "cells");
        /* Read as a video layer is, by a reader of its own; a cells layer
         * is drawn from a grid of its own rather than a picture. */
        s->is_video = 1;
        s->is_cells = !video;
        return NULL;
    }
    s->z = z;
    snprintf(s->type, sizeof s->type, "%s", video ? "video" : "cells");
    x = (w->base && w->base->state == S_OPEN) ? w->base : NULL;
    if (x) {
        if (!take)
            return "busy";
        /* A film or a mirror timed afresh takes the base from the one
         * before it: what that one showed stays up until the new one has
         * a frame, rather than a black gap seconds long. */
        w->handover = video && x->is_video;
        session_end(x, "displaced");
        w->handover = 0;
    }
    s->is_video = video;
    return NULL;
}

/* Read the door and take every whole request. Held O_RDWR, so it never
 * ends and requests from different callers never mix. */
static void wall_door(Wall *w, double now)
{
    char buf[65536 + 4096];
    ssize_t n;
    size_t at = 0, st;
    if (w->door_fd < 0)
        return;
    memcpy(buf, w->door_rest, w->door_nrest);
    at = w->door_nrest;
    n = ipc_read(w->door_fd, buf + at, sizeof buf - at - 1);
    if (n > 0)
        at += (size_t)n;
    st = 0;
    {
        size_t i;
        for (i = 0; i < at; i++)
            if (buf[i] == '\n') {
                buf[i] = 0;
                wall_request(w, buf + st, now);
                st = i + 1;
            }
    }
    w->door_nrest = at - st;
    if (w->door_nrest > sizeof w->door_rest)
        w->door_nrest = 0;      /* a line too long to be a request; dropped */
    else
        memcpy(w->door_rest, buf + st, w->door_nrest);
}

/* One controller's `in`, read without blocking: its commands acted on, its
 * frame bytes let go, and its writer leaving noticed. */
static void controller_read(Session *s, double now)
{
    uint8_t buf[65536];
    for (;;) {
        ssize_t n = ipc_read(s->in_fd, buf, sizeof buf);
        if (n > 0) {
            if (!s->connected) {
                s->connected = 1;
                s->last_in = now;
                wsay(s, "repaint");
            }
            s->last_in = now;
            s->n_in += (double)n;
            s->w->s->bytes_in += (double)n;
            controller_feed(s, buf, (size_t)n);
        } else if (n == 0) {
            if (s->connected)
                session_end(s, "eof");
            break;
        } else {
            break;              /* EAGAIN: nothing waiting */
        }
    }
}

/* -- the base, and the wall's clocks ------------------------------------- */

static void sender_gone(Wall *w, double now)
{
    Screen *s = w->s;
    w->connected = 0;
    w->asked_font = 0;
    w->disconnected_since = now;
    remember(w);
    recall(w);
    s->len = 0;
    s->cut = 1;
    s->x = s->y = 0;
    pen_reset(&s->pen);
    kitty_clear(s);
    if (!s->blanked) {
        clear(s);
        present(s);
    }
}

static void wall_data(Wall *w)
{
    Screen *s = w->s;
    Session *b = w->base;
    if (!b)
        return;
    if (w->dozing) {
        w->dozing = 0;
        if (!dark_asked(w))
            blank(s, 0);
    }
    if (!b->connected) {
        b->connected = 1;
        w->connected = 1;
        w->showing_status = 0;
        b->last_in = now_ms();
        emit(b, "repaint");
        if (!s->blanked) {
            pen_reset(&s->pen);
            clear(s);
        }
        {
            int want = w->asked_font ? w->asked_font : w->font;
            if (want && s->size != want)
                set_font(s, want);
        }
        remember(w);
    }
    {
        long before = s->painted;
        feed(s);
        b->n_frames += s->painted - before;
    }
    b->last_in = now_ms();
    w->last_data = b->last_in;
}

/* The controller that asked `glass`, while its session is open. */
static Session *glass_asker(Wall *w)
{
    Session *x;
    for (x = w->sessions; w->glass_to[0] && x; x = x->next)
        if (x->state == S_OPEN && !strcmp(x->name, w->glass_to))
            return x;
    return NULL;
}

/* `glass` is answered by the area, the asker's id on it: the area every
 * session is told once the glass has taken a size (wall_resized), or,
 * where the size asked has had time to come and the glass has stayed as
 * it was -- a window held to the screen, or the size it already was --
 * the area as it stands, said to the asker alone. */
static void glass_answer(Wall *w, double now)
{
    Session *x;
    char line[96];
    if (!w->glass_to[0] || w->s->settle_at || now - w->glass_at < SETTLE)
        return;
    if ((x = glass_asker(w)) != NULL) {
        snprintf(line, sizeof line, "area %dx%d %d%s%s", w->s->cols, w->s->rows,
                 w->s->size, w->glass_id[0] ? " id=" : "", w->glass_id);
        emit(x, line);
    }
    w->glass_to[0] = w->glass_id[0] = 0;
}

/* The glass changing size under the wall: a window dragged, a glass with
 * no screen asked for another size, a monitor's mode changed. A size the
 * display offers is taken once it has held for SETTLE, so a drag is drawn
 * at the size it stops at and not at every size on the way. Then what is
 * the glass's size is made again -- the GPU's own glass where it keeps
 * one, the software path's surfaces -- and set_font measures the grid
 * from the font, takes the layers above down and tells every session
 * `area`, `screen` and `repaint` as a new glass is told; a stream keeps
 * its own size and fits the new glass into it frame by frame. */
static void wall_resized(Wall *w)
{
    Screen *s = w->s;
    int pw, ph;
    double now = now_ms();
    display_size(s, &pw, &ph);
    if (pw == s->phys_w && ph == s->phys_h) {
        s->settle_at = 0;
        glass_answer(w, now);
        return;
    }
    if (!s->settle_at || pw != s->settle_w || ph != s->settle_h) {
        s->settle_w = pw;
        s->settle_h = ph;
        s->settle_at = now;
        return;
    }
    if (now - s->settle_at < SETTLE)
        return;
    s->settle_at = 0;
    take_size(s);
    fprintf(stderr, "bitwall-paint: wall %d: the glass is %dx%d\n", s->index, s->width, s->height);
    if (s->gpu) {
        if (gpu_resized(s) < 0)
            fprintf(stderr, "bitwall-paint: wall %d: the glass did not take %dx%d\n",
                    s->index, s->phys_w, s->phys_h);
    } else {
        s->glass = display_surface(s);
        SDL_FreeSurface(s->paper);
        s->paper = SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height,
                                                  s->glass->format->BitsPerPixel,
                                                  s->glass->format->format);
        SDL_SetSurfaceBlendMode(s->glass, SDL_BLENDMODE_NONE);
        if (s->paper)
            SDL_SetSurfaceBlendMode(s->paper, SDL_BLENDMODE_NONE);
        s->target = s->blanked ? s->paper : s->glass;
    }
    /* The area said to every session is the answer to whoever asked for
     * the glass, and carries its id there. */
    if ((w->to = glass_asker(w)) != NULL)
        snprintf(w->id, sizeof w->id, "%s", w->glass_id);
    w->glass_to[0] = w->glass_id[0] = 0;
    set_font(s, s->size);       /* re-measures the grid and announces the area */
    w->to = NULL;
    w->id[0] = 0;
}

/* `glass <w>x<h>`: the glass asked to be that many pixels, by a
 * controller. With no screen it becomes that size; in a window the window
 * is made it; a monitor's glass is its mode, not the painter's to change,
 * and `caps` says `glass` only where it is offered. Answered by the area
 * once the glass has settled, with the asker's id. */
static void cmd_glass(Wall *w, Session *from, char **words, int n)
{
    Screen *s = w->s;
    char said[48];
    int gw = 0, gh = 0, most = s->gpu ? s->gl.max_texture : 16384;
    char end = 0;
    if (!glass_offered(s)) {
        wsay(from, "error glass: not offered");
        return;
    }
    if (n < 2 || sscanf(words[1], "%dx%d%c", &gw, &gh, &end) != 2 || gw < 1 || gh < 1) {
        wsay(from, "error glass %s: not <w>x<h>",
             n >= 2 ? plain(words[1], said, sizeof said) : "");
        return;
    }
    if (gw < GLASS_MIN || gh < GLASS_MIN || gw > most || gh > most) {
        wsay(from, "error glass %dx%d: not between %dx%d and %dx%d", gw, gh,
             GLASS_MIN, GLASS_MIN, most, most);
        return;
    }
    display_glass(s, gw, gh);
    snprintf(w->glass_to, sizeof w->glass_to, "%s", from->name);
    snprintf(w->glass_id, sizeof w->glass_id, "%s", w->id);
    w->glass_at = now_ms();
}

static void wall_tick(Wall *w, double now)
{
    Screen *s = w->s;
    Session *sess, *next;

    pool_deliver();
    wall_resized(w);
    for (sess = w->sessions; sess; sess = sess->next)
        flush_out(sess);
    if (!w->display_said) {
        w->display_said = display_word(w);
        w->power_said = power_word(w);
        w->watched_said = watched(w);
    } else {
        wall_power(w);
    }

    for (sess = w->sessions; sess; sess = next) {
        next = sess->next;
        if (sess->state != S_OPEN)
            continue;
        if (!sess->connected && now - sess->opened_at >= UNWRITTEN * CLK * 1e3) {
            session_end(sess, "timeout");
            continue;
        }
        session_rates(sess, now);
        if (sess->connected)
            ping_clock(sess, now, watched(w));
        if (sess->state == S_OPEN)
            mirror_tick(w, sess, now);
        if (sess->state != S_OPEN || sess->role != ROLE_CONTROLLER)
            continue;
        if (sess->measures && now >= sess->measure_due) {
            Sample look = sample_of(s);
            char line[MAX_APC], full[MAX_APC + 16];
            Session *x;
            measure_line(w, &look, &sess->seen, line, sizeof line);
            sess->seen = look;
            sess->measure_due = now + sess->measures * 1e3;
            snprintf(full, sizeof full, "measure %s", line);
            wsay_line(sess, full);
            for (x = w->sessions; x; x = x->next)
                if (x->state == S_OPEN) {
                    session_measure(x, now, line, sizeof line);
                    snprintf(full, sizeof full, "measure %s", line);
                    wsay_line(sess, full);
                }
        }
    }

    for (sess = w->sessions; sess; sess = sess->next)
        if (sess->state == S_OPEN && sess->ds)
            data_play(sess);
    {
        int z;
        for (z = 0; z < LAYERS; z++)
            if (s->pics[z].on && now >= s->pics[z].until)
                video_clear(w, z);  /* no frame within its hold */
    }
    redraw_owed(s);
    outputs_tick(w->s);
    if (s->blanked && s->lit && s->blanked_at && now - s->blanked_at > STANDBY * 1e3)
        power(s, 0);
    if (!s->blanked && !w->connected && now - w->last_data > DOZE * 1e3) {
        w->dozing = 1;
        w->showing_status = 0;
        blank(s, 1);
    }
    /* The status is the wall's own while nobody holds the base. A base
     * opened and not yet written to is left alone until its first byte,
     * which tells it `repaint` before anything else: putting the status
     * up again under it would tell it the status font's area first. */
    if (!s->blanked && !w->connected && !w->base
        && now - w->last_status >= STATUS_PERIOD * 1e3) {
        if (!w->showing_status) {
            w->showing_status = 1;
            set_font(s, w->status_font);
        }
        wall_status(w);
        w->last_status = now;
    }
    if (s->st.on && now - s->st.last >= STATS_PERIOD) {
        double due = s->st.last + STATS_PERIOD;
        wall_stats(w);
        if (now - due < STATS_PERIOD)
            s->st.last = due;
    }
    session_reap(w, now);
}

/* How long the loop may wait before a wall needs it: soon while a session
 * has bytes waiting or a mirror or a measure is due. */
static int wall_wait(Wall *w, int wait)
{
    double now = now_ms();
    Session *s;
    int k;
    for (k = 0; k < LAYERS; k++)
        if (w->s->pics[k].on) {
            double left = w->s->pics[k].until - now;
            if (left < wait)
                wait = left < 1 ? 1 : (int)left + 1;
        }
    wait = outputs_wait(w->s, wait);
    for (s = w->sessions; s; s = s->next) {
        if (s->state != S_OPEN)
            continue;
        if (s->ds)
            wait = data_wait(s->ds, wait);
        /* Lines waiting go out soon while the reader keeps taking them;
         * one that has stopped is not asked again sooner than the loop
         * comes round anyway, since only its reading makes room. */
        if (s->qhead && !s->stuck && wait > 5)
            wait = 5;
        for (k = 0; k < MIRRORS; k++)
            if (s->mir[k].target[0] && s->mir[k].every > 0) {
                double left = s->mir[k].due - now;
                int ms = left < 1 ? 1 : (int)left + 1;
                if (ms < wait)
                    wait = ms;
            }
        if (s->measures) {
            double left = s->measure_due - now;
            int ms = left < 1 ? 1 : (int)left + 1;
            if (ms < wait)
                wait = ms;
        }
    }
    return wait;
}

/* ---------------------------------------------------------------- setup */

static void screens_free(Screen **s, int n)
{
    int i;
    for (i = 0; i < n; i++) {
        Screen *sc = s[i];
        if (!sc)
            continue;
        glyphs_clear(sc);
        if (sc->font)
            TTF_CloseFont(sc->font);
        if (sc->st.font)
            TTF_CloseFont(sc->st.font);
        SDL_FreeSurface(sc->st.soft);
        SDL_FreeSurface(sc->st.under);
        if (sc->gpu) {
            free(sc->gl.verts);
            free(sc->gl.mask);
            free(sc->gl.shows);
            gpu_drop(sc);
        }
        if (sc->paper)
            SDL_FreeSurface(sc->paper);
        power(sc, 1);
        if (sc->win)
            display_drop(sc);
        free(sc);
    }
    video_close();
}

/* A wall, and everything under it, taken down in good order: its sessions
 * ended, its inbox stopped, and its directory removed so the next receiver
 * finds it free. */
static void wall_free(Wall *w)
{
    Session *s, *next;
    int i;
    outputs_free(w->s);
    if (w->base)
        inbox_stop(&w->in);
    if (w->in.fd >= 0 && w->base)
        ipc_close(w->in.fd);
    if (w->door_fd >= 0)
        ipc_close(w->door_fd);
    for (s = w->sessions; s; s = next) {
        next = s->next;
        free_queue(s);
        if (s->out_fd >= 0)
            ipc_close(s->out_fd);
        if (s->in_fd >= 0)
            ipc_close(s->in_fd);
        for (i = 0; i < MIRRORS; i++)
            mirror_stop(&s->mir[i]);
        free(s->vbuf);
        free(s->vlast);
        free(s);
    }
    ipc_remove(w->dir);
    free(w);
}

static volatile sig_atomic_t stopping;

static void on_stop(int sig)
{
    (void)sig;
    stopping = 1;
}

/* The font every wall is drawn in, the same file on every machine so
 * cells match from one wall to the next. */
#define FONT_NAME "MesloLGSNerdFontMono-Regular.ttf"

/* The font where `--font-file` does not say: beside the painter, then
 * among the user's fonts as a Mac and Linux keep them. NULL when it is in
 * none of them. */
static const char *font_found(const char *argv0)
{
    static char path[1024];
#ifdef BITWALL_WIN
    /* Beside the program itself, however it was named when started. */
    static char self[1024];
    if (GetModuleFileNameA(NULL, self, sizeof self) > 0)
        argv0 = self;
#endif
    const char *home = getenv("HOME"), *slash = strrchr(argv0, '/');
#ifdef BITWALL_WIN
    if (strrchr(argv0, '\\') > slash)
        slash = strrchr(argv0, '\\');
#endif
    if (slash) {
        snprintf(path, sizeof path, "%.*s/%s", (int)(slash - argv0), argv0, FONT_NAME);
        if (access(path, R_OK) == 0)
            return path;
    }
    if (home) {
        snprintf(path, sizeof path, "%s/Library/Fonts/%s", home, FONT_NAME);
        if (access(path, R_OK) == 0)
            return path;
        snprintf(path, sizeof path, "%s/.local/share/fonts/%s", home, FONT_NAME);
        if (access(path, R_OK) == 0)
            return path;
    }
    return NULL;
}

/* A wall's name as a target writes it after the host (`host:hdmi`): lower
 * case letters, digits, `_` and `-`, starting with a letter or digit, at
 * most 32 (bitwall/session.py WALL_TOKEN). */
static int wall_name_ok(const char *t)
{
    size_t n = strlen(t), k;
    if (n < 1 || n > 32 || !(islower((unsigned char)t[0]) || isdigit((unsigned char)t[0])))
        return 0;
    for (k = 0; k < n; k++)
        if (!(islower((unsigned char)t[k]) || isdigit((unsigned char)t[k])
              || t[k] == '_' || t[k] == '-'))
            return 0;
    return 1;
}

static void usage(void)
{
    fprintf(stderr,
            "usage: bitwall-paint [--font-file PATH] [--font N] [--driver NAME | --glass WxH]\n"
            "                     [--display N | --walls auto|N] [--run DIR]\n"
            "                     [--mode WALL=WxH ...] [--rotate WALL=0|90|180|270 ...]\n"
            "                     [--presence WALL=NAME ...] [--name WALL=NAME ...]\n"
            "                     [--workers N]\n"
            "                     [--decoder-threads N] [--decoder-nice N]\n"
            "  With --run it claims <DIR>/bitwall/<n>/ for each screen and speaks\n"
            "  the protocol; without it, it reads a paintable stream on stdin.\n"
            "  --driver is SDL's video driver, " DISPLAY_DRIVER " unless said; `none`,\n"
            "  where the machine offers it, is no screen, its glass as --mode says.\n"
            "  --glass WxH is no screen with every wall's glass WxH. --name gives\n"
            "  a wall a name in place of its number, so a painter beside another in\n"
            "  the same run directory claims walls of its own: `--glass 1920x1080\n"
            "  --name 0=hdmi` is a wall with no screen that senders reach as\n"
            "  <host>:hdmi. The font is\n"
            "  " FONT_NAME ", beside the painter or among the user's\n"
            "  fonts, unless --font-file says.\n"
            "  --workers is how many threads decode pictures and encode snapshots\n"
            "  (0: the main thread does), --decoder-threads ffmpeg's -threads and\n"
            "  --decoder-nice its nice (0-19); by default half the cores (1-4),\n"
            "  1 where a hardware decoder does the work and as many as the workers\n"
            "  in software (2 at most with 2 GB or less), and 5. `report` says them.\n");
}

/* A knob's value, a whole number from `lo` to `hi`, into `*out`; 0 when
 * it is not one. */
static int knob_arg(const char *v, int lo, int hi, int *out)
{
    char *end;
    long k = strtol(v, &end, 10);
    if (end == v || *end || k < lo || k > hi)
        return 0;
    *out = (int)k;
    return 1;
}

#ifndef BITWALL_TEST
int main(int argc, char **argv)
{
    const char *font_file = NULL, *driver = DISPLAY_DRIVER, *run = NULL;
    int size = 14, display = 0, display_given = 0, walls = 0;
    int i, n;
    Screen *s[MAX_WALLS];
    Wall *w[MAX_WALLS];
    Inbox *in[MAX_WALLS * LAYERS];
    static Inbox stdin_box;
    static char presence_names[MAX_WALLS][33];
    static char wall_names[MAX_WALLS][33];
    static int mode_w[MAX_WALLS], mode_h[MAX_WALLS], turn[MAX_WALLS];
    const char *env;

    painter_started = time(NULL);
    env = getenv("BITWALL_CLOCK_SCALE");
    if (env) {
        double v = atof(env);
        if (v > 0)
            CLK = v;
    }
    srand((unsigned)getpid() ^ (unsigned)time(NULL));

    for (i = 0; i < MAX_WALLS; i++) {
        mode_w[i] = 1920;
        mode_h[i] = 1080;
    }

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : NULL;
        if (strcmp(a, "--font") == 0 && v) {
            size = atoi(v);
            i++;
        } else if (strcmp(a, "--font-file") == 0 && v) {
            font_file = v;
            i++;
        } else if (strcmp(a, "--driver") == 0 && v) {
            driver = v;
            i++;
        } else if (strcmp(a, "--glass") == 0 && v) {
            int k, gw, gh;
            if (sscanf(v, "%dx%d", &gw, &gh) != 2 || gw < 1 || gh < 1) {
                usage();
                return 2;
            }
            for (k = 0; k < MAX_WALLS; k++) {
                mode_w[k] = gw;
                mode_h[k] = gh;
            }
            driver = "none";
            i++;
        } else if (strcmp(a, "--display") == 0 && v) {
            display = atoi(v);
            display_given = 1;
            i++;
        } else if (strcmp(a, "--walls") == 0 && v) {
            walls = strcmp(v, "auto") == 0 ? -1 : atoi(v);
            if (walls == 0) {
                usage();
                return 2;
            }
            i++;
        } else if (strcmp(a, "--run") == 0 && v) {
            run = v;
            i++;
        } else if (strcmp(a, "--mode") == 0 && v) {
            int k, mw, mh;
            const char *eq = strchr(v, '=');
            if (!eq || sscanf(eq + 1, "%dx%d", &mw, &mh) != 2
                || (k = atoi(v)) < 0 || k >= MAX_WALLS || mw < 1 || mh < 1) {
                usage();
                return 2;
            }
            mode_w[k] = mw;
            mode_h[k] = mh;
            i++;
        } else if (strcmp(a, "--rotate") == 0 && v) {
            int k, deg;
            const char *eq = strchr(v, '=');
            if (!eq || sscanf(eq + 1, "%d", &deg) != 1 || (k = atoi(v)) < 0 || k >= MAX_WALLS
                || (deg != 0 && deg != 90 && deg != 180 && deg != 270)) {
                usage();
                return 2;
            }
            turn[k] = deg;
            i++;
        } else if (strcmp(a, "--presence") == 0 && v) {
            int k = atoi(v);
            const char *eq = strchr(v, '=');
            if (!eq || k < 0 || k >= MAX_WALLS || !id_ok(eq + 1)) {
                usage();
                return 2;
            }
            snprintf(presence_names[k], sizeof presence_names[k], "%s", eq + 1);
            i++;
        } else if (strcmp(a, "--name") == 0 && v) {
            int k = atoi(v);
            const char *eq = strchr(v, '=');
            if (!eq || k < 0 || k >= MAX_WALLS || !wall_name_ok(eq + 1)) {
                usage();
                return 2;
            }
            snprintf(wall_names[k], sizeof wall_names[k], "%s", eq + 1);
            i++;
        } else if (strcmp(a, "--workers") == 0 && v) {
            if (!knob_arg(v, 0, POOL_MAX, &pool_want)) {
                usage();
                return 2;
            }
            i++;
        } else if (strcmp(a, "--decoder-threads") == 0 && v) {
            if (!knob_arg(v, 1, 64, &decoder_threads)) {
                usage();
                return 2;
            }
            i++;
        } else if (strcmp(a, "--decoder-nice") == 0 && v) {
            if (!knob_arg(v, 0, 19, &decoder_nice)) {
                usage();
                return 2;
            }
            i++;
        } else {
            usage();
            return 2;
        }
    }
    if (!font_file)
        font_file = font_found(argv[0]);
    if (!font_file) {
        fprintf(stderr, "bitwall-paint: no %s beside the painter or among the fonts; "
                "--font-file says where\n", FONT_NAME);
        usage();
        return 2;
    }

#ifdef SIGPIPE
    signal(SIGPIPE, SIG_IGN);
#endif
    signal(SIGTERM, on_stop);
    signal(SIGINT, on_stop);

    shapes_init();
    palette_init();

    if (video_open(driver) < 0)
        return 1;
    if (run) {
        codecs_probe();
        pool_start();
        fprintf(stderr, "bitwall-paint: %d workers of %d cores; ffmpeg -threads %d in "
                "hardware and %d in software, nice %d\n",
                pool.size, machine_cores(), decoder_threads, decoder_soft, decoder_nice);
    }
    n = walls_wanted(walls, display_given, !run, driver);
    for (i = 0; i < n; i++) {
        int di = n == 1 ? display : i;
        s[i] = calloc(1, sizeof *s[i]);
        w[i] = NULL;
        in[i] = NULL;
        if (s[i] && face_alloc(s[i]) < 0) {
            free(s[i]);
            s[i] = NULL;
        }
        if (!s[i]) {
            fprintf(stderr, "bitwall-paint: out of memory\n");
            return 1;
        }
        s[i]->font_path = font_file;
        s[i]->nodraw = getenv("BITWALL_PAINT_NODRAW") != NULL;
        s[i]->turn = turn[di];
        if (screen_open(s[i], di, mode_w[di], mode_h[di]) < 0)
            return 1;
        apply_mode(s[i], mode_w[di], mode_h[di], driver);
    }
    walls_up(s, n);
    for (i = 0; i < n; i++) {
        pen_reset(&s[i]->pen);
        if (set_font(s[i], size) < 0) {
            fprintf(stderr, "bitwall-paint: cannot open font %s: %s\n",
                    font_file, TTF_GetError());
            return 1;
        }
        present(s[i]);
    }

    if (run) {
        for (i = 0; i < n; i++) {
            char name[33];
            w[i] = calloc(1, sizeof *w[i]);
            if (!w[i]) {
                fprintf(stderr, "bitwall-paint: out of memory\n");
                return 1;
            }
            w[i]->in.fd = -1;
            if (wall_names[i][0])
                snprintf(name, sizeof name, "%s", wall_names[i]);
            else
                snprintf(name, sizeof name, "%d", i);
            if (wall_claim(w[i], s[i], run, name) < 0)
                return 1;
            w[i]->font = size;
            snprintf(w[i]->presence_name, sizeof w[i]->presence_name, "%.32s",
                     presence_names[i]);
            fprintf(stderr, "bitwall-paint: wall %d: %s\n", i, w[i]->dir);
        }
        while (!stopping) {
            int wait = 20, ni = 0;
            double now;
            for (i = 0; i < n; i++)
                wait = wall_wait(w[i], wait);
            for (i = 0; i < n; i++) {
                int z;
                if (w[i]->base && w[i]->in.reader)
                    in[ni++] = &w[i]->in;
                for (z = 0; z < LAYERS; z++)
                    if (w[i]->above[z] && w[i]->above[z]->vin.reader)
                        in[ni++] = &w[i]->above[z]->vin;
            }
            inbox_wait(in, ni, wait);
            if (display_events())
                stopping = 1;
            now = now_ms();
            for (i = 0; i < n; i++) {
                Wall *wl = w[i];
                Screen *sc = s[i];
                Session *sess;
                wall_door(wl, now);
                if (wl->base && wl->in.reader) {
                    size_t room = sc->len < BUF_SIZE ? BUF_SIZE - sc->len : 0;
                    ssize_t got = room ? inbox_take(&wl->in, sc->raw, room < READ_SIZE
                                                    ? room : READ_SIZE, 0) : 0;
                    if (got < 0) {
                        if (wl->base)
                            session_end(wl->base, "eof");
                    } else if (got > 0) {
                        double t0 = now_ms(), pre = sc->t_flush + sc->t_swap;
                        memcpy(sc->buf + sc->len, sc->raw, (size_t)got);
                        sc->len += (size_t)got;
                        sc->bytes_in += (double)got;
                        wl->base->n_in += (double)got;
                        wall_data(wl);
                        sc->t_parse += now_ms() - t0 - (sc->t_flush + sc->t_swap - pre);
                        if (sc->len == BUF_SIZE)
                            sc->len = 0;
                    }
                }
                for (sess = wl->sessions; sess; sess = sess->next)
                    if (sess->role == ROLE_CONTROLLER && sess->state == S_OPEN)
                        controller_read(sess, now);
                    else if (sess->is_video && sess->state == S_OPEN)
                        video_read(sess, now);
                wall_tick(wl, now);
            }
        }
        for (i = 0; getenv("BITWALL_PAINT_STATS") && i < n; i++) {
            Screen *st = s[i];
            double secs = st->t_active / 1e3;
            double present_ms = st->t_flush + st->t_swap;
            char who[32] = "";
            if (!st->frames)
                continue;
            if (secs <= 0)
                secs = 1e-3;
            if (n > 1)
                snprintf(who, sizeof who, "wall %d: ", i);
            fprintf(stderr,
                    "bitwall-paint: %s%ld frames in %.2fs painting = %.1f fps (%s); per frame: "
                    "parse+paint %.2f ms, draw %.2f ms, swap %.2f ms\n",
                    who, st->frames, secs, st->frames / secs, st->backend,
                    (secs * 1e3 - present_ms) / st->frames,
                    st->t_flush / st->frames, st->t_swap / st->frames);
        }
        for (i = 0; i < n; i++)
            wall_free(w[i]);
        pool_stop();
        screens_free(s, n);
        return 0;
    }

    /* No run directory: a painter, not a receiver. It reads a paintable
     * stream on stdin and puts it on the glass, which is what a benchmark
     * or a test wants. */
    memset(&stdin_box, 0, sizeof stdin_box);
    if (inbox_start(&stdin_box, ipc_stdin()) < 0) {
        fprintf(stderr, "bitwall-paint: reader: %s\n", SDL_GetError());
        return 1;
    }
    in[0] = &stdin_box;
    for (;;) {
        ssize_t got;
        if (stopping)
            break;
        inbox_wait(in, 1, 100);
        if (display_events())
            break;
        got = inbox_take(&stdin_box, s[0]->buf + s[0]->len,
                         BUF_SIZE - s[0]->len, 0);
        if (got < 0)
            break;
        if (got > 0) {
            s[0]->len += (size_t)got;
            feed(s[0]);
            if (s[0]->len == BUF_SIZE)
                s[0]->len = 0;
        }
    }
    inbox_stop(&stdin_box);
    screens_free(s, n);
    return 0;
}
#endif

/* The seams' implementations for this machine (platform.h): a Mac's, or
 * a Pi's. The display goes last, since on a Pi what it includes brings
 * Xlib's names with it. */
#ifdef BITWALL_MAC
#include "gpu-gl.c"
#include "child-posix.c"
#include "probe-mac.c"
#include "machine-mac.c"
#include "ipc-posix.c"
#include "display-sdl.c"
#elif defined(BITWALL_WIN)
#include "gpu-win.c"
#include "child-win.c"
#include "probe-win.c"
#include "machine-win.c"
#include "ipc-win.c"
#include "display-sdl.c"
#else
#include "gpu-gles.c"
#include "child-posix.c"
#include "probe-v4l2.c"
#include "machine-linux.c"
#include "ipc-posix.c"
#include "display-kms.c"
#endif
