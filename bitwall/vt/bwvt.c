/* A terminal emulator for TmuxSource and CommandSource: libghostty-vt,
 * Ghostty's own, behind the few calls bitwall/terminal.py makes.
 *
 * Python reads a screen in one call rather than a cell at a time, because
 * a wall is thousands of cells and a call through ctypes costs a
 * microsecond or more. A read hands over only the rows that changed since
 * the last one, as fixed records and one buffer of text, which
 * terminal.py turns into bitwall's cells.
 *
 * A record is twelve bytes, little-endian:
 *
 *     u32 fg      the colour, as its kind says
 *     u32 bg
 *     u8  fgkind  0 default, 1 a palette index, 2 0xRRGGBB
 *     u8  bgkind
 *     u8  flags   1 bold, 2 italic, 4 underline, 8 strikethrough,
 *                 16 inverse, 32 faint
 *     u8  wide    0 narrow, 1 wide, 2 the cell after a wide one
 *
 * and each cell's text is its grapheme in UTF-8 followed by a NUL: a
 * space for a cell with nothing in it, nothing for the cell after a wide
 * glyph, which is the cell format's own spelling of both.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <ghostty/vt.h>

#if defined(_WIN32)
#define BWVT_API __declspec(dllexport)
#else
#define BWVT_API __attribute__((visibility("default")))
#endif

/* What a cell is drawn at, for the size reports a program may ask for.
 * No pixels are drawn here, so any size will do. */
#define CELL_W 10
#define CELL_H 20

/* How long a program may hold the screen while it draws a frame
 * (synchronized output, mode 2026) before the hold is let go: one that
 * crashes or forgets mid-frame must not freeze the wall. Ghostty's own
 * renderer and kitty give up after a second too. */
#define HOLD_MS 1000

typedef struct {
    GhosttyTerminal term;
    GhosttyRenderState state;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    /* What the terminal said back to the program -- a cursor position, a
     * device's attributes -- until the host takes it to the pty. */
    uint8_t *said;
    size_t said_len, said_cap;
    /* A program drawing a frame it wants seen whole: the screen as it
     * stood when the frame began is what is read until it ends. */
    bool held;
    uint64_t held_since;
} Bwvt;

static uint64_t now_ms(void)
{
    struct timespec ts;
    timespec_get(&ts, TIME_UTC);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}

/* Taken when a hold begins rather than at the next read, so a frame that
 * ends and a next that begins between two reads leaves the finished one
 * to be read, not the start of the next. */
static void hold(GhosttyTerminal t, void *user, bool held)
{
    Bwvt *v = user;
    if (held && !v->held) {
        ghostty_render_state_update(v->state, t);
        v->held_since = now_ms();
    }
    v->held = held;
}

static void said(GhosttyTerminal t, void *user, const uint8_t *data, size_t n)
{
    (void)t;
    Bwvt *v = user;
    if (v->said_len + n > v->said_cap) {
        size_t cap = v->said_cap ? v->said_cap * 2 : 256;
        while (cap < v->said_len + n)
            cap *= 2;
        uint8_t *grown = realloc(v->said, cap);
        if (!grown)
            return;
        v->said = grown;
        v->said_cap = cap;
    }
    memcpy(v->said + v->said_len, data, n);
    v->said_len += n;
}

BWVT_API void bwvt_free(Bwvt *v)
{
    if (!v)
        return;
    ghostty_render_state_row_cells_free(v->cells);
    ghostty_render_state_row_iterator_free(v->rows);
    ghostty_render_state_free(v->state);
    ghostty_terminal_free(v->term);
    free(v->said);
    free(v);
}

BWVT_API Bwvt *bwvt_new(uint16_t cols, uint16_t rows)
{
    Bwvt *v = calloc(1, sizeof *v);
    if (!v)
        return NULL;
    if (ghostty_terminal_new(NULL, &v->term, cols, rows) != GHOSTTY_SUCCESS
        || ghostty_render_state_new(NULL, &v->state) != GHOSTTY_SUCCESS
        || ghostty_render_state_row_iterator_new(NULL, &v->rows) != GHOSTTY_SUCCESS
        || ghostty_render_state_row_cells_new(NULL, &v->cells) != GHOSTTY_SUCCESS) {
        bwvt_free(v);
        return NULL;
    }
    /* A wall shows the screen and never scrolls back. */
    size_t none = 0;
    ghostty_terminal_set(v->term, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &none);
    ghostty_terminal_set(v->term, GHOSTTY_TERMINAL_OPT_USERDATA, v);
    ghostty_terminal_set(v->term, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *)said);
    ghostty_terminal_set(v->term, GHOSTTY_TERMINAL_OPT_RENDER_HOLD, (const void *)hold);
    ghostty_terminal_resize(v->term, cols, rows, CELL_W, CELL_H);
    return v;
}

BWVT_API void bwvt_write(Bwvt *v, const uint8_t *data, size_t n)
{
    ghostty_terminal_vt_write(v->term, data, n);
}

BWVT_API int bwvt_resize(Bwvt *v, uint16_t cols, uint16_t rows)
{
    return ghostty_terminal_resize(v->term, cols, rows, CELL_W, CELL_H);
}

/* The replies the program is owed, up to cap bytes; the rest stay for the
 * next call. */
BWVT_API size_t bwvt_take_said(Bwvt *v, uint8_t *out, size_t cap)
{
    size_t n = v->said_len < cap ? v->said_len : cap;
    memcpy(out, v->said, n);
    memmove(v->said, v->said + n, v->said_len - n);
    v->said_len -= n;
    return n;
}

static void colour(GhosttyStyleColor c, uint32_t *value, uint8_t *kind)
{
    switch (c.tag) {
    case GHOSTTY_STYLE_COLOR_PALETTE:
        *kind = 1;
        *value = c.value.palette;
        break;
    case GHOSTTY_STYLE_COLOR_RGB:
        *kind = 2;
        *value = (uint32_t)c.value.rgb.r << 16 | (uint32_t)c.value.rgb.g << 8
            | c.value.rgb.b;
        break;
    default:
        *kind = 0;
        *value = 0;
    }
}

/* The screen's size, and the rows that changed since the last read: their
 * numbers in `ys`, their records in `recs` (cols of them a row) and their
 * text in `text`. Returns how many rows, or -1 when a buffer is too small,
 * in which case the rows already read are marked as read: ask again with
 * larger buffers and `all`. `all` asks for every row whether it changed or
 * not. */
BWVT_API int bwvt_read(Bwvt *v, int all, uint16_t *cols_out, uint16_t *rows_out,
                       uint16_t *ys, uint8_t *recs, size_t recs_cap,
                       uint8_t *text, size_t text_cap, size_t *text_len)
{
    if (v->held && now_ms() - v->held_since >= HOLD_MS) {
        GhosttyTerminalModeConfig off = { .mode = GHOSTTY_MODE_SYNC_OUTPUT,
                                          .value = false };
        ghostty_terminal_set(v->term, GHOSTTY_TERMINAL_OPT_MODE, &off);
        v->held = false;
    }
    if (!v->held && ghostty_render_state_update(v->state, v->term) != GHOSTTY_SUCCESS)
        return -1;
    uint16_t cols = 0, rows = 0;
    ghostty_render_state_get(v->state, GHOSTTY_RENDER_STATE_DATA_COLS, &cols);
    ghostty_render_state_get(v->state, GHOSTTY_RENDER_STATE_DATA_ROWS, &rows);
    *cols_out = cols;
    *rows_out = rows;
    GhosttyRenderStateDirty dirty = GHOSTTY_RENDER_STATE_DIRTY_FALSE;
    ghostty_render_state_get(v->state, GHOSTTY_RENDER_STATE_DATA_DIRTY, &dirty);
    if (dirty == GHOSTTY_RENDER_STATE_DIRTY_FULL)
        all = 1;
    if (recs_cap < (size_t)cols * rows * 12)
        return -1;
    if (ghostty_render_state_get(v->state, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR,
                                 &v->rows) != GHOSTTY_SUCCESS)
        return -1;

    int n = 0;
    size_t at = 0;
    uint16_t y = 0;
    bool clean = false;
    for (;; y++) {
        if (!ghostty_render_state_row_iterator_next(v->rows))
            break;
        bool changed = false;
        ghostty_render_state_row_get(v->rows, GHOSTTY_RENDER_STATE_ROW_DATA_DIRTY,
                                     &changed);
        if (!changed && !all)
            continue;
        if (ghostty_render_state_row_get(v->rows, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS,
                                         &v->cells) != GHOSTTY_SUCCESS)
            return -1;
        uint8_t *rec = recs + (size_t)n * cols * 12;
        for (uint16_t x = 0; x < cols; x++, rec += 12) {
            if (!ghostty_render_state_row_cells_next(v->cells)) {
                memset(rec, 0, 12);
                if (at + 2 > text_cap)
                    return -1;
                text[at++] = ' ';
                text[at++] = 0;
                continue;
            }
            GhosttyStyle style = GHOSTTY_INIT_SIZED(GhosttyStyle);
            ghostty_render_state_row_cells_get(
                v->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style);
            GhosttyCell raw = 0;
            ghostty_render_state_row_cells_get(
                v->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, &raw);
            GhosttyCellWide wide = GHOSTTY_CELL_WIDE_NARROW;
            ghostty_cell_get(raw, GHOSTTY_CELL_DATA_WIDE, &wide);

            uint32_t fg, bg;
            uint8_t fgk, bgk;
            colour(style.fg_color, &fg, &fgk);
            colour(style.bg_color, &bg, &bgk);
            /* The cell's own background, where it is coloured with no
             * style -- a region erased in a colour. */
            GhosttyCellContentTag content = GHOSTTY_CELL_CONTENT_CODEPOINT;
            ghostty_cell_get(raw, GHOSTTY_CELL_DATA_CONTENT_TAG, &content);
            if (bgk == 0 && content == GHOSTTY_CELL_CONTENT_BG_COLOR_PALETTE) {
                GhosttyColorPaletteIndex index = 0;
                ghostty_cell_get(raw, GHOSTTY_CELL_DATA_COLOR_PALETTE, &index);
                bgk = 1;
                bg = index;
            } else if (bgk == 0 && content == GHOSTTY_CELL_CONTENT_BG_COLOR_RGB) {
                GhosttyColorRgb rgb = {0};
                ghostty_cell_get(raw, GHOSTTY_CELL_DATA_COLOR_RGB, &rgb);
                bgk = 2;
                bg = (uint32_t)rgb.r << 16 | (uint32_t)rgb.g << 8 | rgb.b;
            }
            uint8_t flags = (style.bold ? 1 : 0) | (style.italic ? 2 : 0)
                | (style.underline ? 4 : 0) | (style.strikethrough ? 8 : 0)
                | (style.inverse ? 16 : 0) | (style.faint ? 32 : 0);
            uint8_t w = wide == GHOSTTY_CELL_WIDE_WIDE ? 1
                : wide == GHOSTTY_CELL_WIDE_SPACER_TAIL ? 2 : 0;
            memcpy(rec, &fg, 4);
            memcpy(rec + 4, &bg, 4);
            rec[8] = fgk;
            rec[9] = bgk;
            rec[10] = flags;
            rec[11] = w;

            if (w == 2) {
                if (at + 1 > text_cap)
                    return -1;
                text[at++] = 0;
                continue;
            }
            GhosttyBuffer buf = { .ptr = text + at, .cap = text_cap - at, .len = 0 };
            if (style.invisible)
                buf.len = 0;
            else if (ghostty_render_state_row_cells_get(
                         v->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8,
                         &buf) != GHOSTTY_SUCCESS)
                return -1;
            if (buf.len == 0) {
                if (at + 1 > text_cap)
                    return -1;
                text[at++] = ' ';
            } else {
                at += buf.len;
            }
            if (at + 1 > text_cap)
                return -1;
            text[at++] = 0;
        }
        ys[n++] = y;
        ghostty_render_state_row_set(v->rows, GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY,
                                     &clean);
    }
    GhosttyRenderStateDirty none = GHOSTTY_RENDER_STATE_DIRTY_FALSE;
    ghostty_render_state_set(v->state, GHOSTTY_RENDER_STATE_OPTION_DIRTY, &none);
    *text_len = at;
    return n;
}
