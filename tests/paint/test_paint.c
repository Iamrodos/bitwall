/*
 * What the Pi's painter must keep being true, run on any machine with a
 * C compiler.
 *
 *     tests/paint/run            or as part of tests/bitwall-tests
 *
 * The painter is included whole, so every static function in it can be
 * called here, and SDL, SDL_ttf, GLES2 and libdrm are the fakes in stub/.
 * The fake font stamps each glyph's code point into its mask, so a test
 * reads back from the atlas which character a cell really shows, rather
 * than trusting the painter's own account of it.
 *
 * Each test runs in a child process of its own. A crash, a sanitizer
 * report or a hang is that test failing, and the rest still run.
 */
#include "../../bitwall/receivers/pi/bitwall-paint.c"

#include <limits.h>
#include <sys/wait.h>

#include "fake.h"

/* ------------------------------------------------------------- harness */

#define CHECK(cond, ...)                                                  \
    do {                                                                  \
        if (!(cond)) {                                                    \
            fprintf(stderr, "%s:%d: %s: ", __FILE__, __LINE__, #cond);    \
            fprintf(stderr, __VA_ARGS__);                                 \
            fprintf(stderr, "\n");                                        \
            exit(1);                                                      \
        }                                                                 \
    } while (0)

typedef struct {
    const char *name;
    void (*fn)(void);
} Test;

/* A code point read back from the atlas that the font did not draw: a
 * block or a shade. The second half of a wide one is the painter's TAIL. */
#define NOT_TYPE 0xFFFFFFFFu

static uint32_t lcg(uint32_t *seed)
{
    *seed = *seed * 1103515245u + 12345u;
    return *seed >> 8;
}

/* A screen on the GPU path, brought up the way the painter does it. */
static Screen *gpu_screen(int font)
{
    Screen *s = calloc(1, sizeof *s);
    CHECK(s, "out of memory");
    s->width = fake.win_w;
    s->height = fake.win_h;
    s->font_path = "fake";
    s->lit = 1;
    s->drm = -1;
    s->win = SDL_CreateWindow("t", 0, 0, s->width, s->height, SDL_WINDOW_OPENGL);
    CHECK(gl_setup(s) == 0, "gl_setup failed");
    s->gpu = 1;
    pen_reset(&s->pen);
    CHECK(set_font(s, font) == 0, "set_font failed");
    return s;
}

/* A screen on the software path, painting the fake window surface. */
static Screen *soft_screen(int font)
{
    Screen *s = calloc(1, sizeof *s);
    CHECK(s, "out of memory");
    s->width = fake.win_w;
    s->height = fake.win_h;
    s->font_path = "fake";
    s->lit = 1;
    s->drm = -1;
    s->win = SDL_CreateWindow("t", 0, 0, s->width, s->height, 0);
    s->glass = SDL_GetWindowSurface(s->win);
    s->paper = SDL_CreateRGBSurfaceWithFormat(0, s->width, s->height, 32, 0);
    s->target = s->glass;
    pen_reset(&s->pen);
    CHECK(set_font(s, font) == 0, "set_font failed");
    return s;
}

/* Bytes into the parser as the main loop hands them over. */
static void send_bytes(Screen *s, const void *data, size_t n)
{
    const uint8_t *p = data;
    while (n) {
        size_t k = BUF_SIZE - s->len;
        if (k > n)
            k = n;
        memcpy(s->buf + s->len, p, k);
        s->len += k;
        p += k;
        n -= k;
        feed(s);
        if (s->len == BUF_SIZE)
            s->len = 0;
    }
}

static void write_text(Screen *s, const char *text)
{
    send_bytes(s, text, strlen(text));
}

/* The same bytes, cut wherever a random pipe would cut them. */
static void send_split(Screen *s, const uint8_t *p, size_t n, uint32_t seed, size_t most)
{
    while (n) {
        size_t k = 1 + lcg(&seed) % most;
        if (k > n)
            k = n;
        send_bytes(s, p, k);
        p += k;
        n -= k;
    }
}

static const Vert *vert_at(Screen *s, int x, int y)
{
    int per = s->gl.points ? 1 : 4;
    CHECK(x >= 0 && y >= 0 && x < s->cols && y < s->rows, "cell %d,%d is off the grid", x, y);
    return &s->gl.verts[(size_t)(y * s->cols + x) * per];
}

/* What cell x,y shows, read from the screen's own atlas where its mask
 * begins. */
static uint32_t shown(Screen *s, int x, int y)
{
    const Vert *v = vert_at(s, x, y);
    const FakeTexture *t = fake_texture(s->gl.atlas);
    long px = ((long)v->u * t->w + 32767) / 65535;
    long py = ((long)v->v * t->h + 32767) / 65535;
    const uint8_t *a;
    CHECK(t->pixels, "no atlas was made");
    CHECK(px + 3 <= t->w && py < t->h, "cell %d,%d points outside the atlas", x, y);
    a = t->pixels + (size_t)py * t->w + px;
    if (!a[0] && !a[1] && !a[2])
        return ' ';
    if (!(a[2] & FAKE_MARK) || (a[0] == 255 && a[1] == 255 && a[2] == 255))
        return NOT_TYPE;
    return ((uint32_t)a[0] | (uint32_t)a[1] << 8 | (uint32_t)(a[2] & 0x1F) << 16)
        | ((a[2] & FAKE_TAIL) ? TAIL : 0);
}

static Rgb ink_at(Screen *s, int x, int y)
{
    const Vert *v = vert_at(s, x, y);
    Rgb c = {v->ink[0], v->ink[1], v->ink[2]};
    return c;
}

static Rgb ground_at(Screen *s, int x, int y)
{
    const Vert *v = vert_at(s, x, y);
    Rgb c = {v->ground[0], v->ground[1], v->ground[2]};
    return c;
}

static int same_rgb(Rgb a, Rgb b)
{
    return a.r == b.r && a.g == b.g && a.b == b.b;
}

static size_t verts_bytes(Screen *s)
{
    return sizeof(Vert) * (size_t)s->cols * s->rows * (s->gl.points ? 1 : 4);
}

/* ---------------------------------------------------------------- utf-8 */

static const uint32_t SAMPLES[] = {0x41, 0x7F, 0xE9, 0x7FF, 0x800, 0x20AC,
                                   0x2588, 0xFFFD, 0xFFFF, 0x10000, 0x1F600, 0x10FFFF};

static void utf8_decodes_every_length_whole(void)
{
    size_t i;
    for (i = 0; i < sizeof SAMPLES / sizeof SAMPLES[0]; i++) {
        char u8[5];
        uint32_t cp = 0;
        int n = utf8_encode(SAMPLES[i], u8);
        size_t used = utf8_decode((const uint8_t *)u8, (size_t)n, &cp);
        CHECK(used == (size_t)n, "U+%04X took %zu of %d bytes", SAMPLES[i], used, n);
        CHECK(cp == SAMPLES[i], "U+%04X came back as U+%04X", SAMPLES[i], cp);
        CHECK(utf8_incomplete((const uint8_t *)u8, (size_t)n) == 0,
              "a whole U+%04X is held as incomplete", SAMPLES[i]);
    }
}

static void utf8_holds_every_incomplete_prefix(void)
{
    size_t i;
    int k;
    for (i = 0; i < sizeof SAMPLES / sizeof SAMPLES[0]; i++) {
        uint8_t buf[8] = {'x', 'y'};
        char u8[5];
        int n = utf8_encode(SAMPLES[i], u8);
        memcpy(buf + 2, u8, (size_t)n);
        for (k = 1; k < n; k++) {
            uint32_t cp = 0;
            size_t held = utf8_incomplete(buf, 2 + (size_t)k);
            CHECK(held == (size_t)k, "U+%04X cut after %d bytes: %zu held", SAMPLES[i], k, held);
            CHECK(utf8_decode(buf + 2, (size_t)k, &cp) == 1 && cp == 0xFFFD,
                  "U+%04X cut after %d decoded as U+%04X", SAMPLES[i], k, cp);
        }
    }
}

static void utf8_malformed_costs_one_byte(void)
{
    static const struct { const char *bytes; size_t n; } bad[] = {
        {"\x80", 1}, {"\xBF", 1}, {"\xFF", 1}, {"\xF8\x88\x80\x80\x80", 5},
        {"\xE2\x28\xA1", 3}, {"\xED\xA0\x80", 3}, {"\xF4\x90\x80\x80", 4}};
    size_t i;
    for (i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        uint32_t cp = 0;
        size_t used = utf8_decode((const uint8_t *)bad[i].bytes, bad[i].n, &cp);
        CHECK(cp == 0xFFFD, "bad sequence %zu decoded as U+%04X", i, cp);
        CHECK(used >= 1 && used <= bad[i].n, "bad sequence %zu took %zu bytes", i, used);
    }
}

static void utf8_split_at_every_byte_reaches_the_cell_whole(void)
{
    static const char frame[] = "\x1b[1;1H" "\xc3\xa9" "\xe2\x82\xac" "\xf0\x9f\x98\x80"
                                "A" "\xe2\x86\x92" "\x1b[0m";
    static const uint32_t want[] = {0xE9, 0x20AC, 0x1F600, 0x1F600 | TAIL, 'A', 0x2192};
    Screen *s = gpu_screen(14);
    size_t n = strlen(frame), cut;
    for (cut = 1; cut < n; cut++) {
        int x;
        write_text(s, "\x1b[1;1H        ");
        send_bytes(s, frame, cut);
        send_bytes(s, frame + cut, n - cut);
        CHECK(s->len == 0, "cut at %zu left %zu bytes waiting", cut, s->len);
        for (x = 0; x < 6; x++)
            CHECK(shown(s, x, 0) == want[x], "cut at %zu: cell %d shows U+%04X, not U+%04X",
                  cut, x, shown(s, x, 0), want[x]);
    }
}

/* ------------------------------------------------------------------ sgr */

/* The parameters in memory of exactly their own length, so reading past
 * the last one is a sanitizer report rather than a lucky zero. */
static Pen pen_after(const int *v, int n)
{
    Pen p;
    int *exact = malloc(sizeof *exact * (size_t)(n ? n : 1));
    CHECK(exact, "out of memory");
    memcpy(exact, v, sizeof *v * (size_t)n);
    pen_reset(&p);
    pen_take(&p, exact, n);
    free(exact);
    return p;
}

static void sgr_truecolour_sets_ink_and_ground(void)
{
    Screen *s = gpu_screen(14);
    Rgb fg = {10, 20, 30}, bg = {40, 50, 60};
    write_text(s, "\x1b[3;4H\x1b[38;2;10;20;30;48;2;40;50;60mX");
    CHECK(shown(s, 3, 2) == 'X', "X is not where it was put");
    CHECK(same_rgb(ink_at(s, 3, 2), fg), "ink is %d,%d,%d", ink_at(s, 3, 2).r,
          ink_at(s, 3, 2).g, ink_at(s, 3, 2).b);
    CHECK(same_rgb(ground_at(s, 3, 2), bg), "ground is wrong");
    write_text(s, "\x1b[38;2;300;256;511mY");
    CHECK(same_rgb(ink_at(s, 4, 2), ((Rgb){44, 0, 255})), "out of range channels not masked");
}

static void sgr_256_colour_uses_the_xterm_cube(void)
{
    Screen *s = gpu_screen(14);
    write_text(s, "\x1b[1;1H\x1b[38;5;196;48;5;21mX\x1b[38;5;232;48;5;255mY\x1b[38;5;999mZ");
    CHECK(same_rgb(ink_at(s, 0, 0), ((Rgb){255, 0, 0})), "196 is not red");
    CHECK(same_rgb(ground_at(s, 0, 0), ((Rgb){0, 0, 255})), "21 is not blue");
    CHECK(same_rgb(ink_at(s, 1, 0), ((Rgb){8, 8, 8})), "232 is not the first grey");
    CHECK(same_rgb(ground_at(s, 1, 0), ((Rgb){238, 238, 238})), "255 is not the last grey");
    CHECK(same_rgb(ink_at(s, 2, 0), PALETTE[999 & 255]), "999 not masked to the palette");
}

static void sgr_named_bold_reverse_and_defaults(void)
{
    Pen p;
    int red[] = {31}, bold_red[] = {1, 31}, bright[] = {91}, blue_bg[] = {44},
        rev[] = {31, 44, 7}, back[] = {31, 44, 39, 49}, reset[] = {31, 0};
    p = pen_after(red, 1);
    CHECK(same_rgb(p.ink, PALETTE[1]), "31 is not red");
    p = pen_after(bold_red, 2);
    CHECK(same_rgb(p.ink, PALETTE[9]) && p.bold, "bold 31 is not bright red");
    p = pen_after(bright, 1);
    CHECK(same_rgb(p.ink, PALETTE[9]), "91 is not bright red");
    p = pen_after(blue_bg, 1);
    CHECK(same_rgb(p.ground, PALETTE[4]), "44 is not blue ground");
    p = pen_after(rev, 3);
    CHECK(same_rgb(p.ink, PALETTE[4]) && same_rgb(p.ground, PALETTE[1]), "reverse did not swap");
    p = pen_after(back, 4);
    CHECK(same_rgb(p.ink, INK) && same_rgb(p.ground, GROUND), "39;49 are not the defaults");
    p = pen_after(reset, 2);
    CHECK(!p.fg_set && same_rgb(p.ink, INK), "0 did not reset");
}

static void sgr_truncated_colour_sets_no_colour(void)
{
    static const int forms[][5] = {{38}, {38, 5}, {38, 2}, {38, 2, 1}, {38, 2, 1, 2},
                                   {48}, {48, 5}, {48, 2, 1, 2}};
    static const int lens[] = {1, 2, 2, 3, 4, 1, 2, 4};
    size_t i;
    Screen *s = gpu_screen(14);
    for (i = 0; i < sizeof lens / sizeof lens[0]; i++) {
        Pen p = pen_after(forms[i], lens[i]);
        CHECK(!p.fg_set && !p.bg_set, "truncated form %zu set a colour", i);
    }
    /* And from the stream, where the form arrives cut by the sequence end. */
    write_text(s, "\x1b[1;1H\x1b[38;2;1mA\x1b[48;5mB\x1b[38mC");
    CHECK(same_rgb(ink_at(s, 0, 0), INK) && same_rgb(ground_at(s, 1, 0), GROUND)
          && same_rgb(ink_at(s, 2, 0), INK), "a truncated form coloured a cell");
}

static void sgr_with_more_parameters_than_kept_is_safe(void)
{
    char seq[512];
    size_t at = 0;
    int i;
    Screen *s = gpu_screen(14);
    at += (size_t)snprintf(seq + at, sizeof seq - at, "\x1b[1;1H\x1b[");
    for (i = 0; i < 60; i++)
        at += (size_t)snprintf(seq + at, sizeof seq - at, "1;");
    snprintf(seq + at, sizeof seq - at, "31mQ");
    write_text(s, seq);
    CHECK(s->len == 0, "the sequence was not consumed");
}

static void sgr_private_forms_leave_the_pen_alone(void)
{
    Screen *s = gpu_screen(14);
    write_text(s, "\x1b[1;1H\x1b[31m\x1b[>4;2mA");
    CHECK(same_rgb(ink_at(s, 0, 0), PALETTE[1]), "a private SGR changed the pen");
}

/* ------------------------------------------------------ frame delimiter */

static void a_bare_reset_presents_one_frame(void)
{
    Screen *s = gpu_screen(14);
    long before = s->frames;
    write_text(s, "\x1b[1;1HA");
    CHECK(s->frames == before, "presented before the frame ended");
    write_text(s, "\x1b[0m");
    CHECK(s->frames == before + 1, "a bare ESC[0m did not present");
    write_text(s, "\x1b[1;1HB\x1b[m");
    CHECK(s->frames == before + 2, "a bare ESC[m did not present");
}

static void a_reset_with_parameters_is_not_a_frame(void)
{
    Screen *s = gpu_screen(14);
    long before = s->frames;
    write_text(s, "\x1b[1;1HA\x1b[0;31mB");
    CHECK(s->frames == before, "ESC[0;31m ended a frame");
    CHECK(same_rgb(ink_at(s, 1, 0), PALETTE[1]), "ESC[0;31m did not colour");
}

static void a_bare_reset_ends_the_pen_too(void)
{
    Screen *s = gpu_screen(14);
    write_text(s, "\x1b[1;1H\x1b[31;44mA\x1b[0mB");
    CHECK(same_rgb(ink_at(s, 1, 0), INK) && same_rgb(ground_at(s, 1, 0), GROUND),
          "the pen outlived the frame");
}

static void a_delimiter_cut_in_half_presents_once_it_arrives(void)
{
    Screen *s = gpu_screen(14);
    long before = s->frames;
    write_text(s, "\x1b[1;1HA\x1b[");
    CHECK(s->frames == before, "presented on half a delimiter");
    write_text(s, "0");
    CHECK(s->frames == before, "presented on two thirds of a delimiter");
    write_text(s, "m");
    CHECK(s->frames == before + 1, "the whole delimiter did not present");
}

static void a_frame_that_drew_nothing_presents_nothing(void)
{
    Screen *s = gpu_screen(14);
    long before;
    write_text(s, "\x1b[0m");
    before = s->frames;
    write_text(s, "\x1b[0m\x1b[0m\x1b[5;5H\x1b[0m");
    CHECK(s->frames == before, "an empty frame was presented");
}

/* ------------------------------------------------------------------ cup */

static void cup_is_one_based_with_defaults(void)
{
    static const struct { const char *seq; int x, y; } cases[] = {
        {"\x1b[H", 0, 0}, {"\x1b[3;5H", 4, 2}, {"\x1b[;7H", 6, 0}, {"\x1b[0;0H", 0, 0},
        {"\x1b[4f", 0, 3}, {"\x1b[2;H", 0, 1}};
    size_t i;
    Screen *s = gpu_screen(14);
    for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        write_text(s, "\x1b[9;9H");
        write_text(s, cases[i].seq);
        CHECK(s->x == cases[i].x && s->y == cases[i].y, "case %zu put the cursor at %d,%d",
              i, s->x, s->y);
    }
}

static void cup_off_the_grid_draws_nothing(void)
{
    Screen *g = gpu_screen(14), *w = soft_screen(14);
    size_t n = verts_bytes(g);
    uint8_t *before = malloc(n);
    size_t pix = (size_t)w->glass->pitch * w->glass->h;
    uint8_t *glass = malloc(pix);
    char seq[64];
    CHECK(before && glass, "out of memory");
    memcpy(before, g->gl.verts, n);
    memcpy(glass, w->glass->pixels, pix);
    snprintf(seq, sizeof seq, "\x1b[%d;1HZZ\x1b[1;%dHZZ\x1b[9999;9999HZZ", g->rows + 1, g->cols + 1);
    write_text(g, seq);
    write_text(w, seq);
    CHECK(memcmp(before, g->gl.verts, n) == 0, "GPU: a cell off the grid changed the grid");
    CHECK(memcmp(glass, w->glass->pixels, pix) == 0, "software: a cell off the grid drew");
}

static void cup_with_huge_numbers_does_not_overflow(void)
{
    Screen *s = gpu_screen(14);
    write_text(s, "\x1b[99999999999999999999;99999999999999999999HA");
    CHECK(s->y >= s->rows && s->x >= s->cols, "a huge CUP landed on the grid at %d,%d", s->x, s->y);
}

static void cup_to_the_last_column_clips_a_wide_glyph(void)
{
    Screen *g = gpu_screen(14), *w = soft_screen(14);
    char seq[64];
    snprintf(seq, sizeof seq, "\x1b[%d;%dH\xf0\x9f\x98\x80", g->rows, g->cols);
    write_text(g, seq);
    write_text(w, seq);
    CHECK(shown(g, g->cols - 1, g->rows - 1) == 0x1F600, "the wide glyph's head is missing");
}

/* ------------------------------------------------------------------ fuzz */

/* One well-formed token of what a sender might write. */
static size_t token(uint8_t *out, uint32_t *seed, int cols, int rows)
{
    static const char *text[] = {"a", "Z", " ", "\xc3\xa9", "\xe2\x82\xac", "\xf0\x9f\x98\x80",
                                 "\xe2\x96\x80", "\xe2\x96\x88", "\xe2\x96\x8c", "\xe2\x96\x91",
                                 "\xe2\xa0\x81", "\xe2\x94\x80", "\xef\xbd\xa6", "\x80", "\xe2\x28"};
    static const char *strings[] = {"\x1b_bitwall report\x1b\\", "\x1b_somebody else\x1b\\",
                                    "\x1b_bitwall blank 0\x1b\\", "\x1b]0;title\x07",
                                    "\x1b]2;x\x1b\\", "\x1bP1$r\x1b\\", "\0337", "\x1b[?25l",
                                    "\x1b[2J", "\x1b[0m", "\x1b[m", "\n", "\t"};
    uint32_t r = lcg(seed) % 10;
    int i, n;
    size_t at = 0;
    if (r < 2)
        return (size_t)sprintf((char *)out, "\x1b[%u;%uH", lcg(seed) % (unsigned)(rows + 4),
                               lcg(seed) % (unsigned)(cols + 4));
    if (r < 4) {
        switch (lcg(seed) % 6) {
        case 0: return (size_t)sprintf((char *)out, "\x1b[38;2;%u;%u;%um", lcg(seed) % 256,
                                       lcg(seed) % 256, lcg(seed) % 256);
        case 1: return (size_t)sprintf((char *)out, "\x1b[48;5;%um", lcg(seed) % 300);
        case 2: return (size_t)sprintf((char *)out, "\x1b[%um", 30 + lcg(seed) % 80);
        case 3: return (size_t)sprintf((char *)out, "\x1b[38;2;%um", lcg(seed) % 256);
        case 4: return (size_t)sprintf((char *)out, "\x1b[1;7;22;27m");
        default: return (size_t)sprintf((char *)out, "\x1b[48;2m");
        }
    }
    if (r < 5) {
        const char *t = strings[lcg(seed) % (sizeof strings / sizeof strings[0])];
        memcpy(out, t, strlen(t));
        return strlen(t);
    }
    n = 1 + (int)(lcg(seed) % 40);
    for (i = 0; i < n; i++) {
        const char *t = text[lcg(seed) % (sizeof text / sizeof text[0])];
        memcpy(out + at, t, strlen(t));
        at += strlen(t);
    }
    return at;
}

static uint8_t *stream_of(uint32_t seed, int tokens, int cols, int rows, size_t *len)
{
    uint8_t *buf = malloc((size_t)tokens * 200);
    size_t at = 0;
    int i;
    CHECK(buf, "out of memory");
    for (i = 0; i < tokens; i++)
        at += token(buf + at, &seed, cols, rows);
    *len = at;
    return buf;
}

static void fuzz_random_splits_paint_what_whole_input_paints(void)
{
    uint32_t seed;
    for (seed = 1; seed <= 12; seed++) {
        Screen *whole = gpu_screen(14), *cut = gpu_screen(14);
        size_t n;
        uint8_t *stream = stream_of(seed, 3000, whole->cols, whole->rows, &n);
        send_bytes(whole, stream, n);
        send_split(cut, stream, n, seed * 7919u, 1 + seed * seed * 5);
        CHECK(whole->x == cut->x && whole->y == cut->y, "seed %u: cursor differs", seed);
        CHECK(whole->frames == cut->frames, "seed %u: %ld frames whole, %ld cut", seed,
              whole->frames, cut->frames);
        CHECK(whole->len == cut->len, "seed %u: %zu bytes held whole, %zu cut", seed,
              whole->len, cut->len);
        CHECK(memcmp(whole->gl.verts, cut->gl.verts, verts_bytes(whole)) == 0,
              "seed %u: the grid differs between whole and cut input", seed);
        free(stream);
    }
}

static void fuzz_garbage_is_survived_on_both_paths(void)
{
    static const uint8_t alphabet[] = {0x1b, 0x1b, 0x1b, '[', '[', '_', ']', 'P', '\\', ';', ';',
                                       '0', '1', '2', '3', '8', '9', 'm', 'H', 'f', 'J', 0x07,
                                       'a', ' ', 0x80, 0xBF, 0xC3, 0xE2, 0xF0, 0xFF, '?', '>'};
    uint32_t seed;
    for (seed = 1; seed <= 6; seed++) {
        Screen *g = gpu_screen(8 + (int)seed * 3), *w = soft_screen(8 + (int)seed * 3);
        size_t n = 200000, i;
        uint8_t *junk = malloc(n);
        uint32_t k = seed;
        CHECK(junk, "out of memory");
        for (i = 0; i < n; i++)
            junk[i] = alphabet[lcg(&k) % sizeof alphabet];
        send_split(g, junk, n, seed, 5000);
        send_split(w, junk, n, seed, 5000);
        CHECK(g->len < BUF_SIZE && w->len < BUF_SIZE, "the buffer filled");
        CHECK(g->len <= MAX_APC + 2 && w->len <= MAX_APC + 2,
              "held %zu and %zu bytes of garbage", g->len, w->len);
        free(junk);
    }
}

/* ---------------------------------------------------------------- atlas */

/* Distinct code points one after another in one cell, `n` of them, from
 * the CJK block so that the fake font draws each two cells wide. */
static void churn(Screen *s, int x, int y, uint32_t first, int n)
{
    int i;
    for (i = 0; i < n; i++) {
        char seq[32], u8[5];
        utf8_encode(first + (uint32_t)i, u8);
        snprintf(seq, sizeof seq, "\x1b[%d;%dH%s\x1b[0m", y + 1, x + 1, u8);
        write_text(s, seq);
    }
}

/* A font whose cell leaves pixels over in both directions, so a grid
 * pinned to a corner and a grid in the middle are told apart. */
static Screen *gpu_screen_with_spare(void)
{
    int font;
    for (font = 11; font < 60; font++) {
        Screen *s = gpu_screen(font);
        if (s->width - s->cols * s->cw >= 2 && s->height - s->rows * s->ch >= 2)
            return s;
        free(s);
    }
    CHECK(0, "no font leaves spare pixels both ways");
    return NULL;
}

static void the_grid_sits_in_the_middle_of_the_glass(void)
{
    Screen *s = gpu_screen_with_spare();
    int ox = (s->width - s->cols * s->cw) / 2, oy = (s->height - s->rows * s->ch) / 2;
    const Vert *first = vert_at(s, 0, 0), *last = vert_at(s, s->cols - 1, s->rows - 1);
    CHECK(first->x == ox && first->y == oy, "the first cell is at %d,%d, not %d,%d",
          first->x, first->y, ox, oy);
    CHECK(last->x + s->cw <= s->width && last->y + s->ch <= s->height,
          "the last cell runs off the glass at %d,%d", last->x, last->y);
    CHECK(s->width - (last->x + s->cw) - ox <= 1 && s->height - (last->y + s->ch) - oy <= 1,
          "the spare is not split evenly");
}

static void every_frame_clears_the_glass_before_the_grid(void)
{
    /* The grid does not cover the glass; what it leaves over has to be
     * put back to black, or it keeps the last picture that reached it. */
    Screen *s = gpu_screen_with_spare();
    int i;
    for (i = 0; i < 5; i++)
        write_text(s, "\x1b[1;1HA\x1b[0m");
    CHECK(fake.swaps >= 5, "only %d frames were put up", fake.swaps);
    CHECK(fake.swaps_uncleared == 0, "%d of %d frames were put up over the last one",
          fake.swaps_uncleared, fake.swaps);
}

static void a_static_cell_keeps_its_glyph_when_the_atlas_fills(void)
{
    /* A big font, so the atlas holds few masks and fills quickly. */
    Screen *s = gpu_screen(200);
    int total = s->gl.slot_cols * s->gl.slot_rows;
    write_text(s, "\x1b[1;1HA\x1b[0m");
    CHECK(shown(s, 0, 0) == 'A', "A was not drawn");
    churn(s, 2, 0, 0x4E00, total + 10);
    CHECK(shown(s, 0, 0) == 'A', "after the atlas filled, A shows U+%04X", shown(s, 0, 0));
    CHECK(shown(s, 2, 0) == 0x4E00 + (uint32_t)total + 9, "the churned cell is wrong");
}

static void a_static_cell_keeps_its_glyph_when_the_table_fills(void)
{
    Screen *s = gpu_screen(14);
    write_text(s, "\x1b[1;1HA\x1b[3;3H\xf0\x9f\x98\x80\x1b[0m");
    churn(s, 10, 5, 0x4E00, CACHE + 50);
    CHECK(shown(s, 0, 0) == 'A', "after the table filled, A shows U+%04X", shown(s, 0, 0));
    CHECK(shown(s, 2, 2) == 0x1F600 && shown(s, 3, 2) == (0x1F600 | TAIL),
          "after the table filled, a wide glyph shows U+%04X U+%04X",
          shown(s, 2, 2), shown(s, 3, 2));
}

static void the_atlas_has_room_for_the_grid_twice_over(void)
{
    static const int fonts[] = {4, 9, 14, 20, 28, 60, 200};
    size_t i;
    fake.max_texture = 8192;
    fake.win_w = 3840;
    fake.win_h = 2160;
    for (i = 0; i < sizeof fonts / sizeof fonts[0]; i++) {
        Screen *s = gpu_screen(fonts[i]);
        long slots = (long)s->gl.slot_cols * s->gl.slot_rows;
        CHECK(slots >= 2L * s->cols * s->rows, "font %d: %ld slots for %d cells", fonts[i],
              slots, s->cols * s->rows);
        CHECK(s->gl.atlas_w <= fake.max_texture && s->gl.atlas_h <= fake.max_texture,
              "font %d: atlas %dx%d is bigger than the GPU allows", fonts[i],
              s->gl.atlas_w, s->gl.atlas_h);
        CHECK(fake.atlas_w == s->gl.atlas_w && fake.atlas_h == s->gl.atlas_h,
              "font %d: the texture is not the atlas the painter thinks it is", fonts[i]);
    }
}

static void a_grid_with_more_glyphs_than_the_atlas_holds_is_survived(void)
{
    /* A GPU too small for the picture: every cell a different glyph. */
    Screen *s;
    int x, y;
    fake.max_texture = 256;
    s = gpu_screen(14);
    for (y = 0; y < s->rows; y++) {
        char line[4096];
        size_t at = (size_t)snprintf(line, sizeof line, "\x1b[%d;1H", y + 1);
        for (x = 0; x < s->cols; x++)
            at += (size_t)utf8_encode(0x10000 + (uint32_t)(y * s->cols + x), line + at);
        snprintf(line + at, sizeof line - at, "\x1b[0m");
        write_text(s, line);
    }
    CHECK(shown(s, s->cols - 1, s->rows - 1) == 0x10000 + (uint32_t)(s->cols * s->rows - 1),
          "the last cell drawn does not show its glyph");
}

static void a_cell_too_big_for_the_atlas_draws_nothing(void)
{
    Screen *s;
    int x;
    fake.max_texture = 256;
    s = gpu_screen(400);
    CHECK(s->gl.slot_cols == 0 || s->gl.slot_rows == 0, "the cell fits after all");
    write_text(s, "\x1b[1;1HA\xf0\x9f\x98\x80\xe2\x96\x88\xe2\x96\x91\x1b[0m");
    for (x = 0; x < s->cols && x < 5; x++)
        CHECK(shown(s, x, 0) == ' ', "cell %d shows U+%04X", x, shown(s, x, 0));
}

static void a_gpu_that_offers_no_texture_is_not_used(void)
{
    Screen *s = calloc(1, sizeof *s);
    CHECK(s, "out of memory");
    fake.max_texture = 0;
    s->width = fake.win_w;
    s->height = fake.win_h;
    s->win = SDL_CreateWindow("t", 0, 0, s->width, s->height, SDL_WINDOW_OPENGL);
    CHECK(gl_setup(s) != 0, "a GPU with no texture size was taken on");
}

/* ----------------------------------------------------------- door, base */
/*
 * The painter's own door and base, at the C level. The protocol itself --
 * the command table, backpressure, pictures, mirrors, presence, meta, the
 * life of a session -- is driven against the running painter by
 * tests/bitwall-contracts; here are the painter's internals a black-box
 * test cannot reach: the base drives the glass, a font keeps the picture,
 * a sender gone clears the screen, and the walls stay apart.
 */

static char run_dir[256];
static int run_made;
static Wall *WALLS[MAX_WALLS];
static Screen *SCRS[MAX_WALLS];
static int NW;

static void tidy_run_dir(void)
{
    char walls[400];
    snprintf(walls, sizeof walls, "%s/bitwall", run_dir);
    rmrf(walls);
    rmdir(run_dir);
}

static void a_run_dir(void)
{
    const char *tmp = getenv("TMPDIR");
    CHECK(!run_made++, "one run directory a test");
    snprintf(run_dir, sizeof run_dir, "%s/bitwall-paint-%ld",
             tmp && *tmp ? tmp : "/tmp", (long)getpid());
    CHECK(mkdir(run_dir, 0700) == 0 || errno == EEXIST, "mkdir %s: %s", run_dir,
          strerror(errno));
    atexit(tidy_run_dir);
}

/* A wall claimed on screen `s`, under a fresh run directory, and registered
 * so the loop below services it. */
static Wall *wall_at(Screen *s, const char *name)
{
    Wall *w = calloc(1, sizeof *w);
    CHECK(w, "out of memory");
    w->in.fd = -1;
    if (!run_made)
        a_run_dir();
    CHECK(wall_claim(w, s, run_dir, name) == 0, "wall_claim %s failed", name);
    w->font = 14;
    WALLS[NW] = w;
    SCRS[NW] = s;
    NW++;
    return w;
}

/* One time round the service loop for every registered wall, for `secs`. */
static void pump(double secs)
{
    double t0 = now_ms();
    do {
        double now;
        int i, ni = 0;
        Inbox *inb[MAX_WALLS * LAYERS];
        for (i = 0; i < NW; i++) {
            int z;
            if (WALLS[i] && WALLS[i]->base && WALLS[i]->in.reader)
                inb[ni++] = &WALLS[i]->in;
            for (z = 0; WALLS[i] && z < LAYERS; z++)
                if (WALLS[i]->above[z] && WALLS[i]->above[z]->vin.reader)
                    inb[ni++] = &WALLS[i]->above[z]->vin;
        }
        inbox_wait(inb, ni, 5);
        now = now_ms();
        for (i = 0; i < NW; i++) {
            Wall *w = WALLS[i];
            Screen *s = SCRS[i];
            Session *ss;
            if (!w)
                continue;
            wall_door(w, now);
            if (w->base && w->in.reader) {
                size_t room = s->len < BUF_SIZE ? BUF_SIZE - s->len : 0;
                ssize_t got = room ? inbox_take(&w->in, s->raw,
                                                room < READ_SIZE ? room : READ_SIZE, 0) : 0;
                if (got < 0) {
                    if (w->base)
                        session_end(w->base, "eof");
                } else if (got > 0) {
                    memcpy(s->buf + s->len, s->raw, (size_t)got);
                    s->len += (size_t)got;
                    wall_data(w);
                    if (s->len == BUF_SIZE)
                        s->len = 0;
                }
            }
            for (ss = w->sessions; ss; ss = ss->next)
                if (ss->role == ROLE_CONTROLLER && ss->state == S_OPEN)
                    controller_read(ss, now);
                else if (ss->is_video && ss->state == S_OPEN)
                    video_read(ss, now);
            wall_tick(w, now);
        }
    } while (now_ms() - t0 < secs * 1e3);
}

static void knock(Wall *w, const char *req)
{
    char door[700];
    int fd;
    snprintf(door, sizeof door, "%s/door.in", w->dir);
    fd = open(door, O_WRONLY | O_NONBLOCK);
    CHECK(fd >= 0, "nothing reads %s: %s", door, strerror(errno));
    dprintf(fd, "%s\n", req);
    close(fd);
}

/* A session's out and in, opened after the wall has made them. */
static int sess_out(Wall *w, const char *name)
{
    char path[800];
    int fd, i;
    snprintf(path, sizeof path, "%s/s/%s/out", w->dir, name);
    for (i = 0; i < 200; i++) {
        fd = open(path, O_RDONLY | O_NONBLOCK);
        if (fd >= 0)
            return fd;
        pump(0.01);
    }
    CHECK(0, "no out for %s", name);
    return -1;
}

static int sess_in(Wall *w, const char *name)
{
    char path[800];
    int fd;
    snprintf(path, sizeof path, "%s/s/%s/in", w->dir, name);
    fd = open(path, O_WRONLY);
    CHECK(fd >= 0, "no in for %s: %s", name, strerror(errno));
    return fd;
}

/* Everything waiting on a session's out, as text. */
static char *out_text(int fd)
{
    static char text[1 << 18];
    size_t got = 0;
    ssize_t n;
    while (got < sizeof text - 1) {
        n = read(fd, text + got, sizeof text - 1 - got);
        if (n > 0)
            got += (size_t)n;
        else
            break;
    }
    text[got] = 0;
    return text;
}

/* A base session opened and connected: its in written a first byte, its
 * `hello`/`opened`/`repaint` read off. Returns its in fd; `*out` its out. */
static int base_open(Wall *w, int *out)
{
    int in, o;
    knock(w, "open base layer z=0 type=cells take=1");
    o = sess_out(w, "base");
    in = sess_in(w, "base");
    if (out)
        *out = o;
    else
        close(o);
    return in;
}

static void a_base_frame_reaches_the_glass(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    (void)w;
    if (write(in, "\x1b[1;1HA\x1b[0m", 11) < 0)
        CHECK(0, "write failed");
    pump(0.2);
    CHECK(shown(s, 0, 0) == 'A', "the base's frame did not reach the glass");
    said = out_text(out);
    CHECK(strncmp(said, "hello v1 line\nopened base z=0\n", 30) == 0,
          "the base was not greeted: %.60s", said);
    CHECK(strstr(said, "\nrepaint\n"), "the connected base was not told repaint: %.120s", said);
    close(in);
    close(out);
}

static void a_base_font_change_resizes_and_announces(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    if (write(in, "\x1b[1;1HA\x1b[0m", 11) < 0)
        CHECK(0, "write failed");
    pump(0.15);
    out_text(out);
    if (write(in, "\x1b_bitwall font 20\x1b\\", 19) < 0)
        CHECK(0, "write failed");
    pump(0.15);
    said = out_text(out);
    CHECK(s->size == 20, "the base's font was not taken: %d", s->size);
    CHECK(strstr(said, "area ") && strstr(said, " 20\n"),
          "the new area was not announced: %.80s", said);
    close(in);
    close(out);
}

static void a_font_that_keeps_the_grid_keeps_the_picture(void)
{
    Screen *s;
    Wall *w;
    int out, in;
    char *said;
    setenv("BITWALL_EVEN_CELLS", "1", 1);
    s = gpu_screen(12);
    w = wall_at(s, "0");
    in = base_open(w, &out);
    if (write(in, "\x1b[1;1HA\x1b[0m", 11) < 0)
        CHECK(0, "write failed");
    pump(0.15);
    out_text(out);
    if (write(in, "\x1b_bitwall font 13\x1b\\", 19) < 0)
        CHECK(0, "write failed");
    pump(0.15);
    said = out_text(out);
    CHECK(s->size == 13 && strstr(said, " 13\n"), "the new size was not announced: %.80s", said);
    CHECK(!strstr(said, "repaint\n"), "a size with the same grid asked for a repaint: %.80s", said);
    CHECK((s->gl.shows[0] & ~TAIL) == 'A', "a size with the same grid lost the picture");
    unsetenv("BITWALL_EVEN_CELLS");
    close(in);
    close(out);
}

/* With nothing to read the loop sleeps for its wait, rather than coming
 * straight back and spinning a core until a session opens. */
static void an_idle_loop_sleeps_for_its_wait(void)
{
    double t0 = now_ms(), took;
    inbox_wait(NULL, 0, 50);
    took = now_ms() - t0;
    CHECK(took >= 40, "a wait of 50 ms with nothing to wait on took %.1f ms", took);
}

/* A base opened and not yet written to hears nothing until its first
 * byte, `repaint` first: the wall's status, which goes up while no sender
 * is connected, is not put up again under it. */
static void a_base_held_but_silent_is_not_told_the_status(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in;
    char *said;
    w->status_font = 20;
    w->last_status = 0;             /* the status's turn is now */
    knock(w, "open base layer z=0 type=cells take=1");
    out = sess_out(w, "base");
    pump(0.2);
    said = out_text(out);
    CHECK(!strcmp(said, "hello v1 line\nopened base z=0\n"),
          "a base not yet written to heard more than its greeting: %.120s", said);
    in = sess_in(w, "base");
    if (write(in, "\x1b[1;1HA\x1b[0m", 11) < 0)
        CHECK(0, "write failed");
    pump(0.2);
    said = out_text(out);
    CHECK(!strncmp(said, "repaint\n", 8), "the first byte was not answered repaint first: %.80s",
          said);
    close(in);
    close(out);
}

/* Whether a `glyphs=` value, CSS unicode-range spans, covers `cp`. */
static int claims(const char *glyphs, uint32_t cp)
{
    const char *p = glyphs;
    while ((p = strstr(p, "U+"))) {
        char *end;
        unsigned long lo = strtoul(p + 2, &end, 16), hi = lo;
        if (*end == '-')
            hi = strtoul(end + 1, &end, 16);
        if (cp >= lo && cp <= hi)
            return 1;
        p = end;
    }
    return 0;
}

static void the_screen_claims_the_icons_the_font_has_and_what_is_drawn_whatever(void)
{
    static const uint32_t icons[] = {0xE0B0, 0xEA60, 0xF05A, 0xF075, 0xF129, 0xF400, 0xF0369};
    static const uint32_t drawn[] = {0x2500, 0x253C, 0x256D, 0x2580, 0x2591, 0x259F,
                                     0x25AA, 0x25E2, 0x2801, 0x28FF, 0x1CD00, 0x1FB00};
    static const uint32_t fonts[] = {0x41, 0x254C, 0x2571, 0x25CF, 0xFF66};
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    char line[320];
    size_t i;
    for (i = 0; i < sizeof icons / sizeof icons[0]; i++)
        CHECK(claims(w->glyphs, icons[i]), "U+%04X is in the font and not claimed: %s",
              icons[i], w->glyphs);
    screen_line(w, line, sizeof line);
    CHECK(claims(line, 0xF1AF0) && strstr(line, "U+1FBE4-1FBE7"),
          "the screen line lost the end of its glyphs: %s", line);
    CHECK(strlen(w->glyphs) <= 256, "glyphs is %zu bytes, past a value's 256: %s",
          strlen(w->glyphs), w->glyphs);
    CHECK(strstr(w->glyphs, "U+20-7E,") == w->glyphs && strstr(w->glyphs, "U+2500-25FF,"),
          "the spans were not sorted and joined: %s", w->glyphs);
    fake.fontless = 1;
    wall_glyphs(w);
    for (i = 0; i < sizeof drawn / sizeof drawn[0]; i++)
        CHECK(claims(w->glyphs, drawn[i]), "U+%04X is drawn and not claimed: %s",
              drawn[i], w->glyphs);
    for (i = 0; i < sizeof fonts / sizeof fonts[0]; i++)
        CHECK(!claims(w->glyphs, fonts[i]), "U+%04X is claimed with no font: %s",
              fonts[i], w->glyphs);
    for (i = 0; i < sizeof icons / sizeof icons[0]; i++)
        CHECK(!claims(w->glyphs, icons[i]), "U+%04X is claimed with no font: %s",
              icons[i], w->glyphs);
}

static void a_sender_gone_clears_the_screen_and_resets(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    if (write(in, "\x1b[1;1HA\x1b[7;9H\x1b[1;31mB\x1b[0m", 25) < 0)
        CHECK(0, "write failed");
    pump(0.15);
    CHECK(shown(s, 0, 0) == 'A', "the base never painted");
    /* The status goes up once a second while nobody holds the base; put
     * its next turn past the look, which is at the clearing. */
    w->last_status = now_ms();
    close(in);                  /* the sender goes: end of file on its `in` */
    pump(0.3);
    said = out_text(out);
    CHECK(strstr(said, "closed base eof"), "the base was not ended on eof: %.80s", said);
    CHECK(shown(s, 0, 0) == ' ' && s->x == 0 && s->y == 0,
          "the screen was not cleared and the cursor put back");
    CHECK(!s->pen.bold && !s->pen.fg_set, "the pen was left as the sender had it");
    CHECK(!w->base && !w->connected, "the wall still thinks it has a sender");
    close(out);
}

static void take_displaces_the_base_and_busy_refuses_without_it(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int aout, ain = base_open(w, &aout);
    char *said;
    int bout;
    if (write(ain, "\x1b[1;1HA\x1b[0m", 11) < 0)
        CHECK(0, "write failed");
    pump(0.1);
    out_text(aout);
    /* A second base without take=1 is busy. */
    knock(w, "open b layer z=0 type=cells");
    bout = sess_out(w, "b");
    pump(0.1);
    said = out_text(bout);
    CHECK(strstr(said, "error open b busy"), "a second base was not busy: %.60s", said);
    close(bout);
    /* With take=1 the holder is displaced. */
    knock(w, "open c layer z=0 type=cells take=1");
    bout = sess_out(w, "c");
    pump(0.2);
    said = out_text(aout);
    CHECK(strstr(said, "closed base displaced"), "the holder was not displaced: %.60s", said);
    close(ain);
    close(aout);
    close(bout);
}

/* ----------------------------------------------------------------- dpms */

static void output(int i, uint32_t id, uint32_t encoder, uint32_t crtc)
{
    fake.conn[i].id = id;
    fake.conn[i].encoder = encoder;
    fake.conn[i].crtc = crtc;
    fake.conn[i].connected = 1;
    fake.conn[i].dpms = 1;
    if (fake.nconnectors <= i)
        fake.nconnectors = i + 1;
}

/* What the painter says on stderr while `fn` runs. */
static char *stderr_of(void (*fn)(Screen *), Screen *s)
{
    static char text[4096];
    FILE *f = tmpfile();
    int saved = dup(2);
    size_t n;
    CHECK(f && saved >= 0, "cannot capture stderr");
    fflush(stderr);
    dup2(fileno(f), 2);
    fn(s);
    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    rewind(f);
    n = fread(text, 1, sizeof text - 1, f);
    text[n] = 0;
    fclose(f);
    return text;
}

static void sleep_output(Screen *s) { power(s, 0); }

static void dpms_is_sought_again_until_an_output_is_found(void)
{
    Screen *s = gpu_screen(14);
    power(s, 0);            /* the card has no outputs yet */
    power(s, 1);
    CHECK(fake.set_calls == 0, "set a property on no output");
    output(0, 31, 1, 5);    /* and now it has one */
    power(s, 0);
    CHECK(fake.set_calls == 1 && fake.set_connector == 31 && fake.set_value == DRM_MODE_DPMS_OFF,
          "an output that appeared later was never put to sleep");
}

static void dpms_goes_to_the_output_being_driven(void)
{
    Screen *s = gpu_screen(14);
    output(0, 31, 1, 0);    /* connected, but nothing drives it */
    output(1, 32, 2, 9);
    power(s, 0);
    CHECK(fake.set_calls == 1 && fake.set_connector == 32,
          "slept connector %u rather than the one being driven", fake.set_connector);
}

static void dpms_falls_back_to_a_connected_output(void)
{
    Screen *s = gpu_screen(14);
    output(0, 31, 0, 0);
    power(s, 0);
    CHECK(fake.set_calls == 1 && fake.set_connector == 31, "no output was put to sleep");
}

static void a_refused_dpms_is_said_so(void)
{
    Screen *s = gpu_screen(14);
    char *said;
    output(0, 31, 1, 5);
    fake.set_result = -EACCES;
    said = stderr_of(sleep_output, s);
    CHECK(strstr(said, "DPMS"), "a refusal went unreported: %s", said);
}

/* --------------------------------------------------------------- cursor */

static void text_of(char *out, int n, const char *each)
{
    int i;
    out[0] = 0;
    for (i = 0; i < n; i++)
        strcat(out, each);
}

static void a_cursor_run_off_the_grid_stops_at_the_edge(void)
{
    static char line[4 * 3000 + 1];
    Screen *both[2];
    int i;
    both[0] = gpu_screen(14);
    both[1] = soft_screen(14);
    for (i = 0; i < 2; i++) {
        Screen *s = both[i];
        char seq[32];
        snprintf(seq, sizeof seq, "\x1b[1;%dH", s->cols - 2);
        write_text(s, seq);
        text_of(line, 3000, "x");
        write_text(s, line);
        CHECK(s->x == s->cols, "%s: ASCII left the cursor at %d of %d", i ? "software" : "GPU",
              s->x, s->cols);
        text_of(line, 3000, "\xe2\x96\x88");
        write_text(s, line);
        text_of(line, 700, "\xf0\x9f\x98\x80");
        write_text(s, line);
        CHECK(s->x == s->cols, "%s: blocks and wide glyphs left the cursor at %d of %d",
              i ? "software" : "GPU", s->x, s->cols);
        write_text(s, "\x1b[2;9999Hzz");
        CHECK(s->x == s->cols, "%s: text beyond the grid left the cursor at %d", i ? "software" : "GPU", s->x);
    }
}

static void a_cursor_at_the_end_of_int_does_not_overflow(void)
{
    Screen *g = gpu_screen(14), *w = soft_screen(14);
    g->x = w->x = INT_MAX - 3;
    write_text(g, "0123456789");
    write_text(w, "0123456789");
    CHECK(g->x == g->cols && w->x == w->cols, "the cursor ended at %d and %d", g->x, w->x);
}

static void a_cursor_left_of_the_grid_draws_nothing(void)
{
    Screen *g = gpu_screen(14), *w = soft_screen(14);
    size_t n = verts_bytes(g);
    uint8_t *before = malloc(n);
    CHECK(before, "out of memory");
    memcpy(before, g->gl.verts, n);
    g->x = w->x = -1;
    g->y = w->y = 1;
    write_text(g, "AB");
    write_text(w, "AB");
    CHECK(memcmp(before, g->gl.verts, n) == 0, "a cursor at -1 drew on the grid");
}

/* ---------------------------------------------------------------- inbox */

/* How much the allocator gave a pointer. Declared here rather than
 * included, because the headers they live in want more of the platform
 * than the painter's feature macros let through. */
#if defined(__APPLE__)
size_t malloc_size(const void *p);
#define allocated(p) malloc_size(p)
#elif defined(__GLIBC__)
size_t malloc_usable_size(void *p);
#define allocated(p) malloc_usable_size(p)
#endif

static void a_small_read_holds_a_small_chunk(void)
{
    int fds[2];
    Chunk *c = NULL;
    ssize_t n;
    CHECK(pipe(fds) == 0, "pipe: %s", strerror(errno));
    CHECK(write(fds[1], "0123456789", 10) == 10, "write failed");
    n = chunk_read(fds[0], &c);
    CHECK(n == 10 && c && c->len == 10 && memcmp(c->data, "0123456789", 10) == 0,
          "the read came back as %zd bytes", n);
#ifdef allocated
    CHECK(allocated(c) < 1024, "ten bytes are held in %zu", (size_t)allocated(c));
#endif
    free(c);
    close(fds[1]);
    CHECK(chunk_read(fds[0], &c) == 0 && !c, "end of file was not zero");
}

static void an_inbox_hands_over_what_arrived_in_order(void)
{
    int fds[2], i;
    Inbox in;
    uint8_t got[64 * 1024];
    size_t have = 0;
    memset(&in, 0, sizeof in);
    CHECK(pipe(fds) == 0, "pipe: %s", strerror(errno));
    CHECK(inbox_start(&in, fds[0]) == 0, "inbox_start failed");
    for (i = 0; i < 1000; i++) {
        char line[16];
        snprintf(line, sizeof line, "%05d,", i);
        CHECK(write(fds[1], line, 6) == 6, "write failed");
    }
    close(fds[1]);
    for (;;) {
        ssize_t n = inbox_take(&in, got + have, sizeof got - have, 200);
        if (n < 0)
            break;
        have += (size_t)n;
    }
    CHECK(have == 6000, "took %zu of 6000 bytes", have);
    for (i = 0; i < 1000; i++) {
        char line[16];
        snprintf(line, sizeof line, "%05d,", i);
        CHECK(memcmp(got + i * 6, line, 6) == 0, "line %d is out of order", i);
    }
}

/* A board with `n` outputs, numbered and driven as a Pi 5's are: HDMI-A-1
 * is connector 33, HDMI-A-2 is 42, each on a CRTC of its own, and SDL's
 * display i is on output i. */
static void board(int n)
{
    int i;
    fake.ndisplays = n;
    for (i = 0; i < n; i++) {
        output(i, 33 + 9 * (uint32_t)i, 1 + (uint32_t)i, 5 + (uint32_t)i);
        fake.disp[i].on = i + 1;
    }
}

/* Every display's screen, opened, placed and given a font as main does
 * it. `s` comes back in wall order. */
static void screens_up(Screen **s, int n)
{
    int i;
    CHECK(video_open("kmsdrm") == 0, "video_open failed");
    for (i = 0; i < n; i++) {
        s[i] = calloc(1, sizeof *s[i]);
        CHECK(s[i], "out of memory");
        s[i]->font_path = "fake";
        CHECK(screen_open(s[i], i, 0, 0) == 0, "screen_open %d failed", i);
    }
    walls_up(s, n);
    for (i = 0; i < n; i++) {
        pen_reset(&s[i]->pen);
        CHECK(set_font(s[i], 14) == 0, "set_font failed");
    }
}

/* What the painter says on stderr while `fn` runs over some screens. */
static char *stderr_of_walls(void (*fn)(Screen **, int), Screen **s, int n)
{
    static char text[8192];
    FILE *f = tmpfile();
    int saved = dup(2);
    size_t got;
    CHECK(f && saved >= 0, "cannot capture stderr");
    fflush(stderr);
    dup2(fileno(f), 2);
    fn(s, n);
    fflush(stderr);
    dup2(saved, 2);
    close(saved);
    rewind(f);
    got = fread(text, 1, sizeof text - 1, f);
    text[got] = 0;
    fclose(f);
    return text;
}

/* Row `y` of what a GPU screen shows, as text: its first `n` cells. */
static char *row_text(Screen *s, int y, int n)
{
    static char text[512];
    int x;
    for (x = 0; x < n && x < s->cols && x < (int)sizeof text - 1; x++) {
        uint32_t cp = shown(s, x, y);
        text[x] = cp < 0x80 ? (char)cp : '?';
    }
    text[x] = 0;
    return text;
}

static int is_fifo(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0 && S_ISFIFO(st.st_mode);
}

static void place_two(Screen **s, int n)
{
    int i;
    CHECK(video_open("kmsdrm") == 0, "video_open failed");
    for (i = 0; i < n; i++) {
        s[i] = calloc(1, sizeof *s[i]);
        CHECK(s[i] && screen_open(s[i], i, 0, 0) == 0, "screen_open %d failed", i);
    }
    walls_up(s, n);
}

static void place_soft(Screen **s, int n)
{
    int i;
    setenv("BITWALL_PAINT_SOFTWARE", "1", 1);
    screens_up(s, n);
    for (i = 0; i < n; i++)
        CHECK(!s[i]->gpu && s[i]->glass, "screen %d is not on the software path", i);
}

/* Whether any pixel of cell x,y on the glass is not ground. */
static int inked(Screen *s, int x, int y)
{
    int px, py;
    for (py = s->oy + y * s->ch; py < s->oy + (y + 1) * s->ch; py++)
        for (px = s->ox + x * s->cw; px < s->ox + (x + 1) * s->cw; px++)
            if (((Uint32 *)((uint8_t *)s->glass->pixels + (size_t)py * s->glass->pitch))[px]
                != 0xff000000u)
                return 1;
    return 0;
}

static void walls_follow_the_card_when_sdl_numbers_displays_otherwise(void)
{
    Screen *s[2];
    char *said;
    board(2);
    fake.disp[0].on = 2;    /* SDL's first display is the card's second output */
    fake.disp[1].on = 1;
    said = stderr_of_walls(place_two, s, 2);
    CHECK(s[0]->display == 1 && s[1]->display == 0, "wall 0 is SDL display %d", s[0]->display);
    CHECK(strcmp(s[0]->output, "HDMI-A-1") == 0 && s[0]->pin == 33,
          "wall 0 is %s, pinned to %u", s[0]->output, s[0]->pin);
    CHECK(strstr(said, "warning") && strstr(said, "wall 0: HDMI-A-1")
          && strstr(said, "wall 1: HDMI-A-2"), "said: %s", said);
}

static void a_window_that_cannot_be_told_apart_takes_its_display_number(void)
{
    Screen *s[2];
    char *said;
    board(2);
    fake.disp[0].on = fake.disp[1].on = 0;     /* frames that reach no CRTC */
    said = stderr_of_walls(place_two, s, 2);
    CHECK(s[0]->display == 0 && strcmp(s[0]->output, "HDMI-A-1") == 0 && s[0]->pin == 33,
          "wall 0 is display %d on %s", s[0]->display, s[0]->output);
    CHECK(s[1]->display == 1 && strcmp(s[1]->output, "HDMI-A-2") == 0 && s[1]->pin == 42,
          "wall 1 is display %d on %s", s[1]->display, s[1]->output);
    CHECK(strstr(said, "cannot tell"), "the guess went unsaid: %s", said);
}

static void text_sent_to_one_wall_never_reaches_the_other_in_software(void)
{
    Screen *s[2];
    board(2);
    stderr_of_walls(place_soft, s, 2);
    CHECK(s[0]->glass != s[1]->glass, "the walls share a window surface");
    write_text(s[1], "\x1b[1;1HB\x1b[0m");
    CHECK(inked(s[1], 0, 0), "wall 1 did not draw its B");
    CHECK(!inked(s[0], 0, 0), "wall 1's B reached wall 0");
    write_text(s[0], "\x1b[2;2HA\x1b[0m");
    CHECK(!inked(s[1], 1, 1), "wall 0's A reached wall 1");
}

/* ----------------------------------------------------------------- exit */

static void a_reader_that_cannot_be_woken_is_left_rather_than_waited_for(void)
{
    int fds[2];
    static Inbox in;
    CHECK(pipe(fds) == 0, "pipe: %s", strerror(errno));
    CHECK(inbox_start(&in, fds[0]) == 0, "inbox_start failed");
    alarm(5);
    inbox_stop(&in);
    CHECK(!in.reader, "the reader is still thought to be ours");
}

static void the_monitor_is_woken_when_the_wall_comes_up(void)
{
    Screen *s = calloc(1, sizeof *s);
    CHECK(s, "out of memory");
    s->font_path = "fake";
    output(0, 31, 1, 5);
    CHECK(video_open("kmsdrm") == 0 && screen_open(s, 0, 0, 0) == 0, "screen_open failed");
    walls_up(&s, 1);
    CHECK(fake.set_calls == 1 && fake.set_value == DRM_MODE_DPMS_ON,
          "a monitor left asleep stays asleep (%d calls)", fake.set_calls);
    screens_free(&s, 1);
}

static void the_monitor_is_woken_when_the_wall_goes(void)
{
    Screen *s = gpu_screen(14);
    output(0, 31, 1, 5);
    power(s, 0);
    screens_free(&s, 1);
    CHECK(fake.set_calls == 2 && fake.set_value == DRM_MODE_DPMS_ON,
          "the wall left its monitor asleep");
}

/* ---------------------------------------------------------------- walls */

/* Two walls, as a Pi with two screens has them, each claimed under a fresh run directory. */
static void claim2(Screen **s, Wall **w)
{
    int i;
    board(2);
    CHECK(video_open("kmsdrm") == 0, "video_open failed");
    for (i = 0; i < 2; i++) {
        s[i] = calloc(1, sizeof *s[i]);
        CHECK(s[i], "out of memory");
        s[i]->font_path = "fake";
        CHECK(screen_open(s[i], i, 0, 0) == 0, "screen_open %d failed", i);
    }
    walls_up(s, 2);
    for (i = 0; i < 2; i++) {
        char nm[8];
        pen_reset(&s[i]->pen);
        CHECK(set_font(s[i], 14) == 0, "set_font failed");
        snprintf(nm, sizeof nm, "%d", i);
        w[i] = wall_at(s[i], nm);
    }
}

static void two_outputs_are_two_walls_in_connector_order(void)
{
    Screen *s[2];
    Wall *w[2];
    char want[400];
    claim2(s, w);
    CHECK(s[0]->index == 0 && s[1]->index == 1 && s[0]->count == 2, "the walls are not numbered");
    CHECK(strcmp(s[0]->output, "HDMI-A-1") == 0 && strcmp(s[1]->output, "HDMI-A-2") == 0,
          "wall 0 is %s and wall 1 is %s", s[0]->output, s[1]->output);
    CHECK(s[0]->pin == 33 && s[1]->pin == 42, "pinned to %u and %u", s[0]->pin, s[1]->pin);
    snprintf(want, sizeof want, "%s/bitwall/0/door.in", run_dir);
    CHECK(is_fifo(want), "wall 0 has no door at %s", want);
    snprintf(want, sizeof want, "%s/bitwall/1/door.in", run_dir);
    CHECK(is_fifo(want), "wall 1 has no door at %s", want);
    CHECK(strcmp(w[0]->name, "0") == 0 && strcmp(w[1]->name, "1") == 0, "the walls are misnamed");
    CHECK(walls_wanted(-1, 0, 0, "kmsdrm") == 2, "--walls auto is not a wall a display");
    CHECK(walls_wanted(0, 0, 0, "kmsdrm") == 2, "KMS does not default to a wall a display");
    CHECK(walls_wanted(0, 0, 0, "offscreen") == 1, "off KMS the default is not one wall");
    CHECK(walls_wanted(0, 1, 0, "kmsdrm") == 1, "--display did not mean one wall");
    CHECK(walls_wanted(-1, 0, 1, "kmsdrm") == 1, "stdin did not mean one wall");
}

static void one_output_is_todays_single_wall(void)
{
    Screen *s[1];
    Wall *w[1];
    char path[400];
    int i;
    board(1);
    CHECK(video_open("kmsdrm") == 0, "video_open failed");
    s[0] = calloc(1, sizeof *s[0]);
    s[0]->font_path = "fake";
    CHECK(screen_open(s[0], 0, 0, 0) == 0, "screen_open failed");
    walls_up(s, 1);
    pen_reset(&s[0]->pen);
    CHECK(set_font(s[0], 14) == 0, "set_font failed");
    w[0] = wall_at(s[0], "0");
    CHECK(s[0]->index == 0 && s[0]->count == 1, "one screen is wall %d of %d",
          s[0]->index, s[0]->count);
    CHECK(strcmp(s[0]->output, "HDMI-A-1") == 0, "the output is %s", s[0]->output);
    CHECK(s[0]->pin == 0, "a single wall was pinned to connector %u", s[0]->pin);
    snprintf(path, sizeof path, "%s/bitwall/0/door.in", run_dir);
    CHECK(is_fifo(path), "no door at %s", path);
    snprintf(path, sizeof path, "%s/bitwall/1", run_dir);
    CHECK(access(path, F_OK) != 0, "a single wall made %s", path);
    wall_status(w[0]);
    CHECK(!strstr(row_text(s[0], 4, 40), "wall"), "a single wall's status page names a wall: %s",
          row_text(s[0], 4, 40));
    CHECK(walls_wanted(-1, 0, 0, "kmsdrm") == 1, "one display made more than one wall");
    (void)i;
}

static void text_sent_to_one_wall_never_reaches_the_other(void)
{
    Screen *s[2];
    Wall *w[2];
    claim2(s, w);
    write_text(s[1], "\x1b[1;1HB\x1b[3;1HC\x1b[0m");
    write_text(s[0], "\x1b[1;1HA\x1b[0m");
    write_text(s[0], "\x1b[2;2HD\x1b[0m");
    CHECK(shown(s[0], 0, 0) == 'A' && shown(s[1], 0, 0) == 'B', "wall 0 shows U+%04X, wall 1 "
          "U+%04X", shown(s[0], 0, 0), shown(s[1], 0, 0));
    CHECK(shown(s[0], 0, 2) == ' ', "wall 1's C reached wall 0");
    CHECK(shown(s[1], 1, 1) == ' ', "wall 0's D reached wall 1");
    CHECK(s[0]->gl.atlas != s[1]->gl.atlas, "the walls share an atlas");
}

static void every_gl_call_is_made_with_its_own_walls_context(void)
{
    Screen *s[2];
    Wall *w[2];
    int i;
    claim2(s, w);
    for (i = 0; i < 3; i++) {
        write_text(s[1], "\x1b[1;1H\xe4\xb8\x80\x1b[0m");
        write_text(s[0], "\x1b[1;1H\xe4\xba\x8c\x1b[0m");
    }
    churn(s[1], 3, 3, 0x4E00, 50);
    churn(s[0], 3, 3, 0x5E00, 50);
    set_font(s[0], 20);
    blank(s[1], 1);
    write_text(s[0], "\x1b[2;2HQ\x1b[0m");
    blank(s[1], 0);
    wall_status(w[1]);
    wall_status(w[0]);
    present(s[1]);
    present(s[0]);
    CHECK(fake.wrong_context == 0, "%d GL calls went to another wall's context",
          fake.wrong_context);
    for (i = 0; i < 2; i++) {
        FakeContext *c = s[i]->gl.ctx;
        CHECK(c->draws && c->swaps && c->uploads, "wall %d: %d draws, %d swaps, %d uploads",
              i, c->draws, c->swaps, c->uploads);
    }
}

static void a_font_change_on_one_wall_leaves_the_others_area_and_atlas(void)
{
    Screen *s[2];
    Wall *w[2];
    const FakeTexture *t;
    uint8_t *before;
    size_t bytes;
    int cols, rows, size, aw, ah;
    claim2(s, w);
    write_text(s[1], "\x1b[1;1HZ\x1b[0m");
    t = fake_texture(s[1]->gl.atlas);
    bytes = (size_t)t->w * t->h;
    before = malloc(bytes);
    CHECK(before, "out of memory");
    memcpy(before, t->pixels, bytes);
    cols = s[1]->cols, rows = s[1]->rows, size = s[1]->size;
    aw = s[1]->gl.atlas_w, ah = s[1]->gl.atlas_h;
    set_font(s[0], 40);
    CHECK(s[0]->size == 40, "wall 0 did not take the font");
    CHECK(s[1]->cols == cols && s[1]->rows == rows && s[1]->size == size,
          "wall 1's area moved to %dx%d %d", s[1]->cols, s[1]->rows, s[1]->size);
    CHECK(s[1]->gl.atlas_w == aw && s[1]->gl.atlas_h == ah && t->w == aw && t->h == ah,
          "wall 1's atlas was resized");
    CHECK(memcmp(before, t->pixels, bytes) == 0, "wall 1's atlas was written");
    CHECK(shown(s[1], 0, 0) == 'Z', "wall 1 lost its Z");
    free(before);
}

static void each_wall_says_which_it_is_on_its_status_page(void)
{
    Screen *s[2];
    Wall *w[2];
    char host[128], want[160];
    int y, found[2] = {0, 0}, i;
    claim2(s, w);
    CHECK(gethostname(host, sizeof host) == 0, "gethostname failed");
    host[sizeof host - 1] = 0;
    for (i = 0; i < 2; i++) {
        wall_status(w[i]);
        for (y = 0; y < 8 && y < s[i]->rows; y++) {
            char *row = row_text(s[i], y, s[i]->cols < 200 ? s[i]->cols : 200);
            if (i)
                snprintf(want, sizeof want, "%s:1", host);
            else
                snprintf(want, sizeof want, "send to        %s ", host);
            if (strstr(row, i ? "HDMI-A-2" : "HDMI-A-1"))
                found[i] |= 1;
            if (strstr(row, want))
                found[i] |= 2;
        }
        CHECK(found[i] == 3, "wall %d's page does not say which it is (%d)", i, found[i]);
    }
}

static void measures_read_the_counters(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    Sample old = {0}, now = {0};
    char v[80], want[64];
    old.cpu = now.cpu = -1;
    now.t = 1000;
    now.frames = 22;
    now.refreshes = 2;
    now.bytes = 6.9e6;
    now.out = 2.1e3;
    now.parse = 40;
    now.flush = 22;
    now.swap = 44;
    measure(w, 2, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "20.0") == 0, "fps %s", v);
    measure(w, 3, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "6.9MB/s") == 0, "in %s", v);
    old.gpu = now.gpu = -1;
    measure(w, 15, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "gpu") == 0, "render with no count %s", v);
    old.gpu = 1.0;
    now.gpu = 1.19;
    measure(w, 15, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "gpu 19%") == 0, "render %s", v);
    measure(w, 1, &now, &old, v, sizeof v);
    snprintf(want, sizeof want, "%dx%d @14", s->cols, s->rows);
    CHECK(strcmp(v, want) == 0, "grid %s", v);
    measure(w, 12, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "lit") == 0, "display %s", v);
    blank(s, 1);
    measure(w, 12, &now, &old, v, sizeof v);
    CHECK(strcmp(v, "blank") == 0, "display %s", v);
}

static void the_legacy_blocks_are_said_whatever_the_font(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    if (write(in, "\x1b_bitwall report\x1b\\", 18) < 0)
        CHECK(0, "write failed");
    pump(0.2);
    said = out_text(out);
    CHECK(strstr(said, "U+1CD00-1CDE5") && strstr(said, "U+1FB00-1FB3B"),
          "the screen line does not offer the octants and sextants: %.200s",
          strstr(said, "screen") ? strstr(said, "screen") : said);
    close(in);
    close(out);
}

/* ---------------------------------------------------------------- shapes */

/* The sizes the shape tests are run at: odd and even cells both ways. */
static const int SHAPE_SIZES[] = {8, 9, 10, 13, 14, 16, 17, 20, 26, 28, 40};
#define NSIZES (sizeof SHAPE_SIZES / sizeof SHAPE_SIZES[0])

/* A shape as this screen will draw it: how many times each pixel of the
 * cell is covered. */
static void shape_mask(Screen *s, uint32_t cp, uint8_t *m)
{
    int idx = shape_index(cp), j, x, y;
    CHECK(idx >= 0, "U+%04X has no shape", cp);
    memset(m, 0, (size_t)s->cw * s->ch);
    for (j = 0; j < s->pn[idx]; j++) {
        const SDL_Rect *r = &s->px[idx][j];
        CHECK(r->x >= 0 && r->y >= 0 && r->x + r->w <= s->cw && r->y + r->h <= s->ch,
              "U+%04X at size %d leaves its cell", cp, s->size);
        for (y = r->y; y < r->y + r->h; y++)
            for (x = r->x; x < r->x + r->w; x++)
                m[y * s->cw + x]++;
    }
}

static void block_halves_meet_with_nothing_between_them(void)
{
    /* Each pair between them is the whole cell. */
    static const uint32_t pairs[][2] = {{0x2580, 0x2584}, {0x258c, 0x2590},
                                        {0x2598, 0x259f}, {0x259a, 0x259e},
                                        {0x2596, 0x259c}, {0x2597, 0x259b}};
    Screen *s = gpu_screen(SHAPE_SIZES[0]);
    size_t k, p;
    for (k = 0; k < NSIZES; k++) {
        uint8_t *a, *b;
        CHECK(set_font(s, SHAPE_SIZES[k]) == 0, "set_font %d failed", SHAPE_SIZES[k]);
        a = malloc((size_t)s->cw * s->ch);
        b = malloc((size_t)s->cw * s->ch);
        for (p = 0; p < sizeof pairs / sizeof pairs[0]; p++) {
            int i;
            shape_mask(s, pairs[p][0], a);
            shape_mask(s, pairs[p][1], b);
            for (i = 0; i < s->cw * s->ch; i++)
                CHECK(a[i] + b[i] == 1, "U+%04X and U+%04X in a %dx%d cell cover "
                      "pixel %d,%d %d times", pairs[p][0], pairs[p][1], s->cw, s->ch,
                      i % s->cw, i / s->cw, a[i] + b[i]);
        }
        free(a);
        free(b);
    }
}

/* Where the parts of a cell cut into `n` begin and end along a side
 * `size` pixels long, each edge rounded as a shape's edges are. */
static void parts_of(int n, int size, int *edge)
{
    int k;
    for (k = 0; k <= n; k++)
        edge[k] = (int)((double)k / n * size + 0.5);
}

static void sextants_and_octants_are_whole_parts_of_their_cell(void)
{
    /* Each covers whole parts of a cell cut two across and three or four
     * down, every pixel of them once and no other, so two side by side or
     * one above another meet with nothing between; and no two draw the
     * same, nor nothing, nor everything. */
    static const struct { uint32_t first; int n, down; } sets[] = {
        {0x1FB00, 60, 3}, {0x1CD00, 230, 4}};
    Screen *s = gpu_screen(SHAPE_SIZES[0]);
    size_t k, t;
    for (k = 0; k < NSIZES; k++) {
        uint8_t *m;
        CHECK(set_font(s, SHAPE_SIZES[k]) == 0, "set_font %d failed", SHAPE_SIZES[k]);
        m = malloc((size_t)s->cw * s->ch);
        for (t = 0; t < sizeof sets / sizeof sets[0]; t++) {
            int xs[3], ys[5], seen[256], down = sets[t].down, i;
            memset(seen, 0, sizeof seen);
            parts_of(2, s->cw, xs);
            parts_of(down, s->ch, ys);
            for (i = 0; i < sets[t].n; i++) {
                uint32_t cp = sets[t].first + (uint32_t)i;
                int bits = 0, part, x, y;
                shape_mask(s, cp, m);
                for (part = 0; part < 2 * down; part++) {
                    int c = part % 2, r = part / 2;
                    if (m[(ys[r] + ys[r + 1]) / 2 * s->cw + (xs[c] + xs[c + 1]) / 2])
                        bits |= 1 << part;
                }
                for (y = 0; y < s->ch; y++)
                    for (x = 0; x < s->cw; x++) {
                        int r = 0, c = x >= xs[1], want;
                        while (r < down - 1 && y >= ys[r + 1])
                            r++;
                        want = bits >> (2 * r + c) & 1;
                        CHECK(m[y * s->cw + x] == want, "U+%04X in a %dx%d cell covers "
                              "pixel %d,%d %d times, not %d", cp, s->cw, s->ch, x, y,
                              m[y * s->cw + x], want);
                    }
                CHECK(bits && bits != (1 << 2 * down) - 1, "U+%04X is empty or full", cp);
                CHECK(!seen[bits], "U+%04X draws what another does", cp);
                seen[bits] = 1;
            }
        }
        free(m);
    }
}

static void legacy_blocks_are_what_their_names_say(void)
{
    /* Held to a table tests/bitwall-tests writes from Unicode's own
     * names: a line a code point, its hex and then which of an 8 x 24 grid
     * of samples its name says it covers, row by row. The painter's
     * tables are its own reading of the same names; this is the second.
     * Run without the table (tests/paint/run on its own) it holds them to
     * nothing. At a cell of whole eighths across and 24ths down every
     * pixel lies in one sample's square. */
    const char *path = getenv("BITWALL_SHAPE_NAMES");
    char line[512];
    Screen *s;
    FILE *f;
    uint8_t *m;
    int size, n = 0;
    if (!path)
        return;
    f = fopen(path, "r");
    CHECK(f, "open %s: %s", path, strerror(errno));
    s = gpu_screen(SHAPE_SIZES[0]);
    for (size = 8; size < 200; size++) {
        CHECK(set_font(s, size) == 0, "set_font %d failed", size);
        if (s->cw % 8 == 0 && s->ch % 24 == 0)
            break;
    }
    CHECK(size < 200, "no size makes a cell of whole eighths and 24ths");
    m = malloc((size_t)s->cw * s->ch);
    while (fgets(line, sizeof line, f)) {
        unsigned cp;
        char bits[256];
        int x, y;
        if (sscanf(line, "%x %255s", &cp, bits) != 2)
            continue;
        CHECK(strlen(bits) == 192, "U+%04X: %zu samples, not 192", cp, strlen(bits));
        shape_mask(s, cp, m);
        for (y = 0; y < s->ch; y++)
            for (x = 0; x < s->cw; x++) {
                int want = bits[(y * 24 / s->ch) * 8 + x * 8 / s->cw] == '1';
                CHECK(m[y * s->cw + x] == want, "U+%04X at pixel %d,%d of %dx%d is %d, "
                      "its name says %d", cp, x, y, s->cw, s->ch, m[y * s->cw + x], want);
            }
        n++;
    }
    fclose(f);
    free(m);
    CHECK(n > 300, "only %d blocks in %s", n, path);
}


/* Which pixels along one edge of a shape are inked, as a bit string. */
static void edge_of(Screen *s, const uint8_t *m, int arm, char *out)
{
    int i, n = arm == UP || arm == DOWN ? s->cw : s->ch;
    for (i = 0; i < n; i++) {
        int x = arm == LEFT ? 0 : arm == RIGHT ? s->cw - 1 : i;
        int y = arm == UP ? 0 : arm == DOWN ? s->ch - 1 : i;
        out[i] = m[y * s->cw + x] ? '1' : '0';
    }
    out[n] = 0;
}

static void box_lines_meet_every_neighbour_they_point_at(void)
{
    Screen *s = gpu_screen(SHAPE_SIZES[0]);
    size_t k;
    for (k = 0; k < NSIZES; k++) {
        /* The edge each weight of arm leaves, the same whichever
         * character it belongs to. */
        char seen[4][4][64];
        uint8_t *m;
        int i, arm;
        CHECK(set_font(s, SHAPE_SIZES[k]) == 0, "set_font %d failed", SHAPE_SIZES[k]);
        CHECK(s->cw < 64 && s->ch < 64, "a cell too big for the test");
        memset(seen, 0, sizeof seen);
        m = malloc((size_t)s->cw * s->ch);
        for (i = 0; i < NBOX; i++) {
            uint32_t cp = SHAPE_FIRST + (uint32_t)i;
            if (shape_index(cp) < 0)
                continue;
            shape_mask(s, cp, m);
            for (arm = 0; arm < 4; arm++) {
                int w = BOX[i][arm];
                char edge[64];
                edge_of(s, m, arm, edge);
                if (!w) {
                    CHECK(!strchr(edge, '1'), "U+%04X at size %d inks an edge it has "
                          "no arm to: %s", cp, s->size, edge);
                    continue;
                }
                CHECK(strchr(edge, '1'), "U+%04X at size %d does not reach its edge",
                      cp, s->size);
                if (!seen[arm][w][0])
                    strcpy(seen[arm][w], edge);
                CHECK(!strcmp(seen[arm][w], edge), "U+%04X at size %d ends %s where "
                      "others of its weight end %s", cp, s->size, edge, seen[arm][w]);
            }
        }
        /* An arm up meets the arm down of the cell above it. */
        for (i = 1; i < 4; i++) {
            CHECK(!strcmp(seen[UP][i], seen[DOWN][i]), "weight %d at size %d: up %s, "
                  "down %s", i, s->size, seen[UP][i], seen[DOWN][i]);
            CHECK(!strcmp(seen[LEFT][i], seen[RIGHT][i]), "weight %d at size %d: left "
                  "%s, right %s", i, s->size, seen[LEFT][i], seen[RIGHT][i]);
        }
        free(m);
    }
}

/* A rounded corner leaves its two edges where a light line does, so a
 * rounded box's sides run into its corners with no step, and touches
 * neither of its other two. */
static void arcs_meet_the_light_lines_they_turn(void)
{
    Screen *s = gpu_screen(SHAPE_SIZES[0]);
    static const int arms[4][2] = {{RIGHT, DOWN}, {LEFT, DOWN}, {UP, LEFT}, {UP, RIGHT}};
    size_t k;
    for (k = 0; k < NSIZES; k++) {
        uint8_t *m, *line;
        int which, arm, x, y;
        CHECK(set_font(s, SHAPE_SIZES[k]) == 0, "set_font %d failed", SHAPE_SIZES[k]);
        CHECK(s->cw < 64 && s->ch < 64, "a cell too big for the test");
        m = malloc((size_t)s->cw * s->ch);
        line = malloc((size_t)s->cw * s->ch);
        for (which = 0; which < 4; which++) {
            for (y = 0; y < s->ch; y++)
                for (x = 0; x < s->cw; x++)
                    m[y * s->cw + x] = arc_in(which, x, y, s->cw, s->ch, s->size);
            for (arm = 0; arm < 4; arm++) {
                char got[64], want[64];
                int used = arm == arms[which][0] || arm == arms[which][1];
                edge_of(s, m, arm, got);
                if (!used) {
                    CHECK(!strchr(got, '1'), "U+%04X at size %d inks an edge it does "
                          "not turn to: %s", ARC + which, s->size, got);
                    continue;
                }
                shape_mask(s, arm == UP || arm == DOWN ? 0x2502 : 0x2500, line);
                edge_of(s, line, arm, want);
                CHECK(!strcmp(got, want), "U+%04X at size %d leaves %s where a light "
                      "line leaves %s", ARC + which, s->size, got, want);
            }
        }
        free(m);
        free(line);
    }
}

static void a_double_line_is_two_lines_and_its_cross_is_open(void)
{
    Screen *s = gpu_screen(14);
    uint8_t *m = malloc((size_t)s->cw * s->ch);
    char edge[64];
    Span v[2], h[2];
    int cx, cy, runs = 0, i;
    /* The middle of a double cross is between its lines, both ways. */
    strokes(3, s->cw, 1, 3, v);
    strokes(3, s->ch, 1, 3, h);
    cx = v[0].hi;
    cy = h[0].hi;
    shape_mask(s, 0x2550, m);                          /* ═ */
    edge_of(s, m, LEFT, edge);
    for (i = 0; edge[i]; i++)
        if (edge[i] == '1' && (i == 0 || edge[i - 1] == '0'))
            runs++;
    CHECK(runs == 2, "═ is %d lines at its edge: %s", runs, edge);
    shape_mask(s, 0x256c, m);                          /* ╬ */
    CHECK(!m[cy * s->cw + cx], "╬ is filled in the middle");
    shape_mask(s, 0x253c, m);                          /* ┼ */
    strokes(1, s->cw, 1, 3, v);
    strokes(1, s->ch, 1, 3, h);
    CHECK(m[h[0].lo * s->cw + v[0].lo], "┼ is open in the middle");
    shape_mask(s, 0x2554, m);                          /* ╔ */
    CHECK(!m[0] && !m[cy * s->cw + cx], "╔ has ink outside its corner");
    free(m);
}

/* The box table's lines are the ones drawn as lines; dashes and
 * diagonals are left to the font, and the rounded corners are drawn as
 * arcs of their own rather than from the table. */
static void dashed_and_diagonal_lines_are_left_to_the_font(void)
{
    uint32_t cp;
    for (cp = 0x2500; cp <= 0x257F; cp++) {
        int drawn = (cp <= 0x254B && !(cp >= 0x2504 && cp <= 0x250B))
                    || (cp >= 0x2550 && cp <= 0x256C) || cp >= 0x2574;
        CHECK((shape_index(cp) >= 0) == drawn, "U+%04X is %s", cp,
              drawn ? "left to the font" : "drawn as a shape");
    }
}

static void box_lines_are_drawn_to_the_edge_in_software(void)
{
    Screen *s = soft_screen(14);
    int x, y, idx = shape_index(0x2500);
    const SDL_Rect *r = &s->px[idx][0];
    Uint32 *row;
    write_text(s, "\x1b[1;1H\xe2\x94\x80\xe2\x94\x80\x1b[0m");     /* ── */
    y = s->oy + r->y;
    row = (Uint32 *)((uint8_t *)s->glass->pixels + (size_t)y * s->glass->pitch);
    for (x = s->ox; x < s->ox + 2 * s->cw; x++)
        CHECK(row[x] != 0xff000000u, "── is not drawn at pixel %d of %d", x - s->ox,
              2 * s->cw);
}

/* ---------------------------------------------------- ghosts and halves */

/* How many of cell x,y's mask pixels in the atlas hold each of the three
 * levels a faint braille cell is drawn from: nothing, an unlit dot, a lit
 * one. */
static void mask_levels(Screen *s, int x, int y, int *none, int *dim, int *lit)
{
    const Vert *v = vert_at(s, x, y);
    const FakeTexture *t = fake_texture(s->gl.atlas);
    long px = ((long)v->u * t->w + 32767) / 65535;
    long py = ((long)v->v * t->h + 32767) / 65535;
    int i, j;
    *none = *dim = *lit = 0;
    for (j = 0; j < s->ch; j++)
        for (i = 0; i < s->cw; i++) {
            uint8_t a = t->pixels[(size_t)(py + j) * t->w + px + i];
            if (a == 0) (*none)++;
            else if (a == 128) (*dim)++;
            else if (a == 255) (*lit)++;
        }
}

static void faint_braille_shows_its_unlit_dots_on_the_gpu(void)
{
    Screen *s = gpu_screen(14);
    int none, dim, lit, none1, dim1, lit1;
    /* dot 1 lit, marked faint; then the same pattern without the mark */
    write_text(s, "\x1b[1;1H\x1b[0;2;38;2;255;0;0;48;2;0;0;255m\xe2\xa0\x81"
                  "\x1b[22m\xe2\xa0\x81\x1b[2m\xe2\xa0\x80");
    CHECK(vert_at(s, 0, 0)->ground[3] == 0, "a faint braille cell is not marked for the shader");
    CHECK(vert_at(s, 1, 0)->ground[3] == 255, "SGR 22 did not end faint");
    mask_levels(s, 0, 0, &none, &dim, &lit);
    CHECK(lit > 0 && dim > 0 && none > 0, "levels %d %d %d", none, dim, lit);
    CHECK(dim >= 6 * lit, "seven unlit dots should outweigh one lit one: %d %d", dim, lit);
    mask_levels(s, 2, 0, &none1, &dim1, &lit1);
    CHECK(lit1 == 0 && dim1 == dim + lit, "a blank faint cell is all unlit dots: %d %d", dim1, lit1);
    CHECK(same_rgb(ground_at(s, 0, 0), (Rgb){0, 0, 255}), "the unlit dots are not the ground colour");
}

static void faint_braille_shows_its_unlit_dots_in_software(void)
{
    Screen *s = soft_screen(14);
    SDL_Rect d[8];
    Uint32 red = SDL_MapRGB(s->glass->format, 255, 0, 0);
    Uint32 blue = SDL_MapRGB(s->glass->format, 0, 0, 255);
    Uint32 black = SDL_MapRGB(s->glass->format, 0, 0, 0);
    int n;
#define AT(x, y) (((Uint32 *)((uint8_t *)s->glass->pixels + (size_t)(y) * s->glass->pitch))[x])
    write_text(s, "\x1b[1;1H\x1b[0;2;38;2;255;0;0;48;2;0;0;255m\xe2\xa0\x81");
    n = dots_px(0xff, s->cw, s->ch, d);
    CHECK(n == 8, "eight dots");
    /* dot 1 lit, dot 8 unlit, and the corner between them black */
    CHECK(AT(s->ox + d[0].x + d[0].w / 2, s->oy + d[0].y + d[0].h / 2) == red, "dot 1 is not ink");
    CHECK(AT(s->ox + d[7].x + d[7].w / 2, s->oy + d[7].y + d[7].h / 2) == blue, "dot 8 is not ground");
    CHECK(AT(s->ox, s->oy) == black, "between the dots is not the wall's black");
    /* without the mark the cell's ground fills it */
    write_text(s, "\x1b[1;1H\x1b[0;38;2;255;0;0;48;2;0;0;255m\xe2\xa0\x81");
    CHECK(AT(s->ox, s->oy) == blue, "unmarked braille lost its ground");
#undef AT
}

/* Whether `n` bytes of `hay` hold `needle` anywhere. */
static int holds(const char *hay, size_t n, const char *needle, size_t k)
{
    size_t i;
    for (i = 0; i + k <= n; i++)
        if (!memcmp(hay + i, needle, k))
            return 1;
    return 0;
}

static void a_picture_says_which_braille_is_faint(void)
{
    Screen *s = gpu_screen(14);
    size_t len = 0;
    char *pic;
    write_text(s, "\x1b[1;1H\x1b[0;2m\xe2\xa0\x81\x1b[22mZ");
    pic = grid_picture(s, &len);
    CHECK(pic, "no picture");
    CHECK(holds(pic, len, "\x1b[2m\xe2\xa0\x81", 7), "the faint cell lost its mark");
    CHECK(holds(pic, len, "\x1b[22mZ", 6), "faint ran on past its cell");
    free(pic);
}

/* Whether what cell x,y shows was drawn by the bold font. */
static int shown_bold(Screen *s, int x, int y)
{
    const Vert *v = vert_at(s, x, y);
    const FakeTexture *t = fake_texture(s->gl.atlas);
    long px = ((long)v->u * t->w + 32767) / 65535;
    long py = ((long)v->v * t->h + 32767) / 65535;
    const uint8_t *a = t->pixels + (size_t)py * t->w + px;
    return (a[2] & FAKE_MARK) && a[2] != 255 && (a[2] & FAKE_BOLD);
}

static void bold_text_is_drawn_in_the_bold_font_and_geometry_is_not(void)
{
    Screen *s = gpu_screen(14);
    s->bold_path = "fake-Bold.ttf";
    CHECK(set_font(s, 14) == 0, "set_font failed");
    CHECK(s->bold, "the bold font was not opened with the face");
    /* bold A, plain A, a bold full block, bold B */
    write_text(s, "\x1b[1;1H\x1b[0;1mA\x1b[22mA\x1b[1m\xe2\x96\x88" "B");
    CHECK(shown(s, 0, 0) == 'A' && shown_bold(s, 0, 0), "bold A is not in the bold font");
    CHECK(shown(s, 1, 0) == 'A' && !shown_bold(s, 1, 0), "SGR 22 did not end bold");
    CHECK(shown(s, 2, 0) == NOT_TYPE, "a bold block was asked of the font");
    CHECK(shown(s, 3, 0) == 'B' && shown_bold(s, 3, 0), "bold B is not in the bold font");
    /* and again at another size, the bold face made with it */
    CHECK(set_font(s, 20) == 0 && s->bold, "the bold font did not follow the size");
    write_text(s, "\x1b[1;1H\x1b[0;1mC");
    CHECK(shown(s, 0, 0) == 'C' && shown_bold(s, 0, 0), "bold C after a font change");
}

static void bold_text_without_a_bold_font_is_drawn_regular(void)
{
    Screen *s = gpu_screen(14);
    CHECK(!s->bold, "a bold font came from nowhere");
    write_text(s, "\x1b[1;1H\x1b[0;1mA");
    CHECK(shown(s, 0, 0) == 'A' && !shown_bold(s, 0, 0), "bold A with no bold font");
}

static void a_picture_of_bold_text_is_its_characters(void)
{
    Screen *s = gpu_screen(14);
    size_t len = 0;
    char *pic;
    s->bold_path = "fake-Bold.ttf";
    CHECK(set_font(s, 14) == 0, "set_font failed");
    write_text(s, "\x1b[1;1H\x1b[0;1mAB\x1b[22mC");
    pic = grid_picture(s, &len);
    CHECK(pic, "no picture");
    CHECK(holds(pic, len, "ABC", 3), "the bold characters did not come back as sent");
    free(pic);
}

static void triangles_fill_to_their_corners_and_complete_each_other(void)
{
    Screen *g = gpu_screen(14), *w = soft_screen(14);
    const FakeTexture *t;
    int k, i, j;
    long pu[4], pv[4];
    /* the four halves, each in its own cell */
    write_text(g, "\x1b[1;1H\xe2\x97\xa2\xe2\x97\xa3\xe2\x97\xa4\xe2\x97\xa5");
    write_text(w, "\x1b[1;1H\x1b[38;2;255;255;255m\xe2\x97\xa2\xe2\x97\xa3\xe2\x97\xa4\xe2\x97\xa5");
    t = fake_texture(g->gl.atlas);
    for (k = 0; k < 4; k++) {
        CHECK(shown(g, k, 0) != TRIANGLE + (uint32_t)k, "triangle %d was typed rather than drawn", k);
        pu[k] = ((long)vert_at(g, k, 0)->u * t->w + 32767) / 65535;
        pv[k] = ((long)vert_at(g, k, 0)->v * t->h + 32767) / 65535;
    }
#define M(k, i, j) t->pixels[(size_t)(pv[k] + (j)) * t->w + pu[k] + (i)]
    /* the corner each one owns is filled right up to the edge */
    CHECK(M(0, g->cw - 1, g->ch - 1) == 255 && M(0, 0, 0) == 0, "lower right");
    CHECK(M(1, 0, g->ch - 1) == 255 && M(1, g->cw - 1, 0) == 0, "lower left");
    CHECK(M(2, 0, 0) == 255 && M(2, g->cw - 1, g->ch - 1) == 0, "upper left");
    CHECK(M(3, g->cw - 1, 0) == 255 && M(3, 0, g->ch - 1) == 0, "upper right");
    /* opposite halves share out every pixel, one each */
    for (j = 0; j < g->ch; j++)
        for (i = 0; i < g->cw; i++) {
            CHECK((M(0, i, j) == 255) != (M(2, i, j) == 255), "pixel %d,%d of lower right/upper left", i, j);
            CHECK((M(1, i, j) == 255) != (M(3, i, j) == 255), "pixel %d,%d of lower left/upper right", i, j);
        }
#undef M
    {
        Uint32 white = SDL_MapRGB(w->glass->format, 255, 255, 255);
#define AT(x, y) (((Uint32 *)((uint8_t *)w->glass->pixels + (size_t)(y) * w->glass->pitch))[x])
        CHECK(AT(w->ox + w->cw - 1, w->oy + w->ch - 1) == white, "software lower right corner");
        CHECK(AT(w->ox, w->oy) != white, "software lower right leaves the upper left");
        CHECK(AT(w->ox + 2 * w->cw, w->oy) == white, "software upper left corner");
#undef AT
    }
}

/* ------------------------------------------------------- a video layer */

/* A frame of w x h pixels, every one `rgba`, as QOI. */
static uint8_t *qoi_of(int w, int h, const uint8_t rgba[4], int *len)
{
    uint8_t *px = malloc((size_t)w * h * 4), *q;
    qoi_desc d = {(unsigned)w, (unsigned)h, 4, QOI_SRGB};
    int i;
    CHECK(px, "out of memory");
    for (i = 0; i < w * h; i++)
        memcpy(px + i * 4, rgba, 4);
    q = qoi_encode(px, &d, len);
    free(px);
    CHECK(q, "qoi_encode failed");
    return q;
}

/* A frame line and its payload, down a video layer's `in`. */
static void send_frame(int in, int w, int h, const uint8_t rgba[4], const char *opts)
{
    int len;
    uint8_t *q = qoi_of(w, h, rgba, &len);
    char line[200];
    int n = snprintf(line, sizeof line, "\x1b_bitwall frame qoi %dx%d %s %d\x1b\\", w, h,
                     opts, len);
    CHECK(write(in, line, (size_t)n) == n, "write failed");
    CHECK(write(in, q, (size_t)len) == len, "write failed");
    free(q);
}

static void put(int in, const char *text)
{
    size_t n = strlen(text);
    CHECK(write(in, text, n) == (ssize_t)n, "write failed");
}

static int layer_open(Wall *w, const char *name, int *out)
{
    char req[100];
    snprintf(req, sizeof req, "open %s layer z=1 type=video", name);
    knock(w, req);
    *out = sess_out(w, name);
    return sess_in(w, name);
}

static void a_video_frame_is_blended_over_the_grid_in_software(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t half[4] = {255, 255, 255, 128};
    Uint32 *g;
    char *said;
    send_frame(in, 4, 2, half, "at=10,5 hold=5000");
    pump(0.2);
    said = out_text(out);
    CHECK(strstr(said, "opened v z=1\n") && strstr(said, "repaint\n"), "not greeted: %.100s", said);
    CHECK(s->pics[1].on && s->pics[1].w == 4 && s->pics[1].x == 10, "the frame is not up");
    SDL_FillRect(s->glass, NULL, SDL_MapRGB(s->glass->format, 0, 0, 200));
    pics_soft(s, 1);
    g = (Uint32 *)s->glass->pixels;
    CHECK(g[5 * s->width + 10] == 0xff8080e4u, "not blended at half: %08x",
          (unsigned)g[5 * s->width + 10]);
    CHECK(g[5 * s->width + 14] == 0xff0000c8u, "drawn past its edge: %08x",
          (unsigned)g[5 * s->width + 14]);
    pics_soft(s, 0);
    CHECK(g[5 * s->width + 10] == 0xff0000c8u, "the picture beneath was not put back");
    close(in);
    close(out);
}

static void a_video_frame_is_a_texture_drawn_over_the_grid_on_the_gpu(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t red[4] = {255, 0, 0, 200};
    const FakeTexture *tx;
    long shown = s->frames, redrawn = s->frames - s->refreshes, painted = s->painted;
    send_frame(in, 3, 3, red, "at=1,2");
    pump(0.2);
    CHECK(s->pics[1].on && s->pics[1].tex, "the frame is not up");
    tx = fake_texture(s->pics[1].tex);
    CHECK(tx && tx->bpp == 4 && tx->w == 3 && tx->pixels[3] == 200 && tx->pixels[0] == 255,
          "the texture is not the frame");
    CHECK(s->frames > shown, "the frame was not shown");
    CHECK(s->frames - s->refreshes > redrawn, "a video frame not counted as a redraw of the glass");
    CHECK(s->painted == painted, "a video frame counted as the base's");
    CHECK(w->above[1]->n_frames == 1 && w->above[1]->n_in > 0, "the layer's own counts: %ld %.0f",
          w->above[1]->n_frames, w->above[1]->n_in);
    {
        /* Its measure line and its row on the overlay, once a second has
         * passed to work its rates out over. */
        Session *v = w->above[1];
        char line[400];
        v->r_at -= 1000;
        session_rates(v, now_ms());
        session_measure(v, now_ms(), line, sizeof line);
        CHECK(strstr(line, "session=v role=layer z=1 type=video fps=") && strstr(line, " decode=")
              && strstr(line, " dropped=0 queue="), "measure: %s", line);
        CHECK(!strstr(line, "decode=-"), "no decode time: %s", line);
        session_row(v, line, sizeof line);
        CHECK(!strncmp(line, "v ", 2) && strstr(line, "1 video") && strstr(line, "0 dropped"),
              "row: %s", line);
    }
    close(in);
    close(out);
}

static void a_video_layer_clears_when_its_hold_runs_out(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {1, 2, 3, 255};
    send_frame(in, 2, 2, c, "hold=150");
    pump(0.08);
    CHECK(s->pics[1].on, "the frame is not up");
    pump(0.25);
    CHECK(!s->pics[1].on, "the frame outlived its hold");
    CHECK(w->above[1] && w->above[1]->state == S_OPEN, "the session ended with the hold");
    close(in);
    close(out);
}

static void a_video_layer_clears_on_clear_close_and_eof(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {1, 2, 3, 255};
    char *said;
    send_frame(in, 2, 2, c, "");
    pump(0.1);
    CHECK(s->pics[1].on, "the frame is not up");
    put(in, "\x1b_bitwall clear\x1b\\");
    pump(0.1);
    CHECK(!s->pics[1].on, "clear left the frame up");
    CHECK(w->above[1] && w->above[1]->state == S_OPEN, "clear ended the session");
    send_frame(in, 2, 2, c, "");
    pump(0.1);
    CHECK(s->pics[1].on, "a frame after clear is not up");
    put(in, "\x1b_bitwall close\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(strstr(said, "closed v asked\n"), "close not said: %.200s", said);
    CHECK(!s->pics[1].on && !w->above[1], "close left the layer up");
    close(in);
    close(out);
    in = layer_open(w, "v2", &out);
    send_frame(in, 2, 2, c, "");
    pump(0.1);
    CHECK(s->pics[1].on, "the second layer's frame is not up");
    close(in);
    pump(0.3);
    said = out_text(out);
    CHECK(strstr(said, "closed v2 eof\n"), "eof not said: %.200s", said);
    CHECK(!s->pics[1].on, "end of file left the frame up");
    close(out);
}

static void a_video_layer_plays_on_through_a_font_change(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {1, 2, 3, 255};
    char *said;
    send_frame(in, 2, 2, c, "hold=5000");
    pump(0.1);
    out_text(out);
    out_text(bout);
    CHECK(s->pics[1].on, "the frame is not up");
    put(bin, "\x1b_bitwall font 20\x1b\\");
    pump(0.15);
    CHECK(s->size == 20, "the font did not change");
    said = out_text(bout);
    CHECK(strstr(said, "area "), "the base was not told its area: %.200s", said);
    CHECK(s->pics[1].on, "a font change took the video layer down");
    said = out_text(out);
    CHECK(!*said, "a font change said something to the video layer: %.200s", said);
    close(in);
    close(out);
    close(bin);
    close(bout);
}

static void a_video_layer_clears_and_is_told_repaint_when_the_pixels_change(void)
{
    char size[300];
    FILE *f;
    Screen *s;
    Wall *w;
    int out, in;
    const uint8_t c[4] = {1, 2, 3, 255};
    char *said;
    snprintf(size, sizeof size, "%s/bitwall-paint-size-%ld", getenv("TMPDIR") ? getenv("TMPDIR") : "/tmp",
             (long)getpid());
    f = fopen(size, "w");
    CHECK(f, "no size file");
    fprintf(f, "%dx%d", fake.win_w, fake.win_h);
    fclose(f);
    setenv("BITWALL_FAKE_SIZE", size, 1);
    s = gpu_screen(14);
    w = wall_at(s, "0");
    in = layer_open(w, "v", &out);
    send_frame(in, 2, 2, c, "hold=5000");
    pump(0.1);
    out_text(out);
    CHECK(s->pics[1].on, "the frame is not up");
    f = fopen(size, "w");
    fprintf(f, "%dx%d", fake.win_w - 80, fake.win_h - 40);
    fclose(f);
    pump(SETTLE / 1e3 + 0.1);   /* taken once it has held */
    unlink(size);
    said = out_text(out);
    CHECK(s->width == fake.win_w - 80, "the glass did not change: %d", s->width);
    CHECK(!s->pics[1].on, "the frame outlived the pixels it was made for");
    CHECK(strstr(said, "screen ") && strstr(said, "repaint\n") && !strstr(said, "area "),
          "not told the new glass and repaint alone: %.200s", said);
    close(in);
    close(out);
}

static void a_video_frame_it_cannot_take_is_skipped_and_said(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {9, 9, 9, 255};
    char *said;
    put(in, "\x1b_bitwall frame vp9 2x2 5\x1b\\hello");
    put(in, "\x1b_bitwall frame qoi 2x2 pts=40 3 id=a\x1b\\abc");
    put(in, "\x1b_bitwall frame qoi 2x2 4 id=b\x1b\\junk");
    send_frame(in, 2, 2, c, "at=3,4");
    pump(0.2);
    said = out_text(out);
    CHECK(strstr(said, "error frame vp9: not offered\n"), "vp9: %.300s", said);
    CHECK(strstr(said, "error frame pts=40: unknown option id=a\n"), "pts: %.300s", said);
    CHECK(strstr(said, "error frame qoi: does not decode id=b\n"), "junk: %.300s", said);
    CHECK(s->pics[1].on && s->pics[1].x == 3 && s->pics[1].y == 4,
          "the good frame after them is not up");
    close(in);
    close(out);
}

static void a_stalled_video_layer_never_holds_up_the_base(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = layer_open(w, "v", &out);
    put(in, "\x1b_bitwall frame qoi 64x64 at=0,0 100000\x1b\\abc");
    pump(0.1);
    put(bin, "\x1b[1;1HZ\x1b[0m");
    pump(0.15);
    CHECK(shown(s, 0, 0) == 'Z', "the base was held up by a layer mid-frame");
    CHECK(w->above[1] && w->above[1]->state == S_OPEN, "the layer went");
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* A layer's frames reach the glass by themselves, one redraw each, with
 * the base neither presenting nor done painting a frame. On the GPU the
 * base's part of that redraw is its last whole frame; in software what
 * the base has changed so far waits for its delimiter. */
static void layer_frames_redraw_the_glass_over_a_base_mid_frame(int gpu)
{
    Screen *s = gpu ? gpu_screen(14) : soft_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {9, 9, 9, 255};
    long frames, painted;
    int k, which;
    put(bin, "\x1b[1;1HA\x1b[0m");
    pump(0.1);
    put(bin, "\x1b[1;1HB");                /* a frame begun and not ended */
    pump(0.1);
    CHECK(s->drew, "the base is not mid-frame");
    frames = s->frames;
    painted = s->painted;
    which = s->gl.which;
    for (k = 1; k <= 3; k++) {
        send_frame(in, 2, 2, c, "hold=150");
        pump(0.05);
        CHECK(s->frames == frames + k, "layer frame %d: %ld redraws", k, s->frames - frames);
    }
    pump(0.3);                              /* the hold runs out */
    CHECK(!s->pics[1].on, "the frame outlived its hold");
    CHECK(s->frames == frames + 4, "the end of the hold was not redrawn: %ld", s->frames - frames);
    CHECK(s->painted == painted && s->drew, "the base's frame was ended for it");
    if (gpu)
        CHECK(s->gl.whole && s->gl.which == which, "half the base's frame went up with a layer");
    if (!gpu)
        CHECK(s->ndirty || s->dirty_all, "the base's changes were pushed out before its delimiter");
    put(bin, "\x1b[0m");
    pump(0.1);
    CHECK(s->painted == painted + 1, "the base's frame did not end");
    CHECK(gpu ? shown(s, 0, 0) == 'B' : !s->ndirty && !s->dirty_all, "the base's frame is not up");
    close(in);
    close(out);
    close(bin);
    close(bout);
}

static void a_layer_frame_redraws_the_glass_over_a_base_mid_frame_on_the_gpu(void)
{
    layer_frames_redraw_the_glass_over_a_base_mid_frame(1);
}

static void a_layer_frame_redraws_the_glass_over_a_base_mid_frame_in_software(void)
{
    layer_frames_redraw_the_glass_over_a_base_mid_frame(0);
}

static const char *caps_of(Wall *w);

static void a_layer_is_placed_above_top_or_where_it_asks(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    /* In order: each ask against the stack the ones before it made. */
    static const char *asks[][3] = {
        {"open a layer z=above type=video", "a", "opened a z=1\n"},
        {"open b layer z=top type=video", "b", "opened b z=7\n"},
        {"open c layer z=above type=video", "c", "opened c z=2\n"},
        {"open d layer z=top type=video", "d", "opened d z=6\n"},
        {"open e layer z=4 type=video", "e", "opened e z=3\n"},
        {"open f layer z=3 type=video", "f", "opened f z=3\n"},
        {"open g layer z=1 type=video take=1", "g", "error open g no-such-key"},
        {"open h layer z=above type=video take=1", "h", "error open h no-such-key"},
        {"open i layer z=above type=mono", "i", "error open i no-such-type"},
        {"open j layer z=any type=video", "j", "error open j no-such-key"},
        {"open k layer z=8 type=video", "k", "error open k full"},
        {"open l layer z=0 type=video", "l", "opened l z=0\n"},
        {"open m layer z=1 type=video data=1", "m", "error open m no-such-codec"},
        {"open n layer z=above type=video", "n", "opened n z=5\n"},
        {"open o layer z=top type=video", "o", "error open o full"},
        {"open p layer z=above type=video", "p", "error open p full"},
        {"open q layer z=top type=video", "q", "error open q full"},
    };
    size_t i;
    int outs[32], nouts = 0, o, z;
    char *said;
    for (i = 0; i < sizeof asks / sizeof asks[0]; i++) {
        knock(w, asks[i][0]);
        o = sess_out(w, asks[i][1]);
        pump(0.05);
        said = out_text(o);
        CHECK(strstr(said, asks[i][2]), "%s: %.100s", asks[i][0], said);
        outs[nouts++] = o;
    }
    for (z = 1; z < LAYERS; z++)
        CHECK(w->above[z] && w->above[z]->state == S_OPEN, "layer %d not held", z);
    /* A layer that goes leaves no gap: the rest of its stack closes up,
     * and `above` goes on top of it again. */
    session_end(w->above[2], "asked");
    CHECK(w->above[2] && !strcmp(w->above[2]->name, "f"), "the stack did not close up");
    knock(w, "open r layer z=above type=video");
    o = sess_out(w, "r");
    pump(0.05);
    said = out_text(o);
    CHECK(strstr(said, "opened r z=5\n"), "above did not go on top: %.100s", said);
    outs[nouts++] = o;
    CHECK(strstr(caps_of(w), "layers=8 "), "caps does not say layers=8");
    while (nouts)
        close(outs[--nouts]);
}

/* The open session called `name`, wherever in the stack it is. */
static Session *layer_named(Wall *w, const char *name)
{
    Session *x;
    for (x = w->sessions; x; x = x->next)
        if (x->state == S_OPEN && !strcmp(x->name, name))
            return x;
    return NULL;
}

/* The caps line a report says. */
static const char *caps_of(Wall *w)
{
    static char line[600];
    int o;
    char *said, *c, *e;
    knock(w, "open capsask controller");
    o = sess_out(w, "capsask");
    pump(0.05);
    {
        int in = sess_in(w, "capsask");
        put(in, "\x1b_bitwall report\x1b\\");
        pump(0.1);
        close(in);
    }
    said = out_text(o);
    close(o);
    line[0] = 0;
    if ((c = strstr(said, "caps ")) && (e = strchr(c, '\n'))) {
        size_t n = (size_t)(e - c) < sizeof line - 2 ? (size_t)(e - c) : sizeof line - 2;
        memcpy(line, c, n);
        line[n] = ' ';
        line[n + 1] = 0;
    }
    return line;
}

/* Two layers blended bottom to top: the upper wins where it is opaque, and
 * the lower shows through where it is not, on the glass and in a picture. */
static void layers_blend_bottom_to_top(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    const uint8_t red[4] = {255, 0, 0, 255}, green[4] = {0, 255, 0, 255};
    int out1, out2, in1, in2;
    uint8_t *px;
    Uint32 *g;
    knock(w, "open lo layer z=above type=video");
    out1 = sess_out(w, "lo");
    in1 = sess_in(w, "lo");
    knock(w, "open hi layer z=top type=video");
    out2 = sess_out(w, "hi");
    in2 = sess_in(w, "hi");
    send_frame(in2, 4, 4, green, "at=12,5 hold=5000");
    send_frame(in1, 4, 4, red, "at=10,5 hold=5000");
    pump(0.2);
    CHECK(s->pics[1].on && s->pics[LAYERS - 1].on, "the frames are not up");
    SDL_FillRect(s->glass, NULL, SDL_MapRGB(s->glass->format, 0, 0, 200));
    px = glass_rgba(s, s->width, s->height);
    CHECK(px, "no glass");
#define P(x, y, k) px[((y) * s->width + (x)) * 4 + (k)]
    CHECK(P(10, 5, 0) == 255 && P(10, 5, 1) == 0, "the lower layer is not under: %d %d",
          P(10, 5, 0), P(10, 5, 1));
    CHECK(P(12, 5, 0) == 0 && P(12, 5, 1) == 255, "the upper layer is not on top: %d %d",
          P(12, 5, 0), P(12, 5, 1));
    CHECK(P(15, 5, 1) == 255 && P(16, 5, 2) == 200, "the upper layer's edge");
#undef P
    free(px);
    pics_soft(s, 1);
    g = (Uint32 *)s->glass->pixels;
    CHECK((g[5 * s->width + 11] & 0xffffff) == 0xff0000u, "glass: lower %06x",
          (unsigned)(g[5 * s->width + 11] & 0xffffff));
    CHECK((g[5 * s->width + 13] & 0xffffff) == 0x00ff00u, "glass: upper %06x",
          (unsigned)(g[5 * s->width + 13] & 0xffffff));
    pics_soft(s, 0);
    CHECK((g[5 * s->width + 13] & 0xffffff) == 0x0000c8u, "glass not put back: %06x",
          (unsigned)(g[5 * s->width + 13] & 0xffffff));
    close(in1);
    close(in2);
    close(out1);
    close(out2);
}

/* ------------------------------------------------------------- capture */

/* Everything waiting on a session's out, bytes and all, and how many. */
static uint8_t *out_bytes(int fd, size_t *n)
{
    static uint8_t buf[1 << 22];
    ssize_t k;
    int quiet = 0;
    *n = 0;
    /* A picture is bigger than the pipe: read, let the wall write more, and
     * read again, until it has nothing left to say. */
    while (quiet < 3 && *n < sizeof buf) {
        k = read(fd, buf + *n, sizeof buf - *n);
        if (k > 0) {
            *n += (size_t)k;
            quiet = 0;
        } else {
            quiet += !pool.out;     /* a picture the pool is still making */
            pump(0.01);
        }
    }
    return buf;
}

/* The first blob on `out` whose line starts with `head`: its line, and its
 * bytes. Null when there is none. */
static const uint8_t *blob_of(const uint8_t *b, size_t n, const char *head, char *line,
                              size_t *len)
{
    size_t i = 0;
    while (i < n) {
        const uint8_t *nl = memchr(b + i, '\n', n - i);
        size_t l, count = 0;
        int blob;
        if (!nl)
            return NULL;
        l = (size_t)(nl - (b + i));
        blob = !strncmp((const char *)b + i, "picture ", 8) || !strncmp((const char *)b + i, "mirror ", 7);
        if (blob && l < 200) {
            char words[200], *w, *save = NULL;
            memcpy(words, b + i, l);
            words[l] = 0;
            for (w = strtok_r(words, " ", &save); w; w = strtok_r(NULL, " ", &save))
                if (strncmp(w, "id=", 3))
                    count = strtoul(w, NULL, 10);
            if (!strncmp((const char *)b + i, head, strlen(head))) {
                memcpy(line, b + i, l);
                line[l] = 0;
                *len = count;
                return b + i + l + 1;
            }
        }
        i += l + 1 + (blob ? count : 0);
    }
    return NULL;
}

static int ctl_open(Wall *w, const char *name, int *out)
{
    char req[80];
    int in;
    snprintf(req, sizeof req, "open %s controller", name);
    knock(w, req);
    *out = sess_out(w, name);
    in = sess_in(w, name);
    put(in, "\x1b_bitwall ping\x1b\\");
    pump(0.05);
    return in;
}

static void a_glass_picture_in_software_blends_the_layers_in(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out);
    const uint8_t half[4] = {255, 255, 255, 128};
    uint8_t *px;
    send_frame(in, 4, 2, half, "at=10,5 hold=5000");
    pump(0.2);
    SDL_FillRect(s->glass, NULL, SDL_MapRGB(s->glass->format, 0, 0, 200));
    px = glass_rgba(s, s->width, s->height);
    CHECK(px, "no glass");
    CHECK(px[(5 * s->width + 10) * 4] == 128 && px[(5 * s->width + 10) * 4 + 2] == 228
          && px[(5 * s->width + 10) * 4 + 3] == 255, "not blended: %d %d",
          px[(5 * s->width + 10) * 4], px[(5 * s->width + 10) * 4 + 2]);
    CHECK(px[(5 * s->width + 14) * 4 + 2] == 200, "drawn past its edge");
    free(px);
    px = glass_rgba(s, s->width / 2, s->height / 2);
    CHECK(px && px[(3 * (s->width / 2) + 5) * 4] == 128, "the smaller glass lost the layer");
    free(px);
    close(in);
    close(out);
}

static void a_glass_picture_is_drawn_at_its_size_and_read_back(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out);
    size_t n, len;
    const uint8_t *b, *q;
    char line[200];
    qoi_desc d;
    void *px;
    out_bytes(out, &n);
    put(in, "\x1b_bitwall picture glass size=48x27 id=a\x1b\\");
    pump(0.1);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "picture glass qoi 48x27 ", line, &len);
    CHECK(q && strstr(line, " id=a"), "no picture: %.*s", (int)(n < 120 ? n : 120), (const char *)b);
    px = qoi_decode(q, (int)len, &d, 4);
    CHECK(px && d.width == 48 && d.height == 27, "the picture is not a 48x27 QOI");
    free(px);
    CHECK(fake.framebuffer == 0, "the window's framebuffer was not bound again");
    put(in, "\x1b_bitwall picture glass id=b\x1b\\");
    pump(0.1);
    b = out_bytes(out, &n);
    CHECK(blob_of(b, n, "picture glass qoi ", line, &len), "no full picture");
    {
        char want[64];
        snprintf(want, sizeof want, "picture glass qoi %dx%d ", s->width, s->height);
        CHECK(!strncmp(line, want, strlen(want)), "not the glass's own size: %s", line);
    }
#ifdef BITWALL_JPEG
    put(in, "\x1b_bitwall picture glass size=48x27 format=jpeg quality=60 id=j\x1b\\");
    pump(0.1);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "picture glass jpeg 48x27 ", line, &len);
    CHECK(q && strstr(line, " id=j"), "no JPEG: %.*s", (int)(n < 120 ? n : 120), (const char *)b);
    {
        int jw = 0, jh = 0;
        px = jpeg_rgba(q, len, &jw, &jh);
        CHECK(px && jw == 48 && jh == 27, "the picture is not a 48x27 JPEG");
        free(px);
    }
    put(in, "\x1b_bitwall picture glass format=jpeg quality=0 id=x\x1b\\"
            "\x1b_bitwall picture glass quality=50 id=y\x1b\\");
    pump(0.1);
    {
        const char *said = out_text(out);
        CHECK(strstr(said, "error picture quality=0: bad quality id=x"), "%.300s", said);
        CHECK(strstr(said, "error picture quality=50: only for jpeg id=y"), "%.300s", said);
    }
#else
    put(in, "\x1b_bitwall picture glass format=jpeg id=j\x1b\\");
    pump(0.1);
    CHECK(strstr(out_text(out), "error picture format=jpeg: not offered id=j"), "jpeg offered");
#endif
    close(in);
    close(out);
}

static void a_picture_of_each_session_is_in_its_own_format(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), vout, vin = layer_open(w, "v", &vout), out, in;
    const uint8_t c[4] = {10, 20, 30, 255};
    size_t n, len;
    const uint8_t *b, *q;
    char line[200], ask[200];
    in = ctl_open(w, "c", &out);
    put(bin, "\x1b[1;1HA\x1b[0m");
    put(vin, "\x1b_bitwall picture v id=e\x1b\\");
    pump(0.1);
    CHECK(strstr(out_text(vout), "error picture v: nothing shown id=e"), "an empty layer pictured");
    send_frame(vin, 3, 2, c, "at=7,9 hold=5000");
    pump(0.1);
    put(in, "\x1b_bitwall picture v id=v\x1b\\");
    snprintf(ask, sizeof ask, "\x1b_bitwall picture base id=g\x1b\\");
    put(in, ask);
    pump(0.1);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "picture v qoi 3x2 at=7,9 ", line, &len);
    CHECK(q && len > 14 && !memcmp(q, "qoif", 4), "no picture of the video layer");
    q = blob_of(b, n, "picture base grid ", line, &len);
    CHECK(q && memchr(q, 'A', len), "no grid of the base: %s [%.*s]", q ? line : "-", (int)(q && len < 300 ? len : 0), q ? (const char *)q : "");
    put(in, "\x1b_bitwall picture nobody id=1\x1b\\\x1b_bitwall picture c id=2\x1b\\"
            "\x1b_bitwall picture base size=4x4 id=3\x1b\\\x1b_bitwall picture\x1b\\"
            "\x1b_bitwall picture glass size=0x9\x1b\\");
    pump(0.1);
    {
        const char *said = out_text(out);
        CHECK(strstr(said, "error picture nobody: no such session id=1"), "%.300s", said);
        CHECK(strstr(said, "error picture c: not a layer id=2"), "%.300s", said);
        CHECK(strstr(said, "error picture base size=4x4: only for the glass id=3"), "%.300s", said);
        CHECK(strstr(said, "error picture no target"), "%.300s", said);
        CHECK(strstr(said, "error picture size=0x9: bad size"), "%.300s", said);
    }
    close(in);
    close(out);
    close(vin);
    close(vout);
    close(bin);
    close(bout);
}

static void a_mirror_of_each_target_sends_what_changed(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), vout, vin = layer_open(w, "v", &vout), out, in;
    const uint8_t c[4] = {10, 20, 30, 255};
    size_t n, len;
    const uint8_t *b, *q;
    char line[200];
    int sent;
    uint8_t *data;
    in = ctl_open(w, "c", &out);
    put(bin, "\x1b[1;1HA\x1b[0m");
    put(in, "\x1b_bitwall mirror glass every=50 size=32x18\x1b\\");
    pump(0.3);
    b = out_bytes(out, &n);
    CHECK(blob_of(b, n, "mirror glass qoi 32x18 ", line, &len), "no glass mirror");
    pump(0.2);
    b = out_bytes(out, &n);
    CHECK(!blob_of(b, n, "mirror glass ", line, &len), "an unchanged glass was sent again");
    put(in, "\x1b_bitwall mirror glass off\x1b\\\x1b_bitwall mirror base every=50\x1b\\");
    pump(0.15);
    b = out_bytes(out, &n);
    CHECK(blob_of(b, n, "mirror base grid ", line, &len), "no whole grid first");
    put(bin, "\x1b[1;2HB\x1b[0m");
    pump(0.15);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "mirror base diff ", line, &len);
    CHECK(q && memchr(q, 'B', len) && !memchr(q, 'A', len), "the diff is not what changed");
    put(in, "\x1b_bitwall mirror base off\x1b\\\x1b_bitwall mirror v\x1b\\");
    pump(0.05);
    out_bytes(out, &n);
    data = qoi_of(3, 2, c, &sent);
    send_frame(vin, 3, 2, c, "at=7,9 hold=700");
    pump(0.1);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "mirror v qoi 3x2 at=7,9 hold=700 ", line, &len);
    CHECK(q && len == (size_t)sent && !memcmp(q, data, len), "the frame was not passed on as it came");
    free(data);
    CHECK(!blob_of(b, n, "mirror base", line, &len), "a stopped mirror went on");
    close(in);
    close(out);
    close(vin);
    close(vout);
    close(bin);
    close(bout);
}

static void a_mirror_frame_is_dropped_while_the_last_waits(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int vout, vin = layer_open(w, "v", &vout), out, in, i, blobs = 0;
    uint8_t c[4] = {0, 0, 0, 255};
    Session *x;
    OutItem *it;
    in = ctl_open(w, "c", &out);
    put(in, "\x1b_bitwall mirror v\x1b\\");
    pump(0.05);
    for (i = 0; i < 60; i++) {
        c[0] = (uint8_t)i;
        send_frame(vin, 300, 200, c, "");    /* never read by the controller */
        pump(0.01);
    }
    for (x = w->sessions; x && strcmp(x->name, "c"); x = x->next)
        ;
    CHECK(x, "no controller");
    for (it = x->qhead; it; it = it->next)
        blobs += it->kind == OUT_BLOB;
    CHECK(blobs <= 1, "%d mirror frames queued behind each other", blobs);
    close(in);
    close(out);
    close(vin);
    close(vout);
}

static void a_dark_wall_is_pictured_as_though_it_were_lit(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout);
    uint8_t *px;
    blank(s, 1);
    CHECK(s->target != s->glass, "a blank wall painted its glass");
    put(bin, "\x1b[1;1H\x1b[48;2;0;200;0m \x1b[0m");
    pump(0.1);
    px = glass_rgba(s, s->width, s->height);
    {
        size_t at = ((size_t)s->oy * s->width + s->ox) * 4;
        CHECK(px && px[at + 1] == 200 && px[at] == 0,
              "the dark glass did not show what would show: %d,%d,%d",
              px ? px[at] : -1, px ? px[at + 1] : -1, px ? px[at + 2] : -1);
    }
    free(px);
    close(bin);
    close(bout);
}

static void a_dark_wall_answers_a_glass_picture_on_the_gpu(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out);
    size_t n, len;
    const uint8_t *b;
    char line[200];
    blank(s, 1);
    power(s, 0);
    out_bytes(out, &n);
    put(in, "\x1b_bitwall picture glass size=32x18 id=d\x1b\\");
    pump(0.1);
    b = out_bytes(out, &n);
    CHECK(blob_of(b, n, "picture glass qoi 32x18 ", line, &len), "a dark wall gave no picture");
    close(in);
    close(out);
}

static void size_is_the_most_a_glass_picture_may_be(void)
{
    Screen *s = gpu_screen(14);
    int bw, bh;
    s->width = 1920;
    s->height = 1080;
    bw = 480, bh = 480;
    glass_fit(s, &bw, &bh);
    CHECK(bw == 480 && bh == 270, "480x480 of 1920x1080 is %dx%d", bw, bh);
    bw = 4000, bh = 90;
    glass_fit(s, &bw, &bh);
    CHECK(bw == 160 && bh == 90, "4000x90 of 1920x1080 is %dx%d", bw, bh);
    bw = 4000, bh = 4000;
    glass_fit(s, &bw, &bh);
    CHECK(bw == 1920 && bh == 1080, "a glass was made larger: %dx%d", bw, bh);
}

static void glass_is_not_a_session_name(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    char path[800];
    knock(w, "open glass controller");
    pump(0.1);
    snprintf(path, sizeof path, "%s/s/glass", w->dir);
    CHECK(access(path, F_OK) != 0, "a session named glass was made");
}

/* Where `what` first is in the n bytes at `b`, or null. */
static const uint8_t *find_bytes(const uint8_t *b, size_t n, const char *what)
{
    size_t k = strlen(what), i;
    for (i = 0; i + k <= n; i++)
        if (!memcmp(b + i, what, k))
            return b + i;
    return NULL;
}

/* The pool's jobs held up as a slow decode or encode would be: a frame
 * placed at x=1 for 300 ms, and a picture's encode for 200. */
static void slow_jobs(Job *j)
{
    if (j->kind == JOB_FRAME && j->fx == 1)
        usleep(300000);
    else if (j->kind == JOB_QOI)
        usleep(200000);
}

/* A layer's frames go up in the order sent, a slow one never overtaken by
 * the next, while another layer's go up beside it as they come. */
static void a_layers_frames_go_up_in_order_however_long_each_decodes(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out), cout, cin, out2, in2;
    const uint8_t a[4] = {10, 0, 0, 255}, b[4] = {20, 0, 0, 255}, c[4] = {30, 0, 0, 255};
    const uint8_t *said, *first, *second;
    size_t n;
    pool.hook = slow_jobs;
    knock(w, "open u layer z=2 type=video");
    out2 = sess_out(w, "u");
    in2 = sess_in(w, "u");
    cin = ctl_open(w, "c", &cout);
    put(cin, "\x1b_bitwall mirror v\x1b\\");
    pump(0.05);
    out_bytes(cout, &n);
    send_frame(in, 2, 2, a, "at=1,0");      /* slow */
    put(in, "\x1b_bitwall frame vp9 2x2 5 id=e\x1b\\hello");
    send_frame(in, 2, 2, b, "at=2,0");
    put(in, "\x1b_bitwall ping id=p\x1b\\");
    pump(0.1);
    send_frame(in2, 2, 2, c, "at=5,0");
    pump(0.1);
    CHECK(s->pics[2].on && s->pics[2].x == 5, "the other layer waited on this one's decode");
    CHECK(!s->pics[1].on, "a frame went up before the slow one before it");
    {
        char *said = out_text(out);
        CHECK(!strstr(said, "pong") && !strstr(said, "error frame vp9"),
              "an answer after the slow frame came before it: %.200s", said);
    }
    pump(0.4);
    CHECK(s->pics[1].on && s->pics[1].x == 2, "the last frame sent is not the one up: x=%d",
          s->pics[1].x);
    {
        char *said = out_text(out), *e = strstr(said, "error frame vp9: not offered id=e\n");
        CHECK(e && strstr(e, "pong id=p"), "not the error, then pong: %.200s", said);
    }
    said = out_bytes(cout, &n);
    first = find_bytes(said, n, "mirror v qoi 2x2 at=1,0 ");
    second = find_bytes(said, n, "mirror v qoi 2x2 at=2,0 ");
    CHECK(first && second && first < second, "the frames went up out of order");
    close(in);
    close(out);
    close(in2);
    close(out2);
    close(cin);
    close(cout);
}

/* While a frame decodes the loop goes round as ever: the base's cells are
 * painted, and a turn of it takes no longer for the decode. */
static void a_slow_decode_never_holds_up_the_main_loop(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = layer_open(w, "v", &out);
    const uint8_t c[4] = {9, 9, 9, 255};
    double t0, turn;
    pool.hook = slow_jobs;
    send_frame(in, 2, 2, c, "at=1,0");
    pump(0.05);
    CHECK(w->above[1] && w->above[1]->jhead, "the frame is not with the pool");
    put(bin, "\x1b[1;1HZ\x1b[0m");
    t0 = now_ms();
    pump(0);
    turn = now_ms() - t0;
    pump(0.05);
    CHECK(shown(s, 0, 0) == 'Z', "the base was held up by a layer's decode");
    CHECK(turn < 100, "a turn of the loop took %.0f ms while a frame decoded", turn);
    CHECK(!s->pics[1].on, "the frame was up before it was decoded");
    pump(0.4);
    CHECK(s->pics[1].on && s->pics[1].x == 1, "the frame never went up");
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* A picture being made holds its place among a session's answers, and one
 * whose session goes first is let go. */
static void a_picture_keeps_its_place_among_the_answers(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), out2, in2 = ctl_open(w, "d", &out2);
    const uint8_t *b, *pic, *pong;
    size_t n, len;
    char line[200];
    pool.hook = slow_jobs;
    out_bytes(out, &n);
    put(in, "\x1b_bitwall picture glass size=32x18 id=a\x1b\\\x1b_bitwall ping id=b\x1b\\");
    put(in2, "\x1b_bitwall picture glass size=32x18\x1b\\\x1b_bitwall close\x1b\\");
    pump(0.05);
    CHECK(!strstr(out_text(out), "pong"), "the ping was answered before the picture");
    b = out_bytes(out, &n);
    pic = blob_of(b, n, "picture glass qoi 32x18 ", line, &len);
    pong = find_bytes(b, n, "pong id=b\n");
    CHECK(pic && strstr(line, " id=a") && pong && pong > pic, "not the picture, then pong");
    pump(0.3);
    CHECK(!pool.out, "%d jobs still out", pool.out);
    close(in);
    close(out);
    close(in2);
    close(out2);
}

static void a_video_mirror_is_told_when_the_layer_is_taken_down(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int vout, vin = layer_open(w, "v", &vout), out, in;
    const uint8_t c[4] = {1, 2, 3, 255};
    const uint8_t *said, *a;
    size_t n;
    in = ctl_open(w, "c", &out);
    put(in, "\x1b_bitwall mirror v\x1b\\");
    pump(0.05);
    send_frame(vin, 2, 2, c, "");
    pump(0.1);
    put(vin, "\x1b_bitwall clear\x1b\\");
    pump(0.1);
    send_frame(vin, 2, 2, c, "hold=100");
    pump(0.35);
    said = out_bytes(out, &n);
    a = find_bytes(said, n, "mirror v clear\n");
    CHECK(a && find_bytes(a + 1, n - (size_t)(a + 1 - said), "mirror v clear\n"),
          "clear and the hold were not both passed on");
    close(in);
    close(out);
    close(vin);
    close(vout);
}

/* ------------------------------------------- a video base, and `data` */

/* Pump until `cond` holds or `secs` have gone; whether it held. */
#define PUMP_UNTIL(cond, secs)                                            \
    ({                                                                    \
        double until_ = now_ms() + (secs) * 1e3;                          \
        int held_;                                                        \
        while (!(held_ = !!(cond)) && now_ms() < until_)                  \
            pump(0.02);                                                   \
        held_;                                                            \
    })

static int data_writer(Wall *w, const char *name)
{
    char path[800];
    int fd;
    snprintf(path, sizeof path, "%s/s/%s/data", w->dir, name);
    fd = open(path, O_WRONLY);
    CHECK(fd >= 0, "no data for %s: %s", name, strerror(errno));
    return fd;
}

static void put_qoi(int fd, int w, int h, const uint8_t rgba[4])
{
    int len;
    uint8_t *q = qoi_of(w, h, rgba, &len);
    CHECK(write(fd, q, (size_t)len) == len, "write failed");
    free(q);
}

static void a_video_base_holds_the_base_and_its_first_frame_is_a_sender(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    static const uint8_t red[4] = {255, 0, 0, 255};
    int o, in, o2, c, co;
    char *said;
    knock(w, "open v layer z=0 type=video");
    o = sess_out(w, "v");
    in = sess_in(w, "v");
    pump(0.05);
    said = out_text(o);
    CHECK(strstr(said, "opened v z=0\n"), "%.200s", said);
    CHECK(w->base == w->above[0] && w->base && w->base->is_video, "the video base is not the base");
    CHECK(!w->connected, "a video base with no frame is a sender");
    knock(w, "open b layer z=0 type=cells");
    o2 = sess_out(w, "b");
    pump(0.05);
    CHECK(strstr(out_text(o2), "error open b busy"), "a held video base was not busy");
    close(o2);
    put(in, "\x1b_bitwall font 20 id=f\x1b\\");
    send_frame(in, 8, 4, red, "at=0,0 hold=5000");
    CHECK(PUMP_UNTIL(s->pics[0].on, 2), "the base's frame is not up");
    CHECK(w->connected, "the video base's first frame did not give the wall a sender");
    said = out_text(o);
    CHECK(strstr(said, "error font not-for-role id=f"), "a video base set a font: %.300s", said);
    c = ctl_open(w, "ctl", &co);
    put(c, "\x1b_bitwall sessions\x1b\\");
    pump(0.1);
    said = out_text(co);
    CHECK(strstr(said, "session v layer 0 video"), "%.300s", said);
    knock(w, "open t layer z=0 type=cells take=1");
    o2 = sess_out(w, "t");
    pump(0.1);
    CHECK(strstr(out_text(o), "closed v displaced"), "take did not displace the video base");
    CHECK(!s->pics[0].on && !w->above[0] && w->base && !w->base->is_video,
          "the displaced video base stayed up");
    close(o2);
    close(o);
    close(in);
    close(c);
    close(co);
}

static void data_is_refused_where_it_cannot_be_taken(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    static const char *asks[][3] = {
        {"open a layer z=1 type=video data=1 codec=rgba", "a", "error open a no-such-codec"},
        {"open b layer z=1 type=video data=1", "b", "error open b no-such-codec"},
        {"open c layer z=1 type=video data=1 codec=hevc", "c", "error open c no-such-codec"},
        {"open d layer z=1 type=video codec=qoi", "d", "error open d no-such-key"},
        {"open e layer z=1 type=video fps=24", "e", "error open e no-such-key"},
        {"open f layer z=1 type=video data=1 codec=qoi start=1000", "f", "error open f no-such-key"},
        {"open g layer z=1 type=video data=1 codec=qoi fps=0", "g", "error open g no-such-key"},
        {"open h layer z=1 type=video data=1 codec=qoi fps=abc", "h", "error open h no-such-key"},
        {"open i layer z=1 type=video data=1 codec=qoi fps=241", "i", "error open i no-such-key"},
        {"open j layer z=0 type=cells data=1 codec=qoi", "j", "error open j no-such-key"},
        {"open k layer z=1 type=video data=2 codec=qoi", "k", "error open k no-such-key"},
        {"open l layer z=1 type=video data=1 codec=qoi fps=24 start=soon", "l", "error open l no-such-key"},
        {"open m controller data=1", "m", "error open m no-such-key"},
        {"open n layer z=1 type=video data=1 codec=qoi fps=23.976 start=1790000000000", "n",
         "opened n z=1 data\n"},
    };
    size_t i;
    char path[800];
    codecs.ok[CODEC_HEVC] = 0;
    for (i = 0; i < sizeof asks / sizeof asks[0]; i++) {
        int o;
        char *said;
        knock(w, asks[i][0]);
        o = sess_out(w, asks[i][1]);
        pump(0.05);
        said = out_text(o);
        CHECK(strstr(said, asks[i][2]), "%s: %.100s", asks[i][0], said);
        close(o);
    }
    snprintf(path, sizeof path, "%s/s/n/data", w->dir);
    CHECK(access(path, F_OK) == 0, "opened with data and no data fifo");
    CHECK(w->above[1]->ds && w->above[1]->ds->fps > 23.9 && w->above[1]->ds->start > 1.7e12,
          "fps and start not kept");
    snprintf(path, sizeof path, "%s/s/a/data", w->dir);
    CHECK(access(path, F_OK) != 0, "a refused session has a data fifo");
    {
        const char *caps = caps_of(w);
        CHECK(strstr(caps, " data ") && strstr(caps, "codecs=qoi "), "caps: %s", caps);
    }
}

static void a_qoi_stream_on_data_fills_the_glass_and_ends_clearing_the_layer(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    static const uint8_t green[4] = {0, 255, 0, 255};
    int o, in, d;
    Session *v;
    knock(w, "open v layer z=1 type=video data=1 codec=qoi");
    o = sess_out(w, "v");
    in = sess_in(w, "v");
    put(in, "\x1b_bitwall ping\x1b\\");
    pump(0.05);
    CHECK(strstr(out_text(o), "opened v z=1 data\n"), "not opened with data");
    v = w->above[1];
    d = data_writer(w, "v");
    put_qoi(d, 16, 9, green);
    CHECK(PUMP_UNTIL(s->pics[1].on, 2), "the stream's frame is not up");
    /* Fitted: the largest 16:9 the glass holds, centred. */
    {
        SDL_Rect r;
        pic_fit(s, 16, 9, 0, NULL, &r);
        CHECK(s->pics[1].w == r.w && s->pics[1].h == r.h && s->pics[1].x == r.x
              && s->pics[1].y == r.y && (r.w == s->width || r.h == s->height),
              "not fitted: %dx%d at %d,%d on %dx%d", s->pics[1].w, s->pics[1].h,
              s->pics[1].x, s->pics[1].y, s->width, s->height);
    }
    /* A stream's frame has no hold, and the loop still wakes on time. */
    CHECK(wall_wait(w, 20) >= 0 && wall_wait(w, 20) <= 20, "wait %d", wall_wait(w, 20));
    put_qoi(d, 16, 9, green);
    CHECK(PUMP_UNTIL(v->n_frames >= 2, 2), "the second frame was not shown");
    CHECK(v->n_in > 0, "data bytes not counted into the session's in");
    /* `align`: a frame wider than the glass's shape against its top, then
     * its bottom; another word is said to be wrong. */
    put(in, "\x1b_bitwall align top\x1b\\");
    pump(0.05);
    put_qoi(d, 64, 4, green);
    CHECK(PUMP_UNTIL(v->n_frames >= 3 && s->pics[1].y == 0, 2), "align top: at y=%d",
          s->pics[1].y);
    put(in, "\x1b_bitwall align bottom\x1b\\");
    pump(0.05);
    put_qoi(d, 64, 4, green);
    CHECK(PUMP_UNTIL(v->n_frames >= 4 && s->pics[1].y + s->pics[1].h == s->height, 2),
          "align bottom: %d high at y=%d on %d", s->pics[1].h, s->pics[1].y, s->height);
    put(in, "\x1b_bitwall align middle\x1b\\");
    pump(0.05);
    CHECK(strstr(out_text(o), "error align middle: not top, centre or bottom"),
          "align middle was not refused");
    /* `box`: the frames fitted to a box on the glass, `align` within it,
     * then `box` alone gives the whole glass back; a box with no size or
     * a bad one is said to be wrong. */
    put(in, "\x1b_bitwall box at=10,20 size=200x100\x1b\\");
    pump(0.05);
    put_qoi(d, 16, 9, green);
    {
        SDL_Rect r;
        int box[4] = {10, 20, 200, 100};
        pic_fit(s, 16, 9, 1, box, &r);
        CHECK(r.x >= 10 && r.y >= 20 && r.x + r.w <= 210 && r.y + r.h <= 120
              && (r.w == 200 || r.h == 100), "box: fitted %dx%d at %d,%d", r.w, r.h, r.x, r.y);
        CHECK(PUMP_UNTIL(v->n_frames >= 5 && s->pics[1].x == r.x && s->pics[1].y == r.y
                         && s->pics[1].w == r.w && s->pics[1].h == r.h, 2),
              "box: %dx%d at %d,%d, not %dx%d at %d,%d", s->pics[1].w, s->pics[1].h,
              s->pics[1].x, s->pics[1].y, r.w, r.h, r.x, r.y);
    }
    put(in, "\x1b_bitwall box\x1b\\");
    pump(0.05);
    put_qoi(d, 16, 9, green);
    {
        SDL_Rect r;
        pic_fit(s, 16, 9, 1, NULL, &r);
        CHECK(PUMP_UNTIL(v->n_frames >= 6 && s->pics[1].w == r.w && s->pics[1].y == r.y, 2),
              "box alone: %dx%d at %d,%d, not the glass's %dx%d at %d,%d", s->pics[1].w,
              s->pics[1].h, s->pics[1].x, s->pics[1].y, r.w, r.h, r.x, r.y);
    }
    /* A box partly off the glass is drawn where it lands, and in software
     * only that part is made; one wholly off it makes nothing. */
    put(in, "\x1b_bitwall box at=-100,-50 size=400x225\x1b\\");
    pump(0.05);
    put_qoi(d, 16, 9, green);
    CHECK(PUMP_UNTIL(v->n_frames >= 7 && s->pics[1].x == -100 && s->pics[1].y == -50, 2),
          "box off the top left: at %d,%d", s->pics[1].x, s->pics[1].y);
    CHECK(s->gpu || (s->pics[1].soft && s->pics[1].soft->w == 300 && s->pics[1].soft->h == 175
                     && s->pics[1].sx == 100 && s->pics[1].sy == 50),
          "box off the top left: made %dx%d from %d,%d",
          s->pics[1].soft ? s->pics[1].soft->w : 0, s->pics[1].soft ? s->pics[1].soft->h : 0,
          s->pics[1].sx, s->pics[1].sy);
    put(in, "\x1b_bitwall box at=60000,60000 size=4096x4096\x1b\\");
    pump(0.05);
    put_qoi(d, 16, 9, green);
    CHECK(PUMP_UNTIL(v->n_frames >= 8 && s->pics[1].x == 60000, 2), "box off the glass: at %d",
          s->pics[1].x);
    CHECK(s->gpu || !s->pics[1].soft || s->pics[1].soft->w <= 300,
          "box off the glass: made %d wide", s->pics[1].soft->w);
    put(in, "\x1b_bitwall box\x1b\\");
    put(in, "\x1b_bitwall box at=1,2\x1b\\");
    put(in, "\x1b_bitwall box size=0x5\x1b\\");
    put(in, "\x1b_bitwall box over=1\x1b\\");
    put(in, "\x1b_bitwall box size\x1b\\");
    put(in, "\x1b_bitwall box at=,5 size=10x10\x1b\\");
    put(in, "\x1b_bitwall box at=+1,2 size=10x10\x1b\\");
    put(in, "\x1b_bitwall box at=65536,0 size=10x10\x1b\\");
    pump(0.05);
    {
        const char *said = out_text(o);
        CHECK(strstr(said, "error box no size"), "a box with no size was not refused");
        CHECK(strstr(said, "error box size=0x5: bad value"), "box size=0x5 was not refused");
        CHECK(strstr(said, "error box over=1: unknown option"), "box over=1 was not refused");
        CHECK(strstr(said, "error box size: unknown option"), "box size was not refused");
        CHECK(strstr(said, "error box at=,5: bad value"), "box at=,5 was not refused");
        CHECK(strstr(said, "error box at=+1,2: bad value"), "box at=+1,2 was not refused");
        CHECK(strstr(said, "error box at=65536,0: bad value"), "box at=65536,0 was not refused");
    }
    close(d);
    CHECK(PUMP_UNTIL(!s->pics[1].on, 2), "the end of the stream left the frame up");
    CHECK(v->state == S_OPEN, "the end of the stream ended the session");
    d = data_writer(w, "v");
    put_qoi(d, 4, 4, green);
    CHECK(PUMP_UNTIL(s->pics[1].on, 2), "the next stream's frame is not up");
    close(d);
    CHECK(PUMP_UNTIL(!s->pics[1].on, 2), "the next stream's end left it up");
    put(in, "\x1b_bitwall close\x1b\\");
    pump(0.1);
    CHECK(!w->above[1], "close left the layer");
    close(in);
    close(o);
}

static void a_timed_stream_waits_for_its_start_and_drops_what_is_late(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    static const uint8_t blue[4] = {0, 0, 255, 255};
    char req[200];
    int o, in, d, i;
    Session *v;
    double start = unix_ms() + 400;
    snprintf(req, sizeof req, "open v layer z=1 type=video data=1 codec=qoi fps=10 start=%.0f",
             start);
    knock(w, req);
    o = sess_out(w, "v");
    in = sess_in(w, "v");
    put(in, "\x1b_bitwall ping\x1b\\");
    pump(0.05);
    v = w->above[1];
    d = data_writer(w, "v");
    for (i = 0; i < 3; i++)
        put_qoi(d, 4, 4, blue);
    pump(0.15);
    CHECK(!s->pics[1].on && unix_ms() < start, "a frame went up before its start");
    CHECK(PUMP_UNTIL(s->pics[1].on, 1), "the first frame did not go up at its start");
    CHECK(unix_ms() >= start - 1, "up %.0f ms early", start - unix_ms());
    CHECK(PUMP_UNTIL(v->n_frames == 3, 1), "the frames were not all shown: %ld", v->n_frames);
    CHECK(unix_ms() >= start + 200 - 1, "the third frame came before its time");
    CHECK(v->n_dropped == 0, "a frame on time was dropped");
    close(d);
    CHECK(PUMP_UNTIL(!s->pics[1].on, 2), "the end did not clear the layer");
    put(in, "\x1b_bitwall close\x1b\\");
    pump(0.05);
    close(in);
    close(o);

    /* A start long gone: every frame is late but the last of what is there. */
    snprintf(req, sizeof req, "open u layer z=2 type=video data=1 codec=qoi fps=50 start=%.0f",
             unix_ms() - 5000);
    knock(w, req);
    o = sess_out(w, "u");
    in = sess_in(w, "u");
    put(in, "\x1b_bitwall ping\x1b\\");
    pump(0.05);
    v = layer_named(w, "u");
    d = data_writer(w, "u");
    for (i = 0; i < 6; i++)
        put_qoi(d, 4, 4, blue);
    close(d);
    CHECK(PUMP_UNTIL(v->n_dropped >= 6, 2), "late frames were not dropped: %lu shown %ld",
          v->n_dropped, v->n_frames);
    CHECK(v->n_frames == 0, "a late frame was shown");
    close(in);
    close(o);
}

static void a_stream_that_does_not_decode_is_said_once(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int o, in, d;
    char *said, *first;
    knock(w, "open v layer z=1 type=video data=1 codec=qoi");
    o = sess_out(w, "v");
    in = sess_in(w, "v");
    put(in, "\x1b_bitwall ping\x1b\\");
    pump(0.05);
    d = data_writer(w, "v");
    put(d, "qoif\x00\x00\x00\x04\x00\x00\x00\x04\x04\x00 this is not a picture at all..........");
    put(d, "and nor is this");
    close(d);
    CHECK(PUMP_UNTIL(strstr(out_text(o), "error data qoi: does not decode"), 2),
          "a stream that does not decode was not said");
    pump(0.1);
    said = out_text(o);
    first = strstr(said, "error data qoi");
    CHECK(!first || !strstr(first + 1, "error data qoi"), "said twice: %.300s", said);
    CHECK(!s->pics[1].on && w->above[1]->state == S_OPEN, "the layer is up or gone");
    close(in);
    close(o);
}

/* A film of `frames` frames at w by h in `codec`, made by ffmpeg into
 * `path`; 0 when there is no ffmpeg here to make one. */
static int film_made(const char *codec, int w, int h, int frames, const char *path)
{
    char cmd[600];
    snprintf(cmd, sizeof cmd,
             "ffmpeg -hide_banner -loglevel error -y -f lavfi -i testsrc2=size=%dx%d:rate=10 "
             "-frames:v %d -pix_fmt yuv420p -c:v %s -f %s '%s' 2>/dev/null",
             w, h, frames, !strcmp(codec, "hevc") ? "libx265 -x265-params log-level=none"
                                                  : "libx264", codec, path);
    return system(cmd) == 0;
}

static void hevc_and_h264_on_data_go_up_as_yuv_through_the_gpu(void)
{
    static const char *const which[] = {"hevc", "h264"};
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int k;
    codecs_probe();
    for (k = 0; k < 2; k++) {
        char film[300], req[120], name[8];
        int o, in, d, fd, n;
        uint8_t buf[65536];
        Session *v;
        snprintf(film, sizeof film, "%s/film.%s", run_dir, which[k]);
        if (!codecs.ok[codec_of(which[k])] || !film_made(which[k], 64, 36, 12, film)) {
            fprintf(stderr, "        (no ffmpeg for %s here: not run)\n", which[k]);
            continue;
        }
        snprintf(name, sizeof name, "f%d", k);
        snprintf(req, sizeof req, "open %s layer z=above type=video data=1 codec=%s fps=24",
                 name, which[k]);
        knock(w, req);
        o = sess_out(w, name);
        in = sess_in(w, name);
        put(in, "\x1b_bitwall ping\x1b\\");
        pump(0.05);
        v = layer_named(w, name);
        CHECK(v && v->ds, "%s not opened with data", which[k]);
        d = data_writer(w, name);
        fd = open(film, O_RDONLY);
        while ((n = (int)read(fd, buf, sizeof buf)) > 0)
            CHECK(write(d, buf, (size_t)n) == n, "write failed");
        close(fd);
        close(d);
        CHECK(PUMP_UNTIL(s->pics[v->z].on, 5), "%s: no frame went up", which[k]);
        CHECK(s->pics[v->z].yuv && s->pics[v->z].plane[0] && s->pics[v->z].plane[2],
              "%s: not up as YUV planes", which[k]);
        CHECK(PUMP_UNTIL(v->n_frames + (long)v->n_dropped >= 12, 5),
              "%s: %ld shown and %lu dropped of 12", which[k], v->n_frames, v->n_dropped);
        CHECK(PUMP_UNTIL(!s->pics[v->z].on, 3), "%s: the end left it up", which[k]);
        CHECK(!strstr(out_text(o), "error data"), "%s: said an error", which[k]);
        close(in);
        close(o);
    }
}

static void hw_names_the_codecs_taken_that_the_board_decodes(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int ok[NCODECS], hw[NCODECS];
    const char *caps;
    memcpy(ok, codecs.ok, sizeof ok);
    memcpy(hw, codecs.hw, sizeof hw);
    codecs.ok[CODEC_HEVC] = codecs.hw[CODEC_HEVC] = 1;
    codecs.ok[CODEC_H264] = 1;
    codecs.hw[CODEC_H264] = 0;
    codecs.ok[CODEC_MJPEG] = 0;
    codecs.hw[CODEC_MJPEG] = 1;     /* a board that decodes it, unoffered */
    caps = caps_of(w);
    CHECK(strstr(caps, " codecs=qoi,hevc,h264 ") && strstr(caps, " hw=hevc "), "caps: %s", caps);
    {
        char said[64];
        codecs.ok[CODEC_HEVC] = codecs.hw[CODEC_HEVC] = 0;
        codecs_said(said, sizeof said, 1);
        CHECK(!said[0], "hw with nothing in hardware: %s", said);
    }
    memcpy(codecs.ok, ok, sizeof ok);
    memcpy(codecs.hw, hw, sizeof hw);
}

static void caps_says_no_hw_where_nothing_is_in_hardware(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int ok[NCODECS], hw[NCODECS];
    const char *caps;
    memcpy(ok, codecs.ok, sizeof ok);
    memcpy(hw, codecs.hw, sizeof hw);
    memset(codecs.hw, 0, sizeof codecs.hw);
    caps = caps_of(w);
    CHECK(strstr(caps, " codecs=") && !strstr(caps, " hw="), "caps: %s", caps);
    memcpy(codecs.ok, ok, sizeof ok);
    memcpy(codecs.hw, hw, sizeof hw);
}

/* The decoder's command line, joined by spaces. */
static const char *decoder_line(int codec)
{
    static char line[600];
    const char *argv[40];
    DataStream d;
    int i, n;
    size_t k = 0;
    memset(&d, 0, sizeof d);
    d.codec = codec;
    n = decoder_argv(&d, argv);
    line[0] = 0;
    for (i = 0; i < n && k < sizeof line; i++)
        k += (size_t)snprintf(line + k, sizeof line - k, " %s", argv[i]);
    CHECK(argv[n] == NULL, "argv not ended");
    return line;
}

static void mjpeg_goes_to_the_boards_decoder_and_h264_where_it_has_one(void)
{
    int hw[NCODECS];
    memcpy(hw, codecs.hw, sizeof hw);
    codecs.hw[CODEC_MJPEG] = codecs.hw[CODEC_H264] = 1;
    CHECK(strstr(decoder_line(CODEC_MJPEG), " -c:v mjpeg_v4l2m2m ")
          && strstr(decoder_line(CODEC_MJPEG), " -f mjpeg -i pipe:0 "),
          "mjpeg: %s", decoder_line(CODEC_MJPEG));
    CHECK(strstr(decoder_line(CODEC_H264), " -c:v h264_v4l2m2m ")
          && strstr(decoder_line(CODEC_H264), " -f h264 -i pipe:0 "),
          "h264: %s", decoder_line(CODEC_H264));
    codecs.hw[CODEC_H264] = 0;
    CHECK(!strstr(decoder_line(CODEC_H264), "-c:v"), "h264 in software: %s",
          decoder_line(CODEC_H264));
    memcpy(codecs.hw, hw, sizeof hw);
}

static void tidy_mjpeg_ffmpeg(void)
{
    char path[300];
    snprintf(path, sizeof path, "%s/ffmpeg-mjpeg", run_dir);
    unlink(path);
}

static void mjpeg_is_taken_only_where_the_board_decodes_it(void)
{
    char ffmpeg[300], cmd[700];
    int ok[NCODECS], hw[NCODECS];
    char was[256];
    memcpy(ok, codecs.ok, sizeof ok);
    memcpy(hw, codecs.hw, sizeof hw);
    memcpy(was, codecs.ffmpeg, sizeof was);
    a_run_dir();
    snprintf(ffmpeg, sizeof ffmpeg, "%s/ffmpeg-mjpeg", run_dir);
    snprintf(cmd, sizeof cmd,
             "printf '#!/bin/sh\necho \" V....D mjpeg_v4l2m2m  V4L2 mem2mem MJPEG decoder\"\n'"
             " > '%s' && chmod +x '%s'", ffmpeg, ffmpeg);
    CHECK(system(cmd) == 0, "could not make %s", ffmpeg);
    atexit(tidy_mjpeg_ffmpeg);
    setenv("BITWALL_FFMPEG", ffmpeg, 1);
    memset(codecs.hw, 0, sizeof codecs.hw);
    codecs_probe();
    unsetenv("BITWALL_FFMPEG");
    if (codecs.hw[CODEC_MJPEG])
        CHECK(codecs.ok[CODEC_MJPEG], "a board that decodes JPEG and ffmpeg to reach it, not taken");
    else
        CHECK(!codecs.ok[CODEC_MJPEG], "taken with no board decoder");
    memcpy(codecs.ok, ok, sizeof ok);
    memcpy(codecs.hw, hw, sizeof hw);
    memcpy(codecs.ffmpeg, was, sizeof was);
}

/* ------------------------------------------------- the software picture */

/* FNV-1a over every pixel of a surface, row by row, so that a pixel moved,
 * recoloured or lost anywhere changes it. */
static uint32_t surface_sum(const SDL_Surface *g)
{
    uint32_t h = 2166136261u;
    int x, y;
    for (y = 0; y < g->h; y++) {
        const Uint32 *row = (const Uint32 *)((const uint8_t *)g->pixels + (size_t)y * g->pitch);
        for (x = 0; x < g->w; x++) {
            Uint32 p = row[x];
            int k;
            for (k = 0; k < 4; k++) {
                h ^= (p >> (8 * k)) & 0xff;
                h *= 16777619u;
            }
        }
    }
    return h;
}

/* What the software path paints for each scene, pixel for pixel. The sums
 * were taken from a painter that drew each scene right; a change to any
 * pixel of any of them fails here, and the scene named is where to look.
 * A change meant to move pixels takes new sums, read off the failure. */
static const struct {
    const char *name, *text;
    uint32_t sum;
} SOFT_SCENES[] = {
    {"cells in colour",
     "\x1b[1;1H\x1b[38;2;255;128;0;48;2;0;64;0mHi\x1b[0;1;38;5;196mB\x1b[7mR"
     "\x1b[27;3;4;9;38;5;46;48;5;17mx\x1b[0m", 0x81433f3a},
    {"box lines",
     "\x1b[1;1H\xe2\x94\x8c\xe2\x94\x80\xe2\x94\xac\xe2\x94\x90"
     "\x1b[2;1H\xe2\x94\x82 \xe2\x95\x91\xe2\x94\x83"
     "\x1b[3;1H\xe2\x94\x94\xe2\x94\x81\xe2\x95\x8b\xe2\x94\x98\x1b[0m", 0x93e93d21},
    {"rounded corners",
     "\x1b[1;1H\x1b[38;2;0;255;255m\xe2\x95\xad\xe2\x94\x80\xe2\x95\xae"
     "\x1b[2;1H\xe2\x94\x82 \xe2\x94\x82"
     "\x1b[3;1H\xe2\x95\xb0\xe2\x94\x80\xe2\x95\xaf\x1b[0m", 0xb478f48f},
    {"braille",
     "\x1b[1;1H\x1b[38;2;255;255;0;48;2;0;0;128m\xe2\xa0\x81\xe2\xa3\xbf\xe2\xa1\x87"
     "\x1b[2m\xe2\xa2\xb8\xe2\xa0\x80\x1b[0m", 0x843b8d85},
    {"blocks and halves",
     "\x1b[1;1H\x1b[38;2;200;50;50;48;2;20;20;90m\xe2\x96\x88\xe2\x96\x80\xe2\x96\x84"
     "\xe2\x96\x8c\xe2\x96\x90\xe2\x96\x9a\xe2\x96\x91\xe2\x96\x93\xe2\x97\xa2\xe2\x97\xa5"
     "\xf0\x9f\xac\x80\xf0\x9c\xb4\x80\x1b[0m", 0x240fcc95},
};
#define NSOFT (sizeof SOFT_SCENES / sizeof SOFT_SCENES[0])

static void the_software_picture_of_each_scene_is_unchanged(void)
{
    size_t i;
    int bad = 0;
    for (i = 0; i < NSOFT; i++) {
        Screen *s = soft_screen(14);
        uint32_t got;
        write_text(s, SOFT_SCENES[i].text);
        got = surface_sum(s->glass);
        if (got != SOFT_SCENES[i].sum) {
            fprintf(stderr, "%s: sum 0x%08x, not 0x%08x\n", SOFT_SCENES[i].name,
                    (unsigned)got, (unsigned)SOFT_SCENES[i].sum);
            bad++;
        }
    }
    CHECK(!bad, "%d of %zu scenes painted differently", bad, NSOFT);
}

static void a_video_layer_over_cells_in_software_is_unchanged(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = layer_open(w, "v", &out);
    const uint8_t half[4] = {255, 64, 0, 128};
    uint32_t got, beneath;
    /* The cells come from a sender, so the status page is gone for good. */
    put(bin, SOFT_SCENES[0].text);
    send_frame(in, 40, 20, half, "at=3,4 hold=5000");
    pump(0.2);
    beneath = surface_sum(s->glass);
    CHECK(s->pics[1].on, "the frame is not up");
    pics_soft(s, 1);
    got = surface_sum(s->glass);
    pics_soft(s, 0);
    CHECK(got != beneath, "the layer is not on the glass");
    CHECK(got == 0xefe4641du, "the layer blended over the cells: sum 0x%08x", (unsigned)got);
    CHECK(surface_sum(s->glass) == beneath, "the cells beneath were not put back");
    close(bin);
    close(bout);
    close(in);
    close(out);
}

static void the_stats_overlay_in_software_is_unchanged(void)
{
    Screen *s = soft_screen(14);
    char lines[3][96] = {"wall   0", "", "base               0 cells      20fps    1.2k/s 0 dropped"};
    uint32_t got;
    s->st.on = 1;
    s->st.size = 14;
    s->st.at = POINT_NE;
    stats_render(s, lines, 3);
    CHECK(s->st.soft && s->st.box.w > 0 && s->st.box.h > 0, "no overlay was made");
    CHECK(s->st.box.x + s->st.box.w == s->width - STATS_INSET && s->st.box.y == STATS_INSET,
          "the overlay is not in the north east: %d,%d %dx%d", s->st.box.x, s->st.box.y,
          s->st.box.w, s->st.box.h);
    got = surface_sum(s->st.soft);
    CHECK(got == 0x2a6eedcfu, "the overlay's pixels: sum 0x%08x", (unsigned)got);
}

/* ------------------------------------------------------ stats and want */

static void stats_turns_on_and_off_and_says_so(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out);
    Uint32 before, *g;
    char *said;
    put(bin, "\x1b[1;1HA\x1b[0m");
    pump(0.1);
    out_text(out);
    put(in, "\x1b_bitwall stats 1 at=nw size=12 show=wall,grid id=a\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(strstr(said, "stats 1 at=nw size=12 show=wall,grid id=a\n"), "not said: %.300s", said);
    CHECK(s->st.on && stats_shown(s), "the overlay is not up");
    CHECK(s->st.soft && s->st.box.x == STATS_INSET && s->st.box.y == STATS_INSET,
          "not in the north west: %d,%d", s->st.box.x, s->st.box.y);
    /* It goes on the glass only while it is pushed out: what is painted
     * next is painted onto the picture alone. */
    g = (Uint32 *)((uint8_t *)s->glass->pixels + (size_t)(STATS_INSET + 1) * s->glass->pitch);
    before = g[STATS_INSET + 1];
    show(s);
    CHECK(g[STATS_INSET + 1] == before, "the overlay was left on the picture");
    CHECK(s->st.was.w == s->st.box.w, "the overlay was not put up");
    put(in, "\x1b_bitwall stats 2\x1b\\");
    put(in, "\x1b_bitwall stats 1 at=up size=x show=nothing bogus=1\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(strstr(said, "error stats 2: not 0 or 1\n"), "a bad state: %.300s", said);
    CHECK(strstr(said, "error stats at=up: unknown point\n")
          && strstr(said, "error stats size=x: not a number\n")
          && strstr(said, "error stats show=nothing: nothing to show\n")
          && strstr(said, "error stats bogus=1: unknown option\n"), "bad options: %.400s", said);
    CHECK(s->st.at == 7 && s->st.size == 12, "a bad option changed the overlay");
    put(in, "\x1b_bitwall stats 0\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(strstr(said, "stats 0 at=nw size=12 show=wall,grid\n"), "off not said: %.300s", said);
    CHECK(!s->st.on && !stats_shown(s), "the overlay is still up");
    show(s);
    CHECK(s->st.was.w == 0, "the overlay was put up while off");
    close(bin);
    close(bout);
    close(in);
    close(out);
}

static void stats_is_not_drawn_over_the_status_page(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out);
    put(in, "\x1b_bitwall stats 1\x1b\\");
    pump(0.1);
    CHECK(s->st.on && !stats_shown(s), "the overlay is up with no sender");
    close(in);
    close(out);
}

static void want_takes_only_well_formed_inputs(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    out_text(out);
    put(in, "\x1b_bitwall want touch=1 keyboard=0 buttons=1\x1b\\");
    put(in, "\x1b_bitwall want mouse=1 touch=yes tap\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(!strstr(said, "error want touch=1") && !strstr(said, "error want keyboard=0")
          && !strstr(said, "error want buttons=1"), "a good want refused: %.300s", said);
    CHECK(strstr(said, "error want mouse=1: unknown option\n")
          && strstr(said, "error want touch=yes: not 0 or 1\n")
          && strstr(said, "error want tap: unknown option\n"), "bad wants: %.300s", said);
    close(in);
    close(out);
}

/* ----------------------------------------------- atlas restart, and meta */

static void an_atlas_restart_puts_every_cell_back_once_it_pays(void)
{
    Screen *s = gpu_screen(14);
    long before = s->restarts;
    write_text(s, "\x1b[1;1HAB\x1b[2;3H\xf0\x9f\x98\x80\x1b[0m");
    /* Too few new masks to pay for putting the grid back: cleared only. */
    s->gl.made = 0;
    atlas_restart(s);
    CHECK(s->restarts == before + 1 && s->gl.made == 0, "not counted");
    /* Enough: every cell is put back, a wide one with its tail. */
    s->gl.made = s->cols * s->rows / 8;
    atlas_restart(s);
    CHECK(s->restarts == before + 2 && s->gl.made == 0 && !s->gl.rebuilding, "not settled");
    CHECK(shown(s, 0, 0) == 'A' && shown(s, 1, 0) == 'B', "the cells lost their glyphs: %04X %04X",
          shown(s, 0, 0), shown(s, 1, 0));
    CHECK(shown(s, 2, 1) == 0x1F600 && shown(s, 3, 1) == (0x1F600 | TAIL),
          "a wide glyph lost itself or its tail: %04X %04X", shown(s, 2, 1), shown(s, 3, 1));
}

static void a_layers_meta_goes_when_its_session_closes(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = layer_open(w, "v", &out), cout, cin;
    char *said;
    put(in, "\x1b_bitwall meta set name Fireflies\x1b\\");
    pump(0.1);
    said = out_text(out);
    CHECK(strstr(said, "meta set name Fireflies\n"), "meta not taken: %.300s", said);
    cin = ctl_open(w, "c", &cout);
    put(cin, "\x1b_bitwall report\x1b\\");
    pump(0.1);
    said = out_text(cout);
    CHECK(strstr(said, "meta v set name Fireflies\n"), "the layer's meta not reported: %.400s",
          said);
    close(in);
    pump(0.3);
    said = out_text(out);
    CHECK(strstr(said, "closed v eof\n"), "eof not said: %.200s", said);
    out_text(cout);
    put(cin, "\x1b_bitwall report\x1b\\");
    pump(0.1);
    said = out_text(cout);
    CHECK(strstr(said, "hello v1 line\n"), "no report: %.200s", said);
    CHECK(!strstr(said, "meta v "), "the closed layer's meta is still reported: %.400s", said);
    close(out);
    close(cin);
    close(cout);
}

static void nothing_after_a_base_ends_itself_is_painted(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = base_open(w, &out);
    char *said;
    put(in, "\x1b[1;1HA\x1b[0m");
    pump(0.1);
    CHECK(shown(s, 0, 0) == 'A', "the base's frame did not reach the glass");
    /* Its detach and more of its stream after it, in one write. */
    put(in, "\x1b[1;2HB\x1b_bitwall detach\x1b\\\x1b[1;3HC\x1b[0m\x1b[1;4HD");
    pump(0.2);
    said = out_text(out);
    CHECK(strstr(said, "closed base asked\n"), "detach not said: %.200s", said);
    CHECK(!w->base && s->len == 0, "the ended stream is still held: %zu bytes", s->len);
    CHECK(shown(s, 2, 0) != 'C' && shown(s, 3, 0) != 'D',
          "what followed the detach was painted: %04X %04X", shown(s, 2, 0), shown(s, 3, 0));
    close(in);
    close(out);
}

/* The main loop stops a session's readers when it ends, and waits for
 * them: each waits on its pipe with a long select, and is rung out of it
 * rather than left to time out. Not rung, each would hold the loop for up
 * to its whole wait. */
static void a_base_ending_holds_up_the_loop_for_no_time(void)
{
    static Inbox in;
    int fds[2];
    char dir[600];
    DataStream *d;
    double t0, reader, feeder;
    CHECK(pipe(fds) == 0, "pipe: %s", strerror(errno));
    CHECK(inbox_start(&in, fds[0]) == 0, "inbox_start failed");
    SDL_Delay(50);          /* the reader is waiting on a quiet pipe */
    t0 = now_ms();
    inbox_stop(&in);
    reader = now_ms() - t0;
    close(fds[1]);
    a_run_dir();
    snprintf(dir, sizeof dir, "%s/bitwall", run_dir);
    CHECK(mkdir(dir, 0700) == 0, "mkdir %s: %s", dir, strerror(errno));
    d = stream_start(dir, CODEC_QOI, 20, 0);
    CHECK(d, "stream_start failed");
    SDL_Delay(50);          /* the feeder is waiting on a `data` nobody writes */
    t0 = now_ms();
    stream_stop(d);
    feeder = now_ms() - t0;
    rmrf(dir);
    CHECK(reader < 250 && feeder < 250, "the loop was held %.1f ms by a reader, %.1f ms "
          "by a stream's feeder", reader, feeder);
}

static void a_stale_wall_is_taken_one_claim_at_a_time(void)
{
    Screen *s1 = gpu_screen(14), *s2 = gpu_screen(14);
    Wall *a = wall_at(s1, "0"), *b = calloc(1, sizeof *b);
    char lock[700], door[800];
    int fd;
    pid_t pid;
    int status = 0;
    CHECK(b, "out of memory");
    b->in.fd = -1;
    /* A live wall is refused. */
    CHECK(wall_claim(b, s2, run_dir, "0") < 0, "a live wall was taken");
    /* One left by a killed receiver is taken, and taken whole: its door
     * is the new one, read by the new wall. */
    close(a->door_fd);
    a->door_fd = -1;
    snprintf(door, sizeof door, "%s/door.in", a->dir);
    CHECK(!door_has_reader(door), "the wall is not stale");
    /* While another claim holds the name's lock, a claim waits for it. */
    snprintf(lock, sizeof lock, "%s/bitwall-0.claim", run_dir);
    fd = open(lock, O_RDWR);
    CHECK(fd >= 0 && file_lock(fd, F_WRLCK) == 0, "the claim lock was not made");
    pid = fork();
    if (pid == 0) {
        alarm(5);
        _exit(wall_claim(b, s2, run_dir, "0") == 0 ? 0 : 1);
    }
    SDL_Delay(200);
    CHECK(waitpid(pid, &status, WNOHANG) == 0, "a claim did not wait for the lock");
    CHECK(!door_has_reader(door), "a claim went ahead under another's lock");
    file_lock(fd, F_UNLCK);
    close(fd);
    CHECK(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
          "the stale wall was not taken once the lock was let go");
}

static void a_stuck_out_waits_for_room_rather_than_spinning(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), i;
    Session *c = w->sessions;
    while (c && strcmp(c->name, "c"))
        c = c->next;
    CHECK(c, "no session c");
    for (i = 0; i < 400 && !c->stuck; i++) {
        put(in, "\x1b_bitwall report id=r\x1b\\");
        pump(0.002);
    }
    CHECK(c->qhead && c->stuck, "the out never filled");
    CHECK(wall_wait(w, 20) == 20, "a stuck out shortens the loop's wait to %d ms",
          wall_wait(w, 20));
    out_text(out);
    flush_out(c);
    CHECK(!c->stuck, "room on the out was not taken");
    CHECK(!c->qhead || wall_wait(w, 20) == 5, "lines still waiting are not sent on soon");
    close(in);
    close(out);
}

/* ---------------------------------------------------------------- kitty */

/* 2x2 RGBA, opaque red, as kitty sends it. */
#define KITTY_RED "/wAA//8AAP//AAD//wAA/w=="

/* On the GPU an image is uploaded once, as a texture of its own pixels; a
 * placement is one more draw over the grid, and one below the text is two,
 * the text drawn again over it; the session's end lets the texture go. */
static void a_kitty_picture_is_a_texture_drawn_over_its_cells(void)
{
    Screen *s = gpu_screen(14);
    FakeContext *c = fake.current;
    const KImage *im;
    const FakeTexture *t;
    SDL_Rect box;
    int draws, grid, cols, rows;
    draws = c->draws;
    show(s);
    grid = c->draws - draws;
    write_text(s, "\x1b[2;3H\x1b_Ga=T,f=32,s=2,v=2,i=9,c=4,r=2,q=2;" KITTY_RED "\x1b\\");
    im = &s->kitty.img[kitty_find(s, 9)];
    CHECK(kitty_find(s, 9) >= 0 && im->tex, "no texture made");
    t = fake_texture(im->tex);
    CHECK(t->w == 2 && t->h == 2 && t->bpp == 4, "texture %dx%d", t->w, t->h);
    CHECK(t->pixels[0] == 255 && t->pixels[1] == 0 && t->pixels[3] == 255,
          "texture holds %d,%d,%d,%d", t->pixels[0], t->pixels[1], t->pixels[2], t->pixels[3]);
    CHECK(s->kitty.place[0].used && s->kitty.place[0].col == 2 && s->kitty.place[0].row == 1,
          "placed at %d,%d", s->kitty.place[0].col, s->kitty.place[0].row);
    kitty_box(s, &s->kitty.place[0], &box, &cols, &rows);
    CHECK(box.x == s->ox + 2 * s->cw && box.y == s->oy + s->ch && box.w == 4 * s->cw
          && box.h == 2 * s->ch && cols == 4 && rows == 2, "box %d,%d %dx%d", box.x, box.y,
          box.w, box.h);
    CHECK(s->x == 6 && s->y == 2, "the cursor went to %d,%d", s->x, s->y);
    draws = c->draws;
    show(s);
    CHECK(c->draws - draws == grid + 1, "%d draws for the grid and one placement",
          c->draws - draws);
    write_text(s, "\x1b_Ga=p,i=9,z=-1,C=1,q=2\x1b\\");
    draws = c->draws;
    show(s);
    CHECK(c->draws - draws == 2 * grid + 2, "%d draws with one placement below the text",
          c->draws - draws);
    CHECK(fake.tex_deleted == 0, "a texture went early");
    kitty_clear(s);
    CHECK(fake.tex_deleted == 1 && !s->kitty.decoded, "%d textures let go, %zu bytes kept",
          fake.tex_deleted, s->kitty.decoded);
    draws = c->draws;
    show(s);
    CHECK(c->draws - draws == grid, "%d draws with nothing placed", c->draws - draws);
}

/* A kitty string longer than a string is held is let go to its end, and
 * none of it is painted as text. */
static void an_overlong_kitty_string_is_let_go_whole(void)
{
    Screen *s = gpu_screen(14);
    size_t n = KITTY_STRING + 4096, i;
    char *big = malloc(n + 64);
    CHECK(big, "out of memory");
    strcpy(big, "\x1b[1;1H\x1b_Ga=T,f=32,s=2,v=2,i=1;");
    for (i = strlen(big); i < n; i++)
        big[i] = 'A';
    memcpy(big + n, "\x1b\\Z\x1b[0m", 7);
    send_split(s, (const uint8_t *)big, n + 7, 7, 200000);
    CHECK(!s->kitty.skip && s->len == 0, "still skipping, %zu bytes held", s->len);
    CHECK(shown(s, 0, 0) == 'Z', "cell 0 shows U+%04X", shown(s, 0, 0));
    CHECK(kitty_find(s, 1) < 0, "an overlong image was kept");
    free(big);
}

/* A kitty string past the limit is refused though it is whole by the time
 * the painter reads its end: a picture of 300x350 RGBA, 560 KB of base64,
 * the last piece bringing both the crossing and the end. */
static void a_whole_overlong_kitty_string_is_refused(void)
{
    Screen *s = gpu_screen(14);
    static const char b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const char *head = "\x1b[1;1H\x1b_Ga=T,f=32,s=300,v=350,i=1,q=2;";
    size_t raw = 300 * 350 * 4, n64 = raw / 3 * 4, h = strlen(head), i;
    size_t n = h + n64 + 7;
    char *big = malloc(n);
    CHECK(big && n64 > KITTY_STRING, "out of memory, or not past the limit");
    memcpy(big, head, h);
    for (i = 0; i < n64; i++)
        big[h + i] = b64[i % 64];
    memcpy(big + h + n64, "\x1b\\Z\x1b[0m", 7);
    send_bytes(s, (const uint8_t *)big, 500000);
    send_bytes(s, (const uint8_t *)big + 500000, n - 500000);
    CHECK(kitty_find(s, 1) < 0, "a string of %zu bytes was kept", n64);
    CHECK(shown(s, 0, 0) == 'Z', "cell 0 shows U+%04X", shown(s, 0, 0));
    free(big);
}

/* A PNG is decoded by libpng to RGBA; one cut short is refused and kept
 * nowhere. In software the picture stays in memory, for scaling. */
static void a_kitty_png_decodes_and_a_cut_one_does_not(void)
{
    Screen *s = soft_screen(14);
    const KImage *im;
    write_text(s, "\x1b_Ga=t,f=100,i=3,q=2;iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAEUl"
                  "EQVR4nGNkOMEAAUwMMAAADkwAzJ2EfDUAAAAASUVORK5CYII=\x1b\\");
    CHECK(kitty_find(s, 3) >= 0, "the PNG was not kept");
    im = &s->kitty.img[kitty_find(s, 3)];
    CHECK(im->w == 3 && im->h == 2 && im->rgba, "decoded %dx%d", im->w, im->h);
    CHECK(im->rgba[5 * 4] == 0 && im->rgba[5 * 4 + 1] == 200 && im->rgba[5 * 4 + 3] == 255,
          "the last pixel is %d,%d,%d,%d", im->rgba[20], im->rgba[21], im->rgba[22],
          im->rgba[23]);
    CHECK(s->kitty.decoded == 24, "%zu bytes counted", s->kitty.decoded);
    write_text(s, "\x1b_Ga=t,f=100,i=4,q=2;iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAEUl"
                  "EQVR4nGNkOMEAAUwMMAAA\x1b\\");
    CHECK(kitty_find(s, 4) < 0 && s->kitty.decoded == 24, "a cut PNG was kept");
}

/* -------------------------------------------------- the glass, streamed
 *
 * The fake GPU runs no shaders and reads back black, or a pattern of its
 * own under read_pattern: so on the GPU path these see the command, the
 * fifo, the frames' header, sizes and cadence, the hand-off and the drops,
 * which framebuffer each draw went to and what was read back, but not the
 * colour the shader makes. The colour is seen on the old path, in
 * software, where the glass is real pixels. */

/* The wall's first stream, or none. */
static Raw *first_stream(Screen *s)
{
    return s->outs && s->outs->n ? s->outs->streams[0] : NULL;
}

static void ask(int in, const char *cmd)
{
    char apc[400];
    snprintf(apc, sizeof apc, "\x1b_bitwall %s\x1b\\", cmd);
    put(in, apc);
    pump(0.05);
}

static void glass_fifo(Wall *w, const char *name, char *out, size_t max)
{
    snprintf(out, max, "%s/s/%s/glass", w->dir, name);
}

/* What a frame's header says. */
typedef struct {
    uint32_t w, h, len;
    uint64_t n, us;
} Head;

static uint64_t le_of(const uint8_t *p, int n)
{
    uint64_t v = 0;
    while (n--)
        v = v << 8 | p[n];
    return v;
}

/* Frames read off a glass fifo for `secs`, the wall pumped meanwhile: how
 * many, the headers of the first `most`, and the last whole frame. Each
 * must start where the last ended, with its magic. */
static int read_glass(int fd, double secs, Head *heads, int most, uint8_t **last)
{
    size_t cap = 1 << 22, have = 0;
    uint8_t *b = malloc(cap);
    double t0 = now_ms();
    int got = 0;
    CHECK(b, "out of memory");
    *last = NULL;
    do {
        ssize_t k;
        pump(0.005);
        while ((k = read(fd, b + have, cap - have)) > 0) {
            have += (size_t)k;
            if (have == cap) {
                cap *= 2;
                b = realloc(b, cap);
                CHECK(b, "out of memory");
            }
        }
        while (have >= RAW_HEADER) {
            size_t len;
            CHECK(!memcmp(b, "BWG1", 4), "frame %d does not start with BWG1", got);
            len = RAW_HEADER + (size_t)le_of(b + 28, 4);
            if (have < len)
                break;
            if (got < most) {
                heads[got].w = (uint32_t)le_of(b + 4, 4);
                heads[got].h = (uint32_t)le_of(b + 8, 4);
                heads[got].n = le_of(b + 12, 8);
                heads[got].us = le_of(b + 20, 8);
                heads[got].len = (uint32_t)le_of(b + 28, 4);
            }
            got++;
            free(*last);
            *last = malloc(len);
            CHECK(*last, "out of memory");
            memcpy(*last, b, len);
            memmove(b, b + len, have - len);
            have -= len;
        }
    } while (now_ms() - t0 < secs * 1e3);
    free(b);
    return got;
}

static Session *session_named(Wall *w, const char *name)
{
    Session *x;
    for (x = w->sessions; x && strcmp(x->name, name); x = x->next)
        ;
    CHECK(x, "no session %s", name);
    return x;
}

static void a_glass_stream_is_refused_what_it_cannot_take(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out);
    int out2, in2 = ctl_open(w, "c2", &out2);
    static const char *const bad[][2] = {
        {"mirror base stream=i420 size=64x36 fps=10",
         "error mirror base stream=i420: only for the glass id=e"},
        {"mirror glass stream=nv12 size=64x36 fps=10", "error mirror stream=nv12: not i420 id=e"},
        {"mirror glass stream=i420 size=63x36 fps=10", "error mirror size=63x36: bad size id=e"},
        {"mirror glass stream=i420 size=8x8 fps=10", "error mirror size=8x8: bad size id=e"},
        {"mirror glass stream=i420 size=big fps=10", "error mirror size=big: bad size id=e"},
        {"mirror glass stream=i420 size=64x2000 fps=10",
         "error mirror size=64x2000: bad size id=e"},
        {"mirror glass stream=i420 size=64x36 fps=0", "error mirror fps=0: not 1 to 60 id=e"},
        {"mirror glass stream=i420 size=64x36 fps=61", "error mirror fps=61: not 1 to 60 id=e"},
        {"mirror glass stream=i420 size=64x36 fps=soon", "error mirror fps=soon: not a number id=e"},
        {"mirror glass stream=i420 fps=10", "error mirror stream=i420: no size id=e"},
        {"mirror glass stream=i420 size=64x36", "error mirror stream=i420: no fps id=e"},
        {"mirror glass stream=i420 size=64x36 fps=10 every=100",
         "error mirror every=100: unknown option id=e"},
    };
    char path[800], cmd[200];
    size_t i;
    out_text(out);
    for (i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        const char *said;
        snprintf(cmd, sizeof cmd, "%s id=e", bad[i][0]);
        ask(in, cmd);
        said = out_text(out);
        CHECK(strstr(said, bad[i][1]), "%s: heard %s", bad[i][0], said);
    }
    CHECK(!first_stream(s), "a refused stream was started");
    out_text(bout);
    ask(bin, "mirror glass stream=i420 size=64x36 fps=10");
    CHECK(strstr(out_text(bout), "error mirror not-for-role"), "a layer was given a stream");
    CHECK(!first_stream(s), "a layer's stream was started");

    ask(in, "mirror glass stream=i420 size=64x36 fps=10 id=g");
    glass_fifo(w, "c", path, sizeof path);
    CHECK(first_stream(s) && is_fifo(path), "the stream did not start");
    CHECK(!strstr(out_text(out), "error"), "a good request was refused");
    ask(in, "mirror glass stream=i420 size=32x18 fps=5 id=b");
    CHECK(strstr(out_text(out), "error mirror busy id=b"), "a second stream was not busy");
    CHECK(first_stream(s)->w == 64 && first_stream(s)->owner == session_named(w, "c") && s->outs->n == 1,
          "the first stream was disturbed");
    out_text(out2);
    ask(in2, "mirror glass stream=i420 size=32x18 fps=5 id=b");
    CHECK(!strstr(out_text(out2), "error") && s->outs->n == 2,
          "another controller's stream was refused");
    CHECK(strstr(caps_of(w), " stream "), "caps does not say stream: %s", caps_of(w));
    close(in);
    close(out);
    close(in2);
    close(out2);
    close(bin);
    close(bout);
}

static void a_glass_stream_is_not_offered_in_software(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out);
    out_text(out);
    ask(in, "mirror glass stream=i420 size=64x36 fps=10");
    CHECK(strstr(out_text(out), "error mirror glass stream=i420: not offered"),
          "software offered a stream");
    CHECK(!first_stream(s), "a stream was started in software");
    CHECK(!strstr(caps_of(w), " stream "), "caps says stream in software: %s", caps_of(w));
    close(in);
    close(out);
}

/* The planes are where they are read back from: Y a row of texels each,
 * U and V side by side below, a frame whose sides are not a multiple of
 * eight trimmed from texels that run past it. */
static void a_glass_stream_writes_whole_frames_to_its_fifo(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), fd, got, i, x, y;
    Head h[64];
    uint8_t *last, *Y, *U, *V;
    char path[800];
    fake.read_pattern = 1;
    ask(in, "mirror glass stream=i420 size=36x20 fps=50");
    glass_fifo(w, "c", path, sizeof path);
    CHECK(is_fifo(path), "no fifo at %s", path);
    fd = open(path, O_RDONLY | O_NONBLOCK);
    CHECK(fd >= 0, "cannot open %s", path);
    got = read_glass(fd, 0.3, h, 64, &last);
    CHECK(got >= 5, "%d frames in 0.3s at 50fps", got);
    for (i = 0; i < got && i < 64; i++) {
        CHECK(h[i].w == 36 && h[i].h == 20, "frame %d is %ux%u", i, h[i].w, h[i].h);
        CHECK(h[i].len == 36 * 20 + 2 * 18 * 10, "frame %d says %u bytes", i, h[i].len);
        CHECK(!i || h[i].n > h[i - 1].n, "frame %d's number %llu after %llu", i,
              (unsigned long long)h[i].n, (unsigned long long)h[i - 1].n);
    }
    CHECK(first_stream(s)->tw == 10 && first_stream(s)->th == 30, "packed %dx%d",
          first_stream(s)->tw, first_stream(s)->th);
    Y = last + RAW_HEADER;
    U = Y + 36 * 20;
    V = U + 18 * 10;
    for (y = 0; y < 20; y++)
        for (x = 0; x < 36; x++)
            CHECK(Y[y * 36 + x] == read_byte(x, y), "Y %d,%d is %d", x, y, Y[y * 36 + x]);
    for (y = 0; y < 10; y++)
        for (x = 0; x < 18; x++) {
            CHECK(U[y * 18 + x] == read_byte(x, 20 + y), "U %d,%d is %d", x, y, U[y * 18 + x]);
            CHECK(V[y * 18 + x] == read_byte(20 + x, 20 + y), "V %d,%d is %d", x, y,
                  V[y * 18 + x]);
        }
    free(last);
    close(fd);
    close(in);
    close(out);
}

/* A frame every 1/fps, numbered from 0 and stamped with when it was drawn;
 * each read back a byte and a half a pixel, never four. */
static void a_glass_stream_comes_at_its_rate_and_its_numbers_and_times_rise(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), fd, got, i;
    Head h[64];
    uint8_t *last;
    char path[800], line[MAX_APC];
    double step;
    uint64_t before = unix_us();
    glass_fifo(w, "c", path, sizeof path);
    ask(in, "mirror glass stream=i420 size=64x36 fps=20");
    fd = open(path, O_RDONLY | O_NONBLOCK);
    CHECK(fd >= 0, "cannot open %s", path);
    got = read_glass(fd, 1.0, h, 64, &last);
    CHECK(got >= 15 && got <= 22, "%d frames in a second at 20fps", got);
    CHECK(h[0].us >= before && h[0].us <= unix_us(), "frame %llu's time is not now",
          (unsigned long long)h[0].n);
    for (i = 1; i < got; i++) {
        CHECK(h[i].n == h[i - 1].n + 1, "frame %llu after %llu with a reader keeping up",
              (unsigned long long)h[i].n, (unsigned long long)h[i - 1].n);
        CHECK(h[i].us > h[i - 1].us, "frame %d's time went back", i);
    }
    step = (double)(h[got - 1].us - h[0].us) / (got - 1) / 1e3;
    CHECK(step > 40 && step < 60, "frames %.1f ms apart at 20fps", step);
    CHECK(fake.reads >= got && fake.read_bytes == (long)fake.reads * 64 * 54,
          "%d reads of %ld bytes", fake.reads, fake.read_bytes);
    session_measure(session_named(w, "c"), now_ms(), line, sizeof line);
    CHECK(strstr(line, " streamed=") && strstr(line, " stream_dropped="),
          "the measure line says nothing of the stream: %s", line);
    free(last);
    close(fd);
    close(in);
    close(out);
}

/* A reader that is slow, or not there, has frames let go and counted; what
 * it does read is whole frames, the newest. */
static void a_slow_reader_has_frames_dropped_and_counted(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), fd, got, i, gaps = 0;
    Head h[64];
    uint8_t *last;
    char path[800], line[MAX_APC], want[64];
    unsigned long dropped;
    glass_fifo(w, "c", path, sizeof path);
    /* A Mac's fifo takes a few kilobytes between two of this reader's
     * looks, so the frames are small enough for several to come whole in
     * the half second it reads. */
    ask(in, "mirror glass stream=i420 size=320x180 fps=60");
    pump(0.3);
    CHECK(first_stream(s)->streamed == 0 && first_stream(s)->dropped >= 10,
          "with nobody reading: %lu written, %lu dropped", first_stream(s)->streamed,
          first_stream(s)->dropped);
    fd = open(path, O_RDONLY | O_NONBLOCK);
    CHECK(fd >= 0, "cannot open %s", path);
    pump(0.5);                      /* opened, and not read */
    dropped = first_stream(s)->dropped;
    CHECK(dropped >= 30, "only %lu dropped for a reader not reading", dropped);
    got = read_glass(fd, 0.5, h, 64, &last);
    CHECK(got >= 2, "%d frames once it read", got);
    for (i = 1; i < got && i < 64; i++)
        gaps += h[i].n != h[i - 1].n + 1;
    CHECK(gaps || h[0].n > 30, "the frames read have no gaps where frames were dropped");
    session_measure(session_named(w, "c"), now_ms(), line, sizeof line);
    snprintf(want, sizeof want, " stream_dropped=%lu", first_stream(s)->dropped);
    CHECK(strstr(line, want), "the measure line does not count the drops: %s", line);
    free(last);
    close(fd);
    close(in);
    close(out);
}

static void a_glass_stream_ends_with_off_or_its_session_and_leaves_nothing(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), out2, in2 = ctl_open(w, "c2", &out2);
    int fbs = fake.framebuffers, threads = fake.threads, deleted = fake.tex_deleted;
    char path[800];
    glass_fifo(w, "c", path, sizeof path);
    ask(in, "mirror glass stream=i420 size=64x36 fps=30");
    CHECK(first_stream(s) && is_fifo(path), "no stream");
    CHECK(fake.threads == threads + 1 && fake.framebuffers > fbs, "%d threads, %d framebuffers",
          fake.threads - threads, fake.framebuffers - fbs);
    ask(in2, "mirror glass off");
    CHECK(first_stream(s), "another controller's off stopped it");
    ask(in, "mirror glass off");
    CHECK(!first_stream(s) && !is_fifo(path), "off left the stream");
    CHECK(fake.framebuffers == fbs && fake.tex_deleted >= deleted + 2,
          "off left %d framebuffers, let go %d textures", fake.framebuffers - fbs,
          fake.tex_deleted - deleted);
    ask(in, "mirror glass stream=i420 size=64x36 fps=30");
    CHECK(first_stream(s), "a stream asked again did not start");
    close(in);
    pump(0.2);
    CHECK(!first_stream(s) && !is_fifo(path), "the session's end left its stream");
    CHECK(fake.framebuffers == fbs, "the session's end left framebuffers");
    close(out);
    close(in2);
    close(out2);
}

/* With no stream the screen is drawn straight into its back buffer, as it
 * always was, and nothing of a stream's is made. */
static void with_no_stream_the_glass_is_drawn_straight_and_nothing_is_made(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), programs = fake.nprograms;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60mhello\x1b[0m");
    pump(0.1);
    CHECK(s->painted > 0, "nothing painted");
    CHECK(!first_stream(s) && fake.framebuffers == 0 && fake.fb_draws == 0 && fake.reads == 0,
          "a stream's things were made: %d framebuffers, %d draws into them",
          fake.framebuffers, fake.fb_draws);
    CHECK(fake.window_draws > 0 && fake.nprograms == programs, "not drawn as before");
    close(bin);
    close(bout);
}

/* While streamed, each frame put up is the scene drawn into its own
 * framebuffer and one quad onto the screen, cleared first. */
static void while_streamed_the_glass_is_drawn_once_and_copied(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out);
    int windows, fbs, swaps, uncleared, reads;
    ask(in, "mirror glass stream=i420 size=64x36 fps=1");
    pump(0.05);
    windows = fake.window_draws;
    fbs = fake.fb_draws;
    swaps = fake.swaps;
    uncleared = fake.swaps_uncleared;
    reads = fake.reads;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60mhello\x1b[0m");
    pump(0.1);
    put(bin, "\x1b[2;1Hthere\x1b[0m");
    pump(0.1);
    CHECK(fake.swaps - swaps >= 2, "%d frames put up", fake.swaps - swaps);
    CHECK(fake.window_draws - windows == fake.swaps - swaps,
          "%d draws onto the screen for %d frames", fake.window_draws - windows,
          fake.swaps - swaps);
    CHECK(fake.fb_draws > fbs && fake.swaps_uncleared == uncleared && fake.framebuffer == 0,
          "the scene was not drawn into its own and the screen cleared");
    CHECK(fake.reads == reads, "a frame put up was read back");
    ask(in, "mirror glass off");
    fbs = fake.fb_draws;
    put(bin, "\x1b[3;1Hagain\x1b[0m");
    pump(0.1);
    CHECK(fake.fb_draws == fbs, "drawn into a framebuffer after the stream ended");
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* BITWALL_STREAM_DRAW_TWICE: the screen drawn as it always was, and each
 * frame the glass drawn again and read as a picture is, in RGBA. */
static void the_old_path_draws_the_glass_again_for_each_frame(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out), fd, got;
    int windows, swaps;
    Head h[64];
    uint8_t *last;
    char path[800];
    setenv("BITWALL_STREAM_DRAW_TWICE", "1", 1);
    glass_fifo(w, "c", path, sizeof path);
    ask(in, "mirror glass stream=i420 size=64x36 fps=20");
    CHECK(s->outs && s->outs->twice && !s->outs->copy_prog, "not the old path");
    windows = fake.window_draws;
    swaps = fake.swaps;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60mhello\x1b[0m");
    fd = open(path, O_RDONLY | O_NONBLOCK);
    got = read_glass(fd, 0.4, h, 64, &last);
    CHECK(got >= 4, "%d frames", got);
    CHECK(h[got - 1].len == 64 * 36 * 3 / 2, "%u bytes", h[got - 1].len);
    CHECK(fake.read_bytes == (long)fake.reads * 64 * 36 * 4, "not read as RGBA");
    CHECK(fake.swaps > swaps && fake.window_draws > windows, "not drawn on the screen");
    {
        const uint8_t *p = last + RAW_HEADER;
        CHECK(p[0] == 16 && p[64 * 36] == 128 && p[64 * 36 + 32 * 18] == 128,
              "black is not 16,128,128: %d,%d,%d", p[0], p[64 * 36], p[64 * 36 + 32 * 18]);
    }
    free(last);
    close(fd);
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* On GLES 3 a frame is read into a pixel buffer and collected at the next,
 * so the loop never waits for the GPU; its number and time are its own. */
static void on_gles3_a_frame_is_read_into_a_buffer_and_collected_at_the_next(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), fd, got, i;
    Head h[64];
    uint8_t *last;
    char path[800];
    fake.es3 = 1;
    fake.read_pattern = 1;
    glass_fifo(w, "c", path, sizeof path);
    ask(in, "mirror glass stream=i420 size=64x36 fps=10");
    fd = open(path, O_RDONLY | O_NONBLOCK);
    CHECK(first_stream(s)->es3 && first_stream(s)->pending && !first_stream(s)->packed,
          "not reading into a pixel buffer");
    CHECK(fake.read_into_buffer == 1 && fake.maps == 0, "%d read, %d collected",
          fake.read_into_buffer, fake.maps);
    got = read_glass(fd, 0.45, h, 64, &last);
    CHECK(got >= 3 && h[0].n == 0, "%d frames, the first %llu", got,
          got ? (unsigned long long)h[0].n : 0ull);
    for (i = 1; i < got; i++)
        CHECK(h[i].n == h[i - 1].n + 1 && h[i].us - h[i - 1].us > 80000,
              "frame %llu: %llu us after the last", (unsigned long long)h[i].n,
              (unsigned long long)(h[i].us - h[i - 1].us));
    CHECK(fake.reads == fake.read_into_buffer && fake.maps >= got, "read without the buffer");
    CHECK(last[RAW_HEADER + 5] == read_byte(5, 0)
          && last[RAW_HEADER + 64 * 36 + 32 * 18 + 3] == read_byte(32 + 3, 36),
          "the planes are not what was read");
    free(last);
    close(fd);
    close(in);
    close(out);
}

static void a_dark_wall_is_streamed_as_though_it_were_lit(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), fd, got, fbs, since = 0, i;
    uint64_t first;
    Head h[64];
    uint8_t *last;
    char path[800];
    ask(in, "blank 1");
    CHECK(s->blanked, "not blank");
    glass_fifo(w, "c", path, sizeof path);
    ask(in, "mirror glass stream=i420 size=64x36 fps=20");
    fd = open(path, O_RDONLY | O_NONBLOCK);
    CHECK(first_stream(s), "no stream");
    /* Draws are counted from here, so only frames numbered from here are
     * held to them: a frame drawn while the stream was asked, before any
     * reader, may still reach this one when the writer is slow to try it. */
    fbs = fake.fb_draws;
    first = first_stream(s)->number;
    got = read_glass(fd, 0.3, h, 64, &last);
    for (i = 0; i < got && i < 64; i++)
        since += h[i].n >= first;
    CHECK(got >= 3, "%d frames from a dark wall", got);
    CHECK(fake.fb_draws - fbs >= 2 * since, "the scene was not drawn for each frame of a dark "
          "wall: %d draws for %d frames", fake.fb_draws - fbs, since);
    free(last);
    close(fd);
    close(in);
    close(out);
}

/* Picture glass the same before a stream, during it and after it. */
static void a_glass_picture_is_the_same_with_a_stream_and_after_it(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out), k;
    uint8_t *was = NULL;
    size_t waslen = 0;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60mhello\x1b[0m");
    pump(0.1);
    for (k = 0; k < 3; k++) {
        size_t n, len;
        const uint8_t *b, *q;
        char line[200];
        if (k == 1)
            ask(in, "mirror glass stream=i420 size=64x36 fps=20");
        if (k == 2)
            ask(in, "mirror glass off");
        out_bytes(out, &n);
        put(in, "\x1b_bitwall picture glass size=96x54 id=p\x1b\\");
        pump(0.1);
        b = out_bytes(out, &n);
        q = blob_of(b, n, "picture glass qoi 96x54 ", line, &len);
        CHECK(q, "no picture %d", k);
        if (!was) {
            was = malloc(len);
            memcpy(was, q, len);
            waslen = len;
        } else {
            CHECK(len == waslen && !memcmp(q, was, len), "picture %d differs", k);
        }
        CHECK(fake.framebuffer == 0 && !s->capturing, "the screen was left drawing elsewhere");
    }
    free(was);
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* A streamed frame against a picture of the glass taken at the same
 * moment, in software, on the old path, where the glass is real pixels:
 * its Y the picture's luma, BT.709 at studio range, within a level; its
 * U and V a solid colour's; and black, 16 and 128, round the glass. */
static void a_streamed_frame_matches_a_picture_of_the_glass(void)
{
    Screen *s = soft_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out), fd, got, x, y;
    Raw *r = calloc(1, sizeof *r);
    Head h[4];
    uint8_t *last, *px, *Y, *U, *V;
    size_t n, len;
    const uint8_t *b, *q;
    char line[200];
    qoi_desc d;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60m                                        "
             "\x1b[48;2;20;180;240m                                        "
             "\x1b[2;1H\x1b[38;2;255;255;255;48;2;0;90;0mHello, glass\x1b[0m");
    for (y = 3; y < 9; y++) {
        char row[400];
        snprintf(row, sizeof row, "\x1b[%d;1H\x1b[48;2;240;200;20m%*s\x1b[0m", y, s->cols, "");
        put(bin, row);
    }
    pump(0.1);
    setenv("BITWALL_STREAM_DRAW_TWICE", "1", 1);
    r->owner = session_named(w, "c");
    r->w = 160;
    r->h = 120;
    r->fps = 10;
    r->every = 100;
    glass_fifo(w, "c", r->path, sizeof r->path);
    CHECK(raw_up(s, r) == 0, "the old path would not start in software");
    fd = open(r->path, O_RDONLY | O_NONBLOCK);
    raw_frame(s, r);
    out_bytes(out, &n);
    put(in, "\x1b_bitwall picture glass size=160x120 id=p\x1b\\");
    got = read_glass(fd, 0.1, h, 4, &last);
    CHECK(got >= 1, "no frame");
    CHECK(r->bx == 0 && r->by == 15 && r->bw == 160 && r->bh == 90, "the glass at %d,%d %dx%d",
          r->bx, r->by, r->bw, r->bh);
    b = out_bytes(out, &n);
    q = blob_of(b, n, "picture glass qoi 160x90 ", line, &len);
    CHECK(q, "no picture");
    px = qoi_decode(q, (int)len, &d, 4);
    CHECK(px, "the picture does not decode");
    Y = last + RAW_HEADER;
    U = Y + 160 * 120;
    V = U + 80 * 60;
    for (y = 0; y < 120; y++)
        for (x = 0; x < 160; x++) {
            int want = 16;
            if (y >= 15 && y < 105) {
                const uint8_t *c = px + ((size_t)(y - 15) * 160 + x) * 4;
                want = (int)(16 + 219 * (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255
                             + 0.5);
            }
            CHECK(abs(Y[y * 160 + x] - want) <= 1, "Y at %d,%d is %d, the picture's %d", x, y,
                  Y[y * 160 + x], want);
        }
    for (y = 15, x = 0; y < 105; y++)
        x += Y[y * 160 + 80] > 150;
    CHECK(x >= 3, "the glass has nothing on it to compare");
    for (y = 0; y < 60; y++)
        for (x = 0; x < 80; x++) {
            const uint8_t *c[4];
            int i, inside = y >= 8 && y < 52, same = 1;
            double u = 128, v = 128;
            for (i = 0; inside && i < 4; i++) {
                c[i] = px + ((size_t)(y * 2 + (i >> 1) - 15) * 160 + x * 2 + (i & 1)) * 4;
                same &= !memcmp(c[i], c[0], 3);
            }
            if (inside && !same)
                continue;           /* an edge, where four pixels are averaged */
            if (inside) {
                u = 128 + 224 * (-0.1146 * c[0][0] - 0.3854 * c[0][1] + 0.5 * c[0][2]) / 255;
                v = 128 + 224 * (0.5 * c[0][0] - 0.4542 * c[0][1] - 0.0458 * c[0][2]) / 255;
            }
            CHECK(U[y * 80 + x] - u <= 1.5 && u - U[y * 80 + x] <= 1.5
                  && V[y * 80 + x] - v <= 1.5 && v - V[y * 80 + x] <= 1.5,
                  "U,V at %d,%d are %d,%d, the picture's %.1f,%.1f", x, y, U[y * 80 + x],
                  V[y * 80 + x], u, v);
        }
    free(px);
    free(last);
    raw_free(s, r);
    close(fd);
    close(in);
    close(out);
    close(bin);
    close(bout);
}

/* Frames off several glass fifos at once for `secs`, the wall pumped
 * meanwhile: how many came whole on each, and each one's size, which
 * must be the same for all of its frames. */
static void read_glasses(const int *fd, int nfd, double secs, int *count, int *w, int *h)
{
    size_t cap[4], have[4] = {0, 0, 0, 0};
    uint8_t *b[4];
    double t0 = now_ms();
    int i;
    CHECK(nfd <= 4, "too many");
    for (i = 0; i < nfd; i++) {
        cap[i] = 1 << 20;
        b[i] = malloc(cap[i]);
        CHECK(b[i], "out of memory");
        count[i] = w[i] = h[i] = 0;
    }
    do {
        pump(0.005);
        for (i = 0; i < nfd; i++) {
            ssize_t k;
            while ((k = read(fd[i], b[i] + have[i], cap[i] - have[i])) > 0) {
                have[i] += (size_t)k;
                if (have[i] == cap[i]) {
                    cap[i] *= 2;
                    b[i] = realloc(b[i], cap[i]);
                    CHECK(b[i], "out of memory");
                }
            }
            while (have[i] >= RAW_HEADER) {
                size_t len;
                int fw, fh;
                CHECK(!memcmp(b[i], "BWG1", 4), "stream %d's frame %d is no frame", i, count[i]);
                len = RAW_HEADER + (size_t)le_of(b[i] + 28, 4);
                if (have[i] < len)
                    break;
                fw = (int)le_of(b[i] + 4, 4);
                fh = (int)le_of(b[i] + 8, 4);
                CHECK(!count[i] || (fw == w[i] && fh == h[i]), "stream %d changed size", i);
                w[i] = fw;
                h[i] = fh;
                count[i]++;
                memmove(b[i], b[i] + len, have[i] - len);
                have[i] -= len;
            }
        }
    } while (now_ms() - t0 < secs * 1e3);
    for (i = 0; i < nfd; i++)
        free(b[i]);
}

/* Several streams at once, from one session and from another, each at
 * the size and rate it asked for, on a fifo named for it; one stopped by
 * name leaves the rest, and the measure line counts a session's all. */
static void many_streams_run_at_once_each_at_its_own_size_and_rate(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), out2, in2 = ctl_open(w, "c2", &out2), fd[3];
    int count[3], fw[3], fh[3], i;
    char path[3][800], line[MAX_APC];
    static const char *const names[3] = {"glass-a", "glass-b", "glass"};
    CHECK(strstr(caps_of(w), " streams=8"), "caps does not say streams: %s", caps_of(w));
    ask(in, "mirror glass stream=i420 size=64x36 fps=20 name=a");
    ask(in, "mirror glass stream=i420 size=32x18 fps=10 name=b");
    ask(in2, "mirror glass stream=i420 size=48x28 fps=5");
    CHECK(!strstr(out_text(out), "error") && !strstr(out_text(out2), "error"),
          "a stream was refused");
    CHECK(s->outs && s->outs->n == 3, "%d streams", s->outs ? s->outs->n : 0);
    for (i = 0; i < 3; i++) {
        snprintf(path[i], sizeof path[i], "%s/s/%s/%s", w->dir, i < 2 ? "c" : "c2", names[i]);
        CHECK(is_fifo(path[i]), "no fifo at %s", path[i]);
        fd[i] = open(path[i], O_RDONLY | O_NONBLOCK);
        CHECK(fd[i] >= 0, "cannot open %s", path[i]);
    }
    read_glasses(fd, 3, 1.0, count, fw, fh);
    CHECK(fw[0] == 64 && fh[0] == 36 && fw[1] == 32 && fh[1] == 18 && fw[2] == 48 && fh[2] == 28,
          "sizes %dx%d %dx%d %dx%d", fw[0], fh[0], fw[1], fh[1], fw[2], fh[2]);
    CHECK(count[0] >= 15 && count[0] <= 22 && count[1] >= 7 && count[1] <= 12
          && count[2] >= 3 && count[2] <= 7, "%d, %d and %d frames in a second at 20, 10 and 5",
          count[0], count[1], count[2]);
    session_measure(session_named(w, "c"), now_ms(), line, sizeof line);
    {
        char want[64];
        Raw *a = s->outs->streams[0], *b = s->outs->streams[1];
        snprintf(want, sizeof want, " streamed=%lu ", a->streamed + b->streamed);
        CHECK(strstr(line, want), "the measure line does not count both: %s", line);
    }
    ask(in, "mirror glass off name=a");
    CHECK(!is_fifo(path[0]) && is_fifo(path[1]) && is_fifo(path[2]) && s->outs->n == 2,
          "off by name stopped the wrong streams");
    out_text(out);
    ask(in, "mirror glass off name=zz id=z");
    CHECK(strstr(out_text(out), "error mirror name=zz: no such stream id=z"),
          "an unknown name was not said");
    ask(in, "mirror glass off");
    CHECK(!is_fifo(path[1]) && is_fifo(path[2]) && s->outs->n == 1,
          "off stopped another session's stream, or not its own");
    close(in2);
    pump(0.2);
    CHECK(!s->outs && !is_fifo(path[2]), "the last stream's end left the outputs");
    for (i = 0; i < 3; i++)
        close(fd[i]);
    close(in);
    close(out);
    close(out2);
}

static void a_streams_name_and_overlay_are_checked_and_a_wall_runs_eight(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int out, in = ctl_open(w, "c", &out), i;
    static const char *const bad[][2] = {
        {"mirror glass stream=i420 size=64x36 fps=10 name=a/b", "error mirror name=a/b: bad name id=e"},
        {"mirror glass stream=i420 size=64x36 fps=10 name=", "error mirror name=: bad name id=e"},
        {"mirror glass stream=i420 size=64x36 fps=10 name=abcdefghijklmnopqrstuvwxy",
         "error mirror name=abcdefghijklmnopqrstuvwxy: bad name id=e"},
        {"mirror glass stream=i420 size=64x36 fps=10 overlay=2", "error mirror overlay=2: not 0 or 1 id=e"},
    };
    char cmd[200];
    out_text(out);
    for (i = 0; i < (int)(sizeof bad / sizeof bad[0]); i++) {
        const char *said;
        snprintf(cmd, sizeof cmd, "%s id=e", bad[i][0]);
        ask(in, cmd);
        said = out_text(out);
        CHECK(strstr(said, bad[i][1]), "%s: heard %s", bad[i][0], said);
    }
    CHECK(!s->outs, "a refused stream was started");
    ask(in, "mirror glass stream=i420 size=32x18 fps=1 name=n0 overlay=1");
    ask(in, "mirror glass stream=i420 size=32x18 fps=1 name=n0 id=b");
    CHECK(strstr(out_text(out), "error mirror busy id=b"), "a name asked twice was not busy");
    CHECK(s->outs->n == 1 && s->outs->streams[0]->overlay, "overlay=1 not kept");
    for (i = 1; i < STREAMS; i++) {
        snprintf(cmd, sizeof cmd, "mirror glass stream=i420 size=32x18 fps=1 name=n%d", i);
        ask(in, cmd);
    }
    CHECK(s->outs->n == STREAMS && !strstr(out_text(out), "error"), "%d streams", s->outs->n);
    ask(in, "mirror glass stream=i420 size=32x18 fps=1 id=f");
    CHECK(strstr(out_text(out), "error mirror glass: 8 streams already id=f"),
          "a ninth stream was not refused");
    close(in);
    pump(0.2);
    CHECK(!s->outs, "the session's end left its streams");
    close(out);
}

/* However many streams look at it, the glass is composed once while it
 * stands still: each stream's frames are its own passes from the one
 * scene, and the overlay is drawn over a copy of it only for a stream
 * that asks for it, while it is up. */
static void every_stream_is_drawn_from_one_composition(void)
{
    Screen *s = gpu_screen(14);
    Wall *w = wall_at(s, "0");
    int bout, bin = base_open(w, &bout), out, in = ctl_open(w, "c", &out);
    int fbs, i;
    long passes = 0;
    put(bin, "\x1b[1;1H\x1b[48;2;200;30;60mhello\x1b[0m");
    pump(0.1);
    ask(in, "mirror glass stream=i420 size=64x36 fps=30 name=a");
    ask(in, "mirror glass stream=i420 size=32x18 fps=20 name=b");
    ask(in, "mirror glass stream=i420 size=48x28 fps=10 name=c");
    pump(0.1);
    fbs = fake.fb_draws;
    for (i = 0; i < 3; i++)
        passes -= (long)s->outs->streams[i]->number;
    pump(0.6);
    for (i = 0; i < 3; i++)
        passes += (long)s->outs->streams[i]->number;
    CHECK(passes >= 20, "%ld frames", passes);
    /* One pass a frame, the planes shrunk on the way; the scene not
     * once, since nothing changed. */
    CHECK(fake.fb_draws - fbs == passes, "%d draws for %ld frames of a glass standing still",
          fake.fb_draws - fbs, passes);
    CHECK(!s->outs->lit_tex, "the overlay was drawn with no stream asking for it");
    s->st.on = 1;
    ask(in, "mirror glass stream=i420 size=64x36 fps=30 name=o overlay=1");
    pump(0.2);
    CHECK(s->outs->lit_tex, "the overlay was not drawn for the stream that asked");
    put(bin, "\x1b[2;1Hthere\x1b[0m");
    pump(0.2);
    close(in);
    close(out);
    close(bin);
    close(bout);
}

static const Test TESTS[] = {
#define T(name) {#name, name}
    /* kitty */
    T(a_kitty_png_decodes_and_a_cut_one_does_not),
    T(a_kitty_picture_is_a_texture_drawn_over_its_cells),
    T(an_overlong_kitty_string_is_let_go_whole),
    T(a_whole_overlong_kitty_string_is_refused),
    /* utf-8 */
    T(utf8_decodes_every_length_whole),
    T(utf8_holds_every_incomplete_prefix),
    T(utf8_malformed_costs_one_byte),
    T(utf8_split_at_every_byte_reaches_the_cell_whole),
    /* sgr */
    T(sgr_truecolour_sets_ink_and_ground),
    T(sgr_256_colour_uses_the_xterm_cube),
    T(sgr_named_bold_reverse_and_defaults),
    T(sgr_truncated_colour_sets_no_colour),
    T(sgr_with_more_parameters_than_kept_is_safe),
    T(sgr_private_forms_leave_the_pen_alone),
    /* frame delimiter */
    T(a_bare_reset_presents_one_frame),
    T(a_reset_with_parameters_is_not_a_frame),
    T(a_bare_reset_ends_the_pen_too),
    T(a_delimiter_cut_in_half_presents_once_it_arrives),
    T(a_frame_that_drew_nothing_presents_nothing),
    /* cup */
    T(cup_is_one_based_with_defaults),
    T(cup_off_the_grid_draws_nothing),
    T(cup_with_huge_numbers_does_not_overflow),
    T(cup_to_the_last_column_clips_a_wide_glyph),
    /* fuzz */
    T(fuzz_random_splits_paint_what_whole_input_paints),
    T(fuzz_garbage_is_survived_on_both_paths),
    /* atlas */
    T(the_grid_sits_in_the_middle_of_the_glass),
    T(every_frame_clears_the_glass_before_the_grid),
    T(a_static_cell_keeps_its_glyph_when_the_atlas_fills),
    T(a_static_cell_keeps_its_glyph_when_the_table_fills),
    T(the_atlas_has_room_for_the_grid_twice_over),
    T(a_grid_with_more_glyphs_than_the_atlas_holds_is_survived),
    T(a_cell_too_big_for_the_atlas_draws_nothing),
    T(a_gpu_that_offers_no_texture_is_not_used),
    /* dpms */
    T(dpms_is_sought_again_until_an_output_is_found),
    T(dpms_goes_to_the_output_being_driven),
    T(dpms_falls_back_to_a_connected_output),
    T(a_refused_dpms_is_said_so),
    /* cursor */
    T(a_cursor_run_off_the_grid_stops_at_the_edge),
    T(a_cursor_at_the_end_of_int_does_not_overflow),
    T(a_cursor_left_of_the_grid_draws_nothing),
    /* inbox */
    T(a_small_read_holds_a_small_chunk),
    T(an_inbox_hands_over_what_arrived_in_order),
    /* exit */
    T(a_reader_that_cannot_be_woken_is_left_rather_than_waited_for),
    T(the_monitor_is_woken_when_the_wall_comes_up),
    T(the_monitor_is_woken_when_the_wall_goes),
    /* the door and the base */
    T(a_base_frame_reaches_the_glass),
    T(a_video_frame_is_blended_over_the_grid_in_software),
    T(a_video_frame_is_a_texture_drawn_over_the_grid_on_the_gpu),
    T(a_video_layer_clears_when_its_hold_runs_out),
    T(a_layer_frame_redraws_the_glass_over_a_base_mid_frame_on_the_gpu),
    T(a_layer_frame_redraws_the_glass_over_a_base_mid_frame_in_software),
    T(a_video_layer_clears_on_clear_close_and_eof),
    T(a_video_layer_plays_on_through_a_font_change),
    T(a_video_layer_clears_and_is_told_repaint_when_the_pixels_change),
    T(a_video_frame_it_cannot_take_is_skipped_and_said),
    T(a_layers_frames_go_up_in_order_however_long_each_decodes),
    T(a_slow_decode_never_holds_up_the_main_loop),
    T(a_picture_keeps_its_place_among_the_answers),
    T(a_stalled_video_layer_never_holds_up_the_base),
    T(a_layer_is_placed_above_top_or_where_it_asks),
    T(layers_blend_bottom_to_top),
    T(a_glass_picture_in_software_blends_the_layers_in),
    T(a_glass_picture_is_drawn_at_its_size_and_read_back),
    T(a_picture_of_each_session_is_in_its_own_format),
    T(a_mirror_of_each_target_sends_what_changed),
    T(a_mirror_frame_is_dropped_while_the_last_waits),
    T(a_dark_wall_is_pictured_as_though_it_were_lit),
    T(a_dark_wall_answers_a_glass_picture_on_the_gpu),
    T(size_is_the_most_a_glass_picture_may_be),
    T(glass_is_not_a_session_name),
    T(a_video_mirror_is_told_when_the_layer_is_taken_down),
    T(a_video_base_holds_the_base_and_its_first_frame_is_a_sender),
    T(data_is_refused_where_it_cannot_be_taken),
    T(a_qoi_stream_on_data_fills_the_glass_and_ends_clearing_the_layer),
    T(a_timed_stream_waits_for_its_start_and_drops_what_is_late),
    T(a_stream_that_does_not_decode_is_said_once),
    T(hevc_and_h264_on_data_go_up_as_yuv_through_the_gpu),
    T(hw_names_the_codecs_taken_that_the_board_decodes),
    T(caps_says_no_hw_where_nothing_is_in_hardware),
    T(mjpeg_goes_to_the_boards_decoder_and_h264_where_it_has_one),
    T(mjpeg_is_taken_only_where_the_board_decodes_it),
    T(a_base_font_change_resizes_and_announces),
    T(a_font_that_keeps_the_grid_keeps_the_picture),
    T(an_idle_loop_sleeps_for_its_wait),
    T(a_base_held_but_silent_is_not_told_the_status),
    T(the_screen_claims_the_icons_the_font_has_and_what_is_drawn_whatever),
    T(a_sender_gone_clears_the_screen_and_resets),
    T(take_displaces_the_base_and_busy_refuses_without_it),
    /* walls */
    T(two_outputs_are_two_walls_in_connector_order),
    T(walls_follow_the_card_when_sdl_numbers_displays_otherwise),
    T(a_window_that_cannot_be_told_apart_takes_its_display_number),
    T(one_output_is_todays_single_wall),
    T(text_sent_to_one_wall_never_reaches_the_other),
    T(text_sent_to_one_wall_never_reaches_the_other_in_software),
    T(every_gl_call_is_made_with_its_own_walls_context),
    T(a_font_change_on_one_wall_leaves_the_others_area_and_atlas),
    T(each_wall_says_which_it_is_on_its_status_page),
    T(measures_read_the_counters),
    T(the_legacy_blocks_are_said_whatever_the_font),
    /* shapes and ghosts */
    T(block_halves_meet_with_nothing_between_them),
    T(box_lines_meet_every_neighbour_they_point_at),
    T(sextants_and_octants_are_whole_parts_of_their_cell),
    T(legacy_blocks_are_what_their_names_say),
    T(arcs_meet_the_light_lines_they_turn),
    T(a_double_line_is_two_lines_and_its_cross_is_open),
    T(dashed_and_diagonal_lines_are_left_to_the_font),
    T(box_lines_are_drawn_to_the_edge_in_software),
    T(faint_braille_shows_its_unlit_dots_on_the_gpu),
    T(faint_braille_shows_its_unlit_dots_in_software),
    T(a_picture_says_which_braille_is_faint),
    T(bold_text_is_drawn_in_the_bold_font_and_geometry_is_not),
    T(bold_text_without_a_bold_font_is_drawn_regular),
    T(a_picture_of_bold_text_is_its_characters),
    T(triangles_fill_to_their_corners_and_complete_each_other),
    /* the software picture, stats, want, restart and meta */
    T(the_software_picture_of_each_scene_is_unchanged),
    T(a_video_layer_over_cells_in_software_is_unchanged),
    T(the_stats_overlay_in_software_is_unchanged),
    T(stats_turns_on_and_off_and_says_so),
    T(stats_is_not_drawn_over_the_status_page),
    T(want_takes_only_well_formed_inputs),
    T(an_atlas_restart_puts_every_cell_back_once_it_pays),
    T(a_layers_meta_goes_when_its_session_closes),
    T(nothing_after_a_base_ends_itself_is_painted),
    T(a_base_ending_holds_up_the_loop_for_no_time),
    T(a_stale_wall_is_taken_one_claim_at_a_time),
    T(a_stuck_out_waits_for_room_rather_than_spinning),
    /* the glass, streamed */
    T(a_glass_stream_is_refused_what_it_cannot_take),
    T(a_glass_stream_is_not_offered_in_software),
    T(a_glass_stream_writes_whole_frames_to_its_fifo),
    T(a_glass_stream_comes_at_its_rate_and_its_numbers_and_times_rise),
    T(a_slow_reader_has_frames_dropped_and_counted),
    T(a_glass_stream_ends_with_off_or_its_session_and_leaves_nothing),
    T(with_no_stream_the_glass_is_drawn_straight_and_nothing_is_made),
    T(while_streamed_the_glass_is_drawn_once_and_copied),
    T(the_old_path_draws_the_glass_again_for_each_frame),
    T(on_gles3_a_frame_is_read_into_a_buffer_and_collected_at_the_next),
    T(a_dark_wall_is_streamed_as_though_it_were_lit),
    T(a_glass_picture_is_the_same_with_a_stream_and_after_it),
    T(a_streamed_frame_matches_a_picture_of_the_glass),
    T(many_streams_run_at_once_each_at_its_own_size_and_rate),
    T(a_streams_name_and_overlay_are_checked_and_a_wall_runs_eight),
    T(every_stream_is_drawn_from_one_composition),
#undef T
};

/* Run one test in a child, and say how it went. What the child said on
 * stderr -- a CHECK, or a sanitizer's report -- is shown under a failure. */
static int run(const Test *t)
{
    int err[2], status = 0;
    pid_t pid;
    char said[4096];
    size_t got = 0;
    ssize_t n;

    if (pipe(err) < 0)
        return 0;
    fflush(stdout);
    pid = fork();
    if (pid == 0) {
        close(err[0]);
        dup2(err[1], 2);
        if (!freopen("/dev/null", "w", stdout))
            exit(2);
        alarm(60);
        signal(SIGPIPE, SIG_IGN);   /* as the painter's main has it */
        fake_reset();
        shapes_init();
        palette_init();
        t->fn();
        exit(0);
    }
    close(err[1]);
    while ((n = read(err[0], said + got, sizeof said - 1 - got)) > 0)
        got += (size_t)n;
    said[got] = 0;
    close(err[0]);
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        printf("  ok    %s\n", t->name);
        return 1;
    }
    printf("  FAIL  %s", t->name);
    if (WIFSIGNALED(status))
        printf("  (signal %d)", WTERMSIG(status));
    printf("\n");
    {
        char *line = said, *nl;
        int lines = 0;
        while (*line && lines < 12) {
            nl = strchr(line, '\n');
            if (nl)
                *nl = 0;
            if (*line)
                printf("        %s\n", line), lines++;
            if (!nl)
                break;
            line = nl + 1;
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    size_t i, total = 0, passed = 0;
    for (i = 0; i < sizeof TESTS / sizeof TESTS[0]; i++) {
        int j, wanted = argc < 2;
        for (j = 1; j < argc; j++)
            if (strstr(TESTS[i].name, argv[j]))
                wanted = 1;
        if (!wanted)
            continue;
        total++;
        passed += (size_t)run(&TESTS[i]);
    }
    printf("\n%zu passed, %zu failed\n", passed, total - passed);
    return passed == total ? 0 : 1;
}
