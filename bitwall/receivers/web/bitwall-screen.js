/* <bitwall-screen> -- a bitwall wall in a browser tab.
 *
 *   <bitwall-screen name="kitchen"></bitwall-screen>
 *
 * The element is the box, and the glass of the wall it names: it stacks
 * the wall's layers, the base's cells on a canvas and each video layer on
 * a canvas of its own over it, and paints what the server hands on from
 * the wall's sessions.
 *
 * Attributes
 *   name       the wall to show; required
 *   font-size  the font the wall's cells are drawn at until one is asked for
 *   cols,rows  fix the grid and let the font be whatever makes it fit
 *   primary    this tab decides the wall, not a thumbnail of it
 *   touch      report taps as input, when a sender asks for them
 *   keyboard   report keystrokes as input, when a sender asks for them
 *
 * Methods
 *   zoom(mult) magnify the font the wall is asked for; 1 is as asked
 *
 * One tab decides the wall. It says its box in device pixels and a cell's
 * size at every font, once a resize has settled, and the wall works out
 * its area from them as the painter does from its font; it counts how
 * often the glass is put up, and when asked hands back a look at it.
 *
 * The stream
 * ----------
 * The base's cells arrive as the sender wrote them, commands taken out. A
 * frame is absolutely positioned runs -- `ESC[y;xH`, an SGR, some text --
 * so the parser is a cursor, a colour and an attribute set. Bytes go into
 * a grid as they arrive and the grid is painted once per animation frame,
 * on frame boundaries, so a repaint the pipe split in two is one paint. A
 * video frame is QOI, put up on its layer as it arrives.
 *
 * A film on a layer's `data` arrives as its access units, each numbered,
 * and is decoded here, by the browser's own decoder (WebCodecs), and
 * played out on the wall's clock: frame i up when the clock reaches
 * t0 + i/fps, a frame whose successor is already due let go and counted.
 * The server sends each a little ahead of its time, and a tab arriving
 * mid-film the frames since the last key, decoded and not shown.
 *
 * The base's kitty graphics come apart from its cells, the server having
 * read them out of the stream: each picture once, under a key of the
 * wall's, and the placements as one list in the order they are drawn. A
 * placement is in cells, drawn on the base's canvas as the rows it covers
 * are, between their ground and their ink where its z is negative and
 * over the ink otherwise, so a font or area change moves it with them.
 */
(function () {
  'use strict';

  /* attribute bits */
  var BOLD = 1, ITALIC = 2, UNDER = 4, STRIKE = 8, REVERSE = 16, FAINT = 32;

  /* fg/bg sentinel: use the theme's colour rather than a named one */
  var DEFAULT = -1;

  /* ------------------------------------------------------------ palette */

  var PALETTE = (function () {
    var p = [0x000000, 0xcd0000, 0x00cd00, 0xcdcd00, 0x0000ee, 0xcd00cd,
             0x00cdcd, 0xe5e5e5, 0x7f7f7f, 0xff0000, 0x00ff00, 0xffff00,
             0x5c5cff, 0xff00ff, 0x00ffff, 0xffffff];
    var steps = [0, 95, 135, 175, 215, 255], r, g, b;
    for (r = 0; r < 6; r++)
      for (g = 0; g < 6; g++)
        for (b = 0; b < 6; b++)
          p.push((steps[r] << 16) | (steps[g] << 8) | steps[b]);
    for (var i = 0; i < 24; i++) {
      var v = 8 + i * 10;
      p.push((v << 16) | (v << 8) | v);
    }
    return p;
  })();

  /* '#rrggbb' for a packed int, cached: canvas wants a string and building
   * one per cell is the whole cost of a frame. */
  var CSS = Object.create(null);
  function css(v) {
    var s = CSS[v];
    if (s === undefined) {
      s = CSS[v] = '#' + ('00000' + v.toString(16)).slice(-6);
    }
    return s;
  }

  /* ---------------------------------------------------------- geometry */

  /* Block elements, as rectangles in unit cell coordinates: [x, y, w, h].
   * Drawn rather than typed because a glyph stops short of the cell edge
   * and leaves a seam at every boundary, which on a wall of them reads as
   * a grid of hairlines. */
  var BLOCK = {
    0x2588: [[0, 0, 1, 1]],
    /* An emitter rather than a fill: inset on every side so a grid of them
     * reads as separate lamps with dark between, the way a real LED panel
     * does. The insets differ because a cell is twice as tall as it is
     * wide, and 0.8 of the width against 0.4 of the height is square. */
    0x25aa: [[0.1, 0.3, 0.8, 0.4]],
    0x2580: [[0, 0, 1, 0.5]],
    0x2584: [[0, 0.5, 1, 0.5]],
    0x258c: [[0, 0, 0.5, 1]],
    0x2590: [[0.5, 0, 0.5, 1]],
    /* quadrants */
    0x2596: [[0, 0.5, 0.5, 0.5]],
    0x2597: [[0.5, 0.5, 0.5, 0.5]],
    0x2598: [[0, 0, 0.5, 0.5]],
    0x2599: [[0, 0, 0.5, 1], [0.5, 0.5, 0.5, 0.5]],
    0x259a: [[0, 0, 0.5, 0.5], [0.5, 0.5, 0.5, 0.5]],
    0x259b: [[0, 0, 1, 0.5], [0, 0.5, 0.5, 0.5]],
    0x259c: [[0, 0, 1, 0.5], [0.5, 0.5, 0.5, 0.5]],
    0x259d: [[0.5, 0, 0.5, 0.5]],
    0x259e: [[0.5, 0, 0.5, 0.5], [0, 0.5, 0.5, 0.5]],
    0x259f: [[0.5, 0, 0.5, 1], [0, 0.5, 0.5, 0.5]]
  };
  (function () {
    var i;
    /* lower eighths, U+2581 (1/8) .. U+2587 (7/8) */
    for (i = 1; i <= 7; i++)
      BLOCK[0x2580 + i] = [[0, 1 - i / 8, 1, i / 8]];
    /* left eighths, U+2589 (7/8) .. U+258F (1/8) */
    for (i = 1; i <= 7; i++)
      BLOCK[0x2588 + i] = [[0, 0, (8 - i) / 8, 1]];
    BLOCK[0x2594] = [[0, 0, 1, 1 / 8]];               /* upper one eighth */
    BLOCK[0x2595] = [[7 / 8, 0, 1 / 8, 1]];           /* right one eighth */
    legacy();
    /* Braille, U+2801 .. U+28FF, as dots on an even grid: two across and
     * four down, each a square 0.6 of its slot and centred in it, so they
     * space the same across a cell boundary as within one, where a font's
     * leave a band between rows. Bit b is dot b+1: 1-3 down the left, 4-6
     * down the right, then 7 and 8 along the bottom. */
    var col = [0, 0, 0, 1, 1, 1, 0, 1], row = [0, 1, 2, 0, 1, 2, 3, 3], b, bit, dots;
    for (b = 1; b < 0x100; b++) {
      dots = [];
      for (bit = 0; bit < 8; bit++)
        if (b & (1 << bit))
          dots.push([col[bit] * 0.5 + 0.1, row[bit] * 0.25 + 0.05, 0.3, 0.15]);
      BLOCK[0x2800 + b] = dots;
    }
  })();

  /* A pattern of a cell cut two across and `rows` down, bit b the b+1th
   * part counted along each row from the top left, as the characters' own
   * names count them. Rows alike and next to each other are one
   * rectangle. The Pi painter's grid_add. */
  function grid(bits, rows) {
    var out = [], r = 0, row, end;
    while (r < rows) {
      row = bits >> (2 * r) & 3;
      end = r + 1;
      while (end < rows && (bits >> (2 * end) & 3) === row) end++;
      if (row)
        out.push([row === 2 ? 0.5 : 0, r / rows, row === 3 ? 1 : 0.5, (end - r) / rows]);
      r = end;
    }
    return out;
  }

  /* Symbols for Legacy Computing's solid blocks, as the Pi painter draws
   * them (legacy_init there). Sextants are every pattern of a 2x3 cell
   * but the four other characters draw, in order; octants every pattern
   * of a 2x4 cell but the 26 other characters draw, in order. With the
   * blocks above, every pattern of a 2x4 cell is one of these. */
  function legacy() {
    var drawn = [0x00, 0x01, 0x02, 0x03, 0x05, 0x0a, 0x0f, 0x14, 0x28, 0x3f, 0x40,
                 0x50, 0x55, 0x5a, 0x5f, 0x80, 0xa0, 0xa5, 0xaa, 0xaf, 0xc0, 0xf0,
                 0xf5, 0xfa, 0xfc, 0xff];
    /* the quarter bars, U+1CEA0-1CEAF: [x, y, w, h] in quarters */
    var bars = [[2, 3, 2, 1], [1, 3, 3, 1], [0, 3, 3, 1], [0, 3, 2, 1],
                [0, 2, 1, 2], [0, 1, 1, 3], [0, 0, 1, 3], [0, 0, 1, 2],
                [0, 0, 2, 1], [0, 0, 3, 1], [1, 0, 3, 1], [2, 0, 2, 1],
                [3, 0, 1, 2], [3, 0, 1, 3], [3, 1, 1, 3], [3, 2, 1, 2]];
    var parts = [2, 3, 5, 6, 7], cp = 0x1fb00, b, i, e = 1 / 8;
    for (b = 1; b < 63; b++)
      if (b !== 21 && b !== 42) BLOCK[cp++] = grid(b, 3);
    for (cp = 0x1cd00, b = 0; b < 256; b++)
      if (drawn.indexOf(b) < 0) BLOCK[cp++] = grid(b, 4);
    for (i = 0; i < 16; i++)
      BLOCK[0x1cea0 + i] = [[bars[i][0] / 4, bars[i][1] / 4, bars[i][2] / 4, bars[i][3] / 4]];
    for (i = 0; i < 6; i++) {                     /* one eighth, 2nd to 7th */
      BLOCK[0x1fb70 + i] = [[(i + 1) * e, 0, e, 1]];
      BLOCK[0x1fb76 + i] = [[0, (i + 1) * e, 1, e]];
    }
    BLOCK[0x1fb7c] = [[0, 0, e, 7 * e], [0, 7 * e, 1, e]];        /* left and lower */
    BLOCK[0x1fb7d] = [[0, 0, 1, e], [0, e, e, 7 * e]];            /* left and upper */
    BLOCK[0x1fb7e] = [[0, 0, 1, e], [7 * e, e, e, 7 * e]];        /* right and upper */
    BLOCK[0x1fb7f] = [[7 * e, 0, e, 7 * e], [0, 7 * e, 1, e]];    /* right and lower */
    BLOCK[0x1fb80] = [[0, 0, 1, e], [0, 7 * e, 1, e]];            /* upper and lower */
    BLOCK[0x1fb81] = [[0, 0, 1, e], [0, 2 * e, 1, e], [0, 4 * e, 1, e], [0, 7 * e, 1, e]];
    for (i = 0; i < 5; i++) {
      BLOCK[0x1fb82 + i] = [[0, 0, 1, parts[i] * e]];
      BLOCK[0x1fb87 + i] = [[1 - parts[i] * e, 0, parts[i] * e, 1]];
    }
    BLOCK[0x1fbe4] = [[0.25, 0, 0.5, 0.5]];       /* upper centre quarter */
    BLOCK[0x1fbe5] = [[0.25, 0.5, 0.5, 0.5]];     /* lower centre */
    BLOCK[0x1fbe6] = [[0, 0.25, 0.5, 0.5]];       /* middle left */
    BLOCK[0x1fbe7] = [[0.5, 0.25, 0.5, 0.5]];     /* middle right */
  }

  /* The four diagonal halves, U+25E2-25E5: lower right, lower left, upper
   * left, upper right, numbered as the Pi painter numbers them. Filled
   * out to the cell's own corners so that two side by side make one
   * slope with no seam. */
  var TRI = { 0x25e2: 0, 0x25e3: 1, 0x25e4: 2, 0x25e5: 3 };

  /* The rounded corners ╭ ╮ ╯ ╰, U+256D-2570, numbered as the Pi
   * painter numbers them: each a quarter ellipse centred on a corner of
   * the cell, from the middle of one edge to the middle of the next, so
   * it runs into the light lines on either side with no step. */
  var ARC = { 0x256d: 0, 0x256e: 1, 0x256f: 2, 0x2570: 3 };

  /* Shades are the full cell at reduced opacity, which keeps them even
   * where a glyph would dither into a pattern that aliases on a wall. */
  var SHADE = { 0x2591: 0.25, 0x2592: 0.5, 0x2593: 0.75 };

  /* Box drawing as arm weights [up, right, down, left]: 0 none, 1 light,
   * 2 heavy, 3 double. Rounded corners are arcs of their own (ARC);
   * dashed and diagonal lines are glyphs. */
  var BOX = {
    0x2500: [0, 1, 0, 1], 0x2501: [0, 2, 0, 2],
    0x2502: [1, 0, 1, 0], 0x2503: [2, 0, 2, 0],

    0x250c: [0, 1, 1, 0], 0x250d: [0, 2, 1, 0],
    0x250e: [0, 1, 2, 0], 0x250f: [0, 2, 2, 0],
    0x2510: [0, 0, 1, 1], 0x2511: [0, 0, 1, 2],
    0x2512: [0, 0, 2, 1], 0x2513: [0, 0, 2, 2],
    0x2514: [1, 1, 0, 0], 0x2515: [1, 2, 0, 0],
    0x2516: [2, 1, 0, 0], 0x2517: [2, 2, 0, 0],
    0x2518: [1, 0, 0, 1], 0x2519: [1, 0, 0, 2],
    0x251a: [2, 0, 0, 1], 0x251b: [2, 0, 0, 2],

    0x251c: [1, 1, 1, 0], 0x251d: [1, 2, 1, 0],
    0x251e: [2, 1, 1, 0], 0x251f: [1, 1, 2, 0],
    0x2520: [2, 1, 2, 0], 0x2521: [2, 2, 1, 0],
    0x2522: [1, 2, 2, 0], 0x2523: [2, 2, 2, 0],

    0x2524: [1, 0, 1, 1], 0x2525: [1, 0, 1, 2],
    0x2526: [2, 0, 1, 1], 0x2527: [1, 0, 2, 1],
    0x2528: [2, 0, 2, 1], 0x2529: [2, 0, 1, 2],
    0x252a: [1, 0, 2, 2], 0x252b: [2, 0, 2, 2],

    0x252c: [0, 1, 1, 1], 0x252d: [0, 1, 1, 2],
    0x252e: [0, 2, 1, 1], 0x252f: [0, 2, 1, 2],
    0x2530: [0, 1, 2, 1], 0x2531: [0, 1, 2, 2],
    0x2532: [0, 2, 2, 1], 0x2533: [0, 2, 2, 2],

    0x2534: [1, 1, 0, 1], 0x2535: [1, 1, 0, 2],
    0x2536: [1, 2, 0, 1], 0x2537: [1, 2, 0, 2],
    0x2538: [2, 1, 0, 1], 0x2539: [2, 1, 0, 2],
    0x253a: [2, 2, 0, 1], 0x253b: [2, 2, 0, 2],

    0x253c: [1, 1, 1, 1], 0x253d: [1, 1, 1, 2],
    0x253e: [1, 2, 1, 1], 0x253f: [1, 2, 1, 2],
    0x2540: [2, 1, 1, 1], 0x2541: [1, 1, 2, 1],
    0x2542: [2, 1, 2, 1], 0x2543: [2, 1, 1, 2],
    0x2544: [2, 2, 1, 1], 0x2545: [1, 1, 2, 2],
    0x2546: [1, 2, 2, 1], 0x2547: [2, 2, 1, 2],
    0x2548: [1, 2, 2, 2], 0x2549: [2, 1, 2, 2],
    0x254a: [2, 2, 2, 1], 0x254b: [2, 2, 2, 2],

    0x2550: [0, 3, 0, 3], 0x2551: [3, 0, 3, 0],
    0x2552: [0, 3, 1, 0], 0x2553: [0, 1, 3, 0],
    0x2554: [0, 3, 3, 0], 0x2555: [0, 0, 1, 3],
    0x2556: [0, 0, 3, 1], 0x2557: [0, 0, 3, 3],
    0x2558: [1, 3, 0, 0], 0x2559: [3, 1, 0, 0],
    0x255a: [3, 3, 0, 0], 0x255b: [1, 0, 0, 3],
    0x255c: [3, 0, 0, 1], 0x255d: [3, 0, 0, 3],
    0x255e: [1, 3, 1, 0], 0x255f: [3, 1, 3, 0],
    0x2560: [3, 3, 3, 0], 0x2561: [1, 0, 1, 3],
    0x2562: [3, 0, 3, 1], 0x2563: [3, 0, 3, 3],
    0x2564: [0, 3, 1, 3], 0x2565: [0, 1, 3, 1],
    0x2566: [0, 3, 3, 3], 0x2567: [1, 3, 0, 3],
    0x2568: [3, 1, 0, 1], 0x2569: [3, 3, 0, 3],
    0x256a: [1, 3, 1, 3], 0x256b: [3, 1, 3, 1],
    0x256c: [3, 3, 3, 3],

    0x2574: [0, 0, 0, 1], 0x2575: [1, 0, 0, 0],
    0x2576: [0, 1, 0, 0], 0x2577: [0, 0, 1, 0],
    0x2578: [0, 0, 0, 2], 0x2579: [2, 0, 0, 0],
    0x257a: [0, 2, 0, 0], 0x257b: [0, 0, 2, 0],
    0x257c: [0, 2, 0, 1], 0x257d: [1, 0, 2, 0],
    0x257e: [0, 1, 0, 2], 0x257f: [2, 0, 1, 0]
  };

  /* Where an arm's lines lie across it, in a cell `len` pixels that way:
   * one [lo, hi] for a light or heavy arm, two for a double. The Pi
   * painter's strokes(), so both receivers draw a double line alike. */
  function strokes(weight, len, light, heavy) {
    var t = weight === 2 ? heavy : light, gap = light;
    var lo = Math.max(0, Math.floor((len - (weight === 3 ? 2 * t + gap : t)) / 2));
    if (weight !== 3) return [[lo, Math.min(len, lo + t)]];
    return [[lo, Math.min(len, lo + t)],
            [lo + t + gap, Math.min(len, lo + 2 * t + gap)]];
  }

  /* How far one line of an arm reaches into the middle, as the painter's
   * reach(): a single line stops across whatever crosses it; one of a
   * double pair turns into the nearest line on its own side, runs on if
   * the arm continues, or else turns into the far line of the arm
   * opposite, which is the outside of a corner. */
  function reach(a, arm, side, other, n, len, light, heavy, far) {
    var ahead = (arm + 2) % 4, mid = Math.floor(len / 2), sp, best = null;
    if (n === 1) {
      [side, other].forEach(function (s) {
        if (!a[s]) return;
        strokes(a[s], len, light, heavy).forEach(function (p) {
          var v = far ? p[1] : p[0];
          if (best === null || (far ? v > best : v < best)) best = v;
        });
      });
      if (best !== null) return best;
      if (a[ahead]) return mid;
      sp = strokes(a[arm], len, light, heavy);
      return far ? sp[0][1] : sp[0][0];
    }
    if (a[side]) {
      sp = strokes(a[side], len, light, heavy);
      return far ? sp[0][1] : sp[sp.length - 1][0];
    }
    if (a[ahead]) return mid;
    if (a[other]) {
      sp = strokes(a[other], len, light, heavy);
      return far ? sp[sp.length - 1][1] : sp[0][0];
    }
    return mid;
  }

  /* Key names, as the receivers agree to say them.
   *
   * A sender is told what was pressed, never a terminal sequence, so the
   * name has to mean the same thing whichever receiver saw it. These are
   * the ones a browser spells differently from the rest. */
  var KEYNAME = {
    'Enter': 'Return', 'Backspace': 'BackSpace', ' ': 'space',
    'ArrowUp': 'Up', 'ArrowDown': 'Down',
    'ArrowLeft': 'Left', 'ArrowRight': 'Right',
    'Delete': 'Delete', 'Escape': 'Escape', 'Tab': 'Tab'
  };

  /* "Mon 15 Sep  20:11:33", as every receiver's status page writes it. */
  var DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function clock() {
    var d = new Date(), p = function (n) { return ('0' + n).slice(-2); };
    return DAYS[d.getDay()] + ' ' + p(d.getDate()) + ' ' +
      MONTHS[d.getMonth()] + '  ' + p(d.getHours()) + ':' +
      p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function isGeometry(cp) {
    return BLOCK[cp] !== undefined || SHADE[cp] !== undefined ||
      BOX[cp] !== undefined || TRI[cp] !== undefined || ARC[cp] !== undefined;
  }

  /* Double-width: the sender writes one cell and skips the next, so the
   * cursor has to step two. The ranges that matter for a wall. */
  function wide(cp) {
    return (cp >= 0x1100 && cp <= 0x115f) ||
      (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
      (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) ||
      (cp >= 0xfe30 && cp <= 0xfe6f) ||
      (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6) ||
      (cp >= 0x1f300 && cp <= 0x1f64f) ||
      (cp >= 0x1f900 && cp <= 0x1f9ff);
  }

  /* -------------------------------------------------------------- grid */

  /* The cells, as parallel typed arrays.
   *
   * One array per property rather than an object per cell: a wall is tens
   * of thousands of cells repainted many times a second, and the parser
   * writes one property at a time. */
  function Grid(cols, rows) {
    this.resize(cols, rows);
  }

  Grid.prototype.resize = function (cols, rows) {
    var n = cols * rows;
    this.cols = cols;
    this.rows = rows;
    this.ch = new Uint32Array(n);
    this.fg = new Int32Array(n);
    this.bg = new Int32Array(n);
    this.at = new Uint8Array(n);
    /* each colour's alpha: 255 but on a cells layer above the base */
    this.fa = new Uint8Array(n);
    this.ba = new Uint8Array(n);
    this.dirty = new Uint8Array(rows);
    this.clear();
  };

  Grid.prototype.clear = function () {
    this.ch.fill(32);
    this.fg.fill(DEFAULT);
    this.bg.fill(DEFAULT);
    this.at.fill(0);
    this.fa.fill(255);
    this.ba.fill(255);
    this.dirty.fill(1);
  };

  /* ------------------------------------------------------------ parser */

  /* The stream into the grid.
   *
   * Everything the wire carries is a cursor move, an attribute set, or
   * text. An escape sequence can be cut in half by the pipe, so anything
   * incomplete at the end of a chunk is held over. */
  function Parser(grid) {
    this.grid = grid;
    this.dec = new TextDecoder('utf-8');
    this.rest = '';
    this.reset();
  }

  Parser.prototype.reset = function () {
    this.x = 0;
    this.y = 0;
    this.fg = DEFAULT;
    this.bg = DEFAULT;
    this.at = 0;
    this.fa = this.ba = 255;
    //: Frames that have arrived whole. A bare reset -- ESC[0m with no
    //: other parameter -- closes a frame and opens a full repaint, and
    //: is the only place either appears, so it is where the picture is
    //: consistent and the only place worth painting from.
    this.frames = 0;
  };

  /* Start counting which cells have been written.
   *
   * A full repaint writes every cell and a diff writes the few that
   * changed, so a grid is known to be complete exactly when the count
   * reaches every cell -- which is the test for whether what is on the
   * canvas can be shown. */
  Parser.prototype.cover = function () {
    this.seen = new Uint8Array(this.grid.cols * this.grid.rows);
    this.covered = 0;
  };

  Parser.prototype.uncover = function () {
    this.seen = null;
  };

  Parser.prototype.feed = function (bytes) {
    var s = this.rest + this.dec.decode(bytes, { stream: true });
    this.rest = '';
    var g = this.grid, cols = g.cols, rows = g.rows;
    var i = 0, n = s.length;

    while (i < n) {
      var c = s.charCodeAt(i);

      if (c === 27) {                                  /* ESC */
        if (i + 1 >= n) { this.rest = s.slice(i); break; }
        if (s.charCodeAt(i + 1) !== 91) { i += 2; continue; }   /* not CSI */
        /* CSI: parameters, then one final byte */
        var j = i + 2;
        while (j < n) {
          var p = s.charCodeAt(j);
          if (p >= 0x40 && p <= 0x7e) break;            /* final */
          j++;
        }
        if (j >= n) { this.rest = s.slice(i); break; }  /* cut in half */
        this.csi(s.slice(i + 2, j), s.charCodeAt(j));
        i = j + 1;
        continue;
      }

      if (c < 32) { i++; continue; }        /* the wire carries no controls */

      /* A run of plain text, written straight into the row. */
      if (this.y >= 0 && this.y < rows) {
        var base = this.y * cols;
        var touched = false;
        while (i < n) {
          c = s.charCodeAt(i);
          if (c < 32) break;
          var cp = c, step = 1;
          if (c >= 0xd800 && c <= 0xdbff && i + 1 < n) {
            cp = s.codePointAt(i);
            step = 2;
          }
          if (this.x >= cols) break;
          var k = base + this.x;
          if (g.ch[k] !== cp || g.fg[k] !== this.fg ||
              g.bg[k] !== this.bg || g.at[k] !== this.at ||
              g.fa[k] !== this.fa || g.ba[k] !== this.ba) {
            g.ch[k] = cp;
            g.fg[k] = this.fg;
            g.bg[k] = this.bg;
            g.at[k] = this.at;
            g.fa[k] = this.fa;
            g.ba[k] = this.ba;
            touched = true;
          }
          var w2 = wide(cp) ? 2 : 1;
          if (this.seen) {
            /* The trailing half of a wide glyph is never written, so it
             * is covered by the glyph that owns it. */
            for (var q = 0; q < w2 && this.x + q < cols; q++) {
              if (!this.seen[k + q]) { this.seen[k + q] = 1; this.covered++; }
            }
          }
          this.x += w2;
          i += step;
        }
        if (touched) g.dirty[this.y] = 1;
        /* Past the right edge: the sender repositions before it writes
         * again, so the rest of this run has nowhere to go. */
        while (i < n && s.charCodeAt(i) >= 32 && this.x >= cols) i++;
      } else {
        while (i < n && s.charCodeAt(i) >= 32) i++;
      }
    }
  };

  Parser.prototype.csi = function (params, final) {
    if (final === 72) {                                /* H: cursor home */
      var a = params.split(';');
      this.y = (parseInt(a[0], 10) || 1) - 1;
      this.x = (parseInt(a[1], 10) || 1) - 1;
      if (this.x < 0) this.x = 0;
      return;
    }
    if (final === 74 && this.layer) {                  /* J: a layer's clear */
      this.grid.clear();
      return;
    }
    if (final !== 109) return;                         /* m: attributes */

    if (params === '0' || params === '') this.frames++;

    var ps = params.length ? params.split(';') : ['0'];
    for (var i = 0; i < ps.length; i++) {
      var v = parseInt(ps[i], 10) || 0;
      if (v === 0) { this.fg = DEFAULT; this.bg = DEFAULT; this.at = 0; this.fa = this.ba = 255; }
      else if (v === 1) this.at |= BOLD;
      else if (v === 2) this.at |= FAINT;
      else if (v === 3) this.at |= ITALIC;
      else if (v === 4) this.at |= UNDER;
      else if (v === 7) this.at |= REVERSE;
      else if (v === 9) this.at |= STRIKE;
      else if (v === 22) this.at &= ~(BOLD | FAINT);
      else if (v === 23) this.at &= ~ITALIC;
      else if (v === 24) this.at &= ~UNDER;
      else if (v === 27) this.at &= ~REVERSE;
      else if (v === 29) this.at &= ~STRIKE;
      else if (v >= 30 && v <= 37) { this.fg = PALETTE[v - 30]; this.fa = 255; }
      else if (v >= 40 && v <= 47) { this.bg = PALETTE[v - 40]; this.ba = 255; }
      else if (v >= 90 && v <= 97) { this.fg = PALETTE[v - 90 + 8]; this.fa = 255; }
      else if (v >= 100 && v <= 107) { this.bg = PALETTE[v - 100 + 8]; this.ba = 255; }
      else if (v === 39) { this.fg = DEFAULT; this.fa = 255; }
      else if (v === 49) { this.bg = DEFAULT; this.ba = 255; }
      else if (v === 38 || v === 48) {
        var mode = parseInt(ps[i + 1], 10);
        var col = DEFAULT, alpha = 255;
        if (mode === 6) {
          /* a colour with an alpha, as a cells layer above the base sends it */
          col = ((parseInt(ps[i + 2], 10) & 255) << 16) |
                ((parseInt(ps[i + 3], 10) & 255) << 8) |
                (parseInt(ps[i + 4], 10) & 255);
          alpha = parseInt(ps[i + 5], 10) & 255;
          i += 5;
        } else if (mode === 5) {
          col = PALETTE[parseInt(ps[i + 2], 10) & 255];
          i += 2;
        } else if (mode === 2) {
          col = ((parseInt(ps[i + 2], 10) & 255) << 16) |
                ((parseInt(ps[i + 3], 10) & 255) << 8) |
                (parseInt(ps[i + 4], 10) & 255);
          i += 4;
        } else { i += 1; }
        if (v === 38) { this.fg = col; this.fa = alpha; }
        else { this.bg = col; this.ba = alpha; }
      }
    }
  };

  /* ------------------------------------------------------------- QOI */

  /* QOI, the one codec a video layer's frames and a look at the glass are
   * in: a few lines each way, alpha kept exactly. */
  function qoiDecode(b) {
    if (b.length < 22 || b[0] !== 0x71 || b[1] !== 0x6f || b[2] !== 0x69 || b[3] !== 0x66)
      return null;
    var w = ((b[4] << 24) | (b[5] << 16) | (b[6] << 8) | b[7]) >>> 0;
    var h = ((b[8] << 24) | (b[9] << 16) | (b[10] << 8) | b[11]) >>> 0;
    if (!w || !h || w > 4096 || h > 4096) return null;
    var n = w * h * 4, out = new Uint8ClampedArray(n), index = new Uint8Array(256);
    var r = 0, g = 0, bl = 0, a = 255, p = 14, run = 0, end = b.length - 8, i, op, k;
    for (i = 0; i < n; i += 4) {
      if (run > 0) {
        run--;
      } else if (p < end) {
        op = b[p++];
        if (op === 0xfe) {
          r = b[p++]; g = b[p++]; bl = b[p++];
        } else if (op === 0xff) {
          r = b[p++]; g = b[p++]; bl = b[p++]; a = b[p++];
        } else if ((op & 0xc0) === 0x00) {
          k = op * 4;
          r = index[k]; g = index[k + 1]; bl = index[k + 2]; a = index[k + 3];
        } else if ((op & 0xc0) === 0x40) {
          r = (r + ((op >> 4) & 3) - 2) & 255;
          g = (g + ((op >> 2) & 3) - 2) & 255;
          bl = (bl + (op & 3) - 2) & 255;
        } else if ((op & 0xc0) === 0x80) {
          var o2 = b[p++], vg = (op & 63) - 32;
          r = (r + vg - 8 + ((o2 >> 4) & 15)) & 255;
          g = (g + vg) & 255;
          bl = (bl + vg - 8 + (o2 & 15)) & 255;
        } else {
          run = op & 63;
        }
        k = ((r * 3 + g * 5 + bl * 7 + a * 11) % 64) * 4;
        index[k] = r; index[k + 1] = g; index[k + 2] = bl; index[k + 3] = a;
      }
      out[i] = r; out[i + 1] = g; out[i + 2] = bl; out[i + 3] = a;
    }
    return { w: w, h: h, data: out };
  }

  function qoiEncode(px, w, h) {
    var out = new Uint8Array(w * h * 5 + 22), index = new Uint8Array(256);
    var p = 0, n = w * h * 4, last = n - 4, i, k, run = 0;
    var pr = 0, pg = 0, pb = 0, pa = 255, r, g, bl, a;
    out.set([0x71, 0x6f, 0x69, 0x66,
             (w >>> 24) & 255, (w >>> 16) & 255, (w >>> 8) & 255, w & 255,
             (h >>> 24) & 255, (h >>> 16) & 255, (h >>> 8) & 255, h & 255, 4, 0]);
    p = 14;
    for (i = 0; i < n; i += 4) {
      r = px[i]; g = px[i + 1]; bl = px[i + 2]; a = px[i + 3];
      if (r === pr && g === pg && bl === pb && a === pa) {
        run++;
        if (run === 62 || i === last) { out[p++] = 0xc0 | (run - 1); run = 0; }
        continue;
      }
      if (run > 0) { out[p++] = 0xc0 | (run - 1); run = 0; }
      k = ((r * 3 + g * 5 + bl * 7 + a * 11) % 64) * 4;
      if (index[k] === r && index[k + 1] === g && index[k + 2] === bl && index[k + 3] === a) {
        out[p++] = k >> 2;
      } else {
        index[k] = r; index[k + 1] = g; index[k + 2] = bl; index[k + 3] = a;
        if (a === pa) {
          var vr = ((r - pr) << 24) >> 24, vg = ((g - pg) << 24) >> 24,
              vb = ((bl - pb) << 24) >> 24, vgr = vr - vg, vgb = vb - vg;
          if (vr > -3 && vr < 2 && vg > -3 && vg < 2 && vb > -3 && vb < 2) {
            out[p++] = 0x40 | ((vr + 2) << 4) | ((vg + 2) << 2) | (vb + 2);
          } else if (vgr > -9 && vgr < 8 && vg > -33 && vg < 32 && vgb > -9 && vgb < 8) {
            out[p++] = 0x80 | (vg + 32);
            out[p++] = ((vgr + 8) << 4) | (vgb + 8);
          } else {
            out[p++] = 0xfe; out[p++] = r; out[p++] = g; out[p++] = bl;
          }
        } else {
          out[p++] = 0xff; out[p++] = r; out[p++] = g; out[p++] = bl; out[p++] = a;
        }
      }
      pr = r; pg = g; pb = bl; pa = a;
    }
    out.set([0, 0, 0, 0, 0, 0, 0, 1], p);
    return out.subarray(0, p + 8);
  }

  /* The tables and the grid, for the element below. */
  window.Bitwall = {
    Parser: Parser, Grid: Grid, css: css, isGeometry: isGeometry,
    BLOCK: BLOCK, SHADE: SHADE, BOX: BOX, TRI: TRI, ARC: ARC, KEYNAME: KEYNAME, clock: clock,
    strokes: strokes, reach: reach, qoiDecode: qoiDecode, qoiEncode: qoiEncode
  };
})();

/* ----------------------------------------------------------- the element */

(function () {
  'use strict';

  var B = window.Bitwall;
  var Grid = B.Grid, Parser = B.Parser;
  var CSS = B.css, isGeometry = B.isGeometry;
  var BLOCK = B.BLOCK, SHADE = B.SHADE, BOX = B.BOX, TRI_WHICH = B.TRI, KEYNAME = B.KEYNAME;
  var ARC_WHICH = B.ARC;
  var clock = B.clock, strokes = B.strokes, reach = B.reach;
  var DEFAULT_FONT = 14, FONT_LOW = 4, FONT_HIGH = 400;
  /* The layers a wall stacks, the base included, and how long a resize has
   * to settle before the box is said: a dragged corner costs one repaint. */
  var LAYERS = 8, SETTLE = 300;
  var BOLD = 1, ITALIC = 2, UNDER = 4, STRIKE = 8, REVERSE = 16, FAINT = 32;
  var DEFAULT = -1;

  /* Braille marked faint: the pattern's unlit dots are shown as well, in
   * the cell's ground, with the wall's own black between the dots. */
  function ghosted(cp, at) {
    return (at & FAINT) !== 0 && cp >= 0x2800 && cp <= 0x28ff;
  }

  var FAMILY = '"bitwall wall", "MesloLGS Nerd Font Mono", Menlo, ' +
    'ui-monospace, Monaco, "DejaVu Sans Mono", monospace';

  /* The theme. A wall is dark because it is a wall. */
  var PAPER = '#000000', INK = '#d0d0d0';

  /* A layer's colour as canvas wants it: '#rrggbb', or rgba() where it
   * carries an alpha short of whole; null where there is nothing to draw. */
  var RGBA = Object.create(null);
  function tint(v, a) {
    if (a === 0) return null;
    if (a === 255) return CSS(v);
    var key = v * 256 + a, s = RGBA[key];
    if (s === undefined) {
      s = RGBA[key] = 'rgba(' + (v >> 16 & 255) + ',' + (v >> 8 & 255) + ',' +
        (v & 255) + ',' + (a / 255).toFixed(3) + ')';
    }
    return s;
  }

  function layerCanvas(z) {
    var c = document.createElement('canvas');
    c.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;' +
      'display:block;pointer-events:none;object-fit:contain;z-index:' + z;
    return c;
  }

  var proto = {
    /* -- lifecycle --------------------------------------------------- */

    init: function () {
      if (getComputedStyle(this).position === 'static') this.style.position = 'relative';
      this.style.overflow = 'hidden';
      /* The stack: the base's cells, a canvas for each layer above it, the
       * receiver's own overlay, and a cover for when the wall is dark. */
      this.canvas = layerCanvas(0);
      this.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d', { alpha: false });
      this.layers = [];
      this.shown = [];
      for (var z = 0; z < LAYERS; z++) {
        var c = layerCanvas(z);
        this.appendChild(c);
        this.layers.push(c);
        this.shown.push(false);
      }
      this.over = layerCanvas(LAYERS);
      this.appendChild(this.over);
      this.octx = this.over.getContext('2d');
      this.cover = document.createElement('div');
      this.cover.style.cssText = 'position:absolute;inset:0;background:#000;display:none;' +
        'z-index:' + (LAYERS + 1);
      this.appendChild(this.cover);
      this._mctx = document.createElement('canvas').getContext('2d');

      this.grid = new Grid(80, 24);
      this.parser = new Parser(this.grid);
      /* The cells layers above the base, by z: each a grid and a parser of
       * its own, at a font of its own, painted over what lies beneath with
       * every cell it has not drawn left clear. */
      this.cl = [];
      this._lcells = 0;
      /* The films playing, by z, the next binary message's film frame,
       * and what each layer showed and let go since the last second. */
      this.films = [];
      this._vchunk = null;
      this.vshown = []; this.vlate = [];
      this.codecs = null;
      this.codecsHw = [];
      this._pump = 0;
      this._zoomMult = 1;
      this.cols = 0; this.rows = 0;
      this.fontPx = 0; this.cw = 0; this.chh = 0; this.baseline = 0;
      this.ox = 0; this.oy = 0;
      this.dpr = 0; this.boxW = 0; this.boxH = 0;
      this.state = null;
      this.decider = false;
      this.held = true;        /* the status page, until there is a sender */
      this.frames = 0; this.bytes = 0; this.parseMs = 0;
      this.painted = 0; this.paintMs = 0;
      this.puts = 0; this.decodeMs = []; this.fseq = [];
      this._raf = 0;
      this._guard = 0;
      this._waiting = null;
      this._waitTimer = 0;
      this._settle = 0;
      this._seenFrames = 0;
      this._frame = null;
      /* The base's kitty graphics: each picture by the wall's key, its
       * bitmap once decoded, and the placements in the order drawn. */
      this.kimgs = {};
      this.kplaces = [];
      this._kimg = null;
      this._kdecoding = 0;
      this._kwaiting = [];
      this.overlay = null;
      this.fps = 0; this._since = 0;
      this.rate = 0; this._rateWas = 0;
      this._statsAt = performance.now();

      var self = this;
      this.measureAll();
      this._ro = new ResizeObserver(function () { self.resized(); });
      this._ro.observe(this);
      if (document.fonts && document.fonts.addEventListener) {
        document.fonts.addEventListener('loadingdone', function () {
          self.measureAll();
          self.sendBox();
          self.layout();
        });
      }
      // Bound whatever the sender has asked for: what a press is worth is
      // decided when it happens, so a sender can turn either on and off
      // while the wall is up.
      this.addEventListener('pointerdown', function (e) { self.onPress(e); });
      window.addEventListener('keydown', function (e) { self.onKey(e); });
      document.addEventListener('visibilitychange', function () { self.presence(); });
      this._tick = setInterval(function () { self.second(); }, 1000);
      this.connect();
    },

    /* -- measuring --------------------------------------------------- */

    measure: function (px) {
      var ctx = this._mctx;
      ctx.font = px + 'px ' + FAMILY;
      var m = ctx.measureText('MMMMMMMMMM');
      var cw = (m.width / 10) || px * 0.6;
      var asc, desc;
      if (typeof m.fontBoundingBoxAscent === 'number' &&
          m.fontBoundingBoxAscent > 0) {
        asc = m.fontBoundingBoxAscent; desc = m.fontBoundingBoxDescent;
      } else {
        asc = px * 0.94; desc = px * 0.24;
      }
      return { cw: cw, ch: Math.max(1, asc + desc), baseline: asc };
    },

    /* A cell at every font the wall has, in whole device pixels, an even
     * number across and a multiple of four down, so the halves of a block,
     * the quadrants and every braille dot fall on the pixel grid at even
     * spacing. The wall works out its area from this, as the painter does
     * from its font, and the page lays out with the same numbers, so the
     * two agree. */
    measureAll: function () {
      var dpr = window.devicePixelRatio || 1, table = [], f;
      for (f = FONT_LOW; f <= FONT_HIGH; f++) {
        var m = this.measure(f);
        var w = Math.max(2, Math.ceil(m.cw * dpr - 0.01));
        var h = Math.max(4, Math.ceil(m.ch * dpr - 0.01));
        w += w % 2;
        h += (4 - h % 4) % 4;
        table.push([w, h]);
      }
      this.table = table;
      this.tableDpr = dpr;
    },

    /* The box, as the glass: whole device pixels. */
    box: function () {
      var dpr = window.devicePixelRatio || 1;
      return [Math.round(this.clientWidth * dpr), Math.round(this.clientHeight * dpr), dpr];
    },

    /* The box has changed. The canvases stretch with it until it settles,
     * and then the wall is told once, so a dragged corner is one resize. */
    resized: function () {
      var self = this;
      if (this._settle) clearTimeout(this._settle);
      this._settle = setTimeout(function () {
        self._settle = 0;
        if ((window.devicePixelRatio || 1) !== self.tableDpr) self.measureAll();
        self.sendBox();
        self.layout();
      }, this.cols ? SETTLE : 0);
    },

    sendBox: function () {
      if (!this.ws || this.ws.readyState !== 1) return;
      var b = this.box();
      if (!b[0] || !b[1]) return;
      var gc = parseInt(this.getAttribute('cols'), 10) || 0;
      var gr = parseInt(this.getAttribute('rows'), 10) || 0;
      var font = parseInt(this.getAttribute('font-size'), 10) || 0;
      var msg = { t: 'box', w: b[0], h: b[1], dpr: b[2], cells: this.table,
                  zoom: this._zoomMult, primary: this.hasAttribute('primary'),
                  can: { touch: this.hasAttribute('touch'),
                         keyboard: this.hasAttribute('keyboard') } };
      if (gc > 0 && gr > 0) msg.fixed = [gc, gr];
      if (font >= FONT_LOW && font <= FONT_HIGH) msg.font = font;
      this.ws.send(JSON.stringify(msg));
    },

    /* The grid the wall says, laid in this box. The tab that decides the
     * wall draws at the wall's font, so its cells are the ones the wall
     * counted; any other tab fits the same grid into its own box at the
     * largest font that fits. Layers are the wall's glass in pixels, the
     * deciding tab's box, and fitted likewise. */
    layout: function () {
      var s = this.state, b = this.box();
      if (!s || !b[0] || !b[1] || !this.table) return;
      var Wd = b[0], Hd = b[1], dpr = b[2], cols = s.cols, rows = s.rows, f, cell;
      if (this.decider && s.font >= FONT_LOW && s.font <= FONT_HIGH) {
        f = s.font;
      } else {
        for (f = FONT_HIGH; f > FONT_LOW; f--) {
          cell = this.table[f - FONT_LOW];
          if (cols * cell[0] <= Wd && rows * cell[1] <= Hd) break;
        }
      }
      cell = this.table[f - FONT_LOW];
      var m = this.measure(f);
      var moved = cols !== this.cols || rows !== this.rows;
      if (!moved && f === this.fontPx && Wd === this.boxW * this.dpr &&
          Hd === this.boxH * this.dpr && dpr === this.dpr) return;
      this.dpr = dpr;
      this.boxW = Wd / dpr; this.boxH = Hd / dpr;
      this.fontPx = f;
      this.cw = cell[0] / dpr; this.chh = cell[1] / dpr;
      this.baseline = m.baseline + (this.chh - m.ch) / 2;
      this.ox = Math.floor((Wd - cols * cell[0]) / 2) / dpr;
      this.oy = Math.floor((Hd - rows * cell[1]) / 2) / dpr;
      this.canvas.width = Wd; this.canvas.height = Hd;
      this.over.width = Wd; this.over.height = Hd;
      var ctx = this.ctx;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.textBaseline = 'alphabetic';
      this._overlayBox = null;
      if (moved) {
        this.cols = cols; this.rows = rows;
        this.grid.resize(cols, rows);
        this.parser.reset();
        this._seenFrames = 0;
      }
      this.glass();
      this.wipe();
      this.grid.dirty.fill(1);
      if (this.held) this.status(this._waiting || 'no sender connected');
      this.schedule();
    },

    /* The layers' canvases as big as the wall's glass. A new size is a
     * new glass, and whatever they held is gone with it. */
    glass: function () {
      var s = this.state, b = this.box();
      var px = s && s.pixels ? s.pixels : [b[0], b[1]];
      for (var z = 0; z < LAYERS; z++) {
        var c = this.layers[z];
        if (c.width !== px[0] || c.height !== px[1]) {
          c.width = px[0]; c.height = px[1];
          this.shown[z] = false;
          if (this.cl[z]) { this.cl[z].grid.dirty.fill(1); this.shown[z] = true; }
        }
      }
    },

    /* -- the picture ------------------------------------------------- */

    wipe: function () {
      var ctx = this.ctx;
      ctx.fillStyle = PAPER;
      ctx.fillRect(0, 0, this.boxW, this.boxH);
    },

    schedule: function () {
      if (this._guard) { clearTimeout(this._guard); this._guard = 0; }
      if (this._raf) return;
      var self = this;
      this._raf = requestAnimationFrame(function () {
        self._raf = 0;
        self.paint();
      });
    },

    /* Paint anyway if a frame never closes.
     *
     * Painting on frame boundaries is what stops a repaint appearing as a
     * band sweeping down the wall, but a sender that stops mid-frame
     * would leave the last of its picture unpainted for good. */
    guard: function () {
      if (this._guard || this._raf) return;
      var self = this;
      this._guard = setTimeout(function () {
        self._guard = 0;
        self.schedule();
      }, 250);
    },

    /* Paint the rows that changed since the last animation frame.
     *
     * Runs, not cells: a row is walked once for backgrounds and once for
     * ink, and each pass draws the longest stretch that shares a colour.
     * A run of ASCII is one fillText, because the font is monospaced and
     * every advance in it is the cell. Anything else is drawn on its own,
     * where a fallback font's advance cannot drag the run out of line. */
    paint: function () {
      this.paintBase();
      for (var z = 1; z < LAYERS; z++) if (this.cl[z]) this.paintLayer(z);
    },

    paintBase: function () {
      var g = this.grid, ctx = this.ctx;
      if (this.held || !this.cols) return;
      var t0 = performance.now();
      var cols = this.cols, rows = this.rows;
      var cw = this.cw, ch = this.chh, ox = this.ox, oy = this.oy;
      var y, x, k, base, rowY, n = 0;

      ctx.font = this.fontPx + 'px ' + FAMILY;
      for (y = 0; y < rows; y++) {
        if (!g.dirty[y]) continue;
        g.dirty[y] = 0;
        n++;
        base = y * cols;
        rowY = oy + y * ch;

        /* backgrounds */
        x = 0;
        var kp = this.kplaces;
        while (x < cols) {
          k = base + x;
          var bg = ghosted(g.ch[k], g.at[k]) ? PAPER : (g.at[k] & REVERSE)
            ? (g.fg[k] === DEFAULT ? INK : CSS(g.fg[k]))
            : (g.bg[k] === DEFAULT ? PAPER : CSS(g.bg[k]));
          var run = x + 1;
          while (run < cols) {
            var kk = base + run;
            var b2 = ghosted(g.ch[kk], g.at[kk]) ? PAPER : (g.at[kk] & REVERSE)
              ? (g.fg[kk] === DEFAULT ? INK : CSS(g.fg[kk]))
              : (g.bg[kk] === DEFAULT ? PAPER : CSS(g.bg[kk]));
            if (b2 !== bg) break;
            run++;
          }
          ctx.fillStyle = bg;
          this.snapped(ox + x * cw, rowY, ox + run * cw, rowY + ch);
          x = run;
        }
        if (kp.length) this.kittyRow(y, true);

        /* ink */
        x = 0;
        while (x < cols) {
          k = base + x;
          var cp = g.ch[k];
          if (cp === 32 || cp === 0) { x++; continue; }
          var at = g.at[k];
          var fg = (at & REVERSE)
            ? (g.bg[k] === DEFAULT ? PAPER : CSS(g.bg[k]))
            : (g.fg[k] === DEFAULT ? INK : CSS(g.fg[k]));

          if (ghosted(cp, at)) {
            var dim = (at & REVERSE)
              ? (g.fg[k] === DEFAULT ? INK : CSS(g.fg[k]))
              : (g.bg[k] === DEFAULT ? PAPER : CSS(g.bg[k]));
            this.geometry(0x28ff, ox + x * cw, rowY, cw, ch, dim);
            if (cp !== 0x2800) this.geometry(cp, ox + x * cw, rowY, cw, ch, fg);
            x++;
            continue;
          }

          if (isGeometry(cp)) {
            this.geometry(cp, ox + x * cw, rowY, cw, ch, fg);
            x++;
            continue;
          }

          if (cp > 126) {                 /* one glyph, on its own advance */
            ctx.fillStyle = fg;
            ctx.font = this.styled(at);
            ctx.fillText(String.fromCodePoint(cp), ox + x * cw,
                         rowY + this.baseline);
            this.decorate(at, ox + x * cw, rowY, cw, fg);
            x++;
            continue;
          }

          var chars = [], start = x;
          while (x < cols) {
            var kc = base + x, c = g.ch[kc];
            if (c < 33 || c > 126 || g.at[kc] !== at) break;
            var f2 = (at & REVERSE)
              ? (g.bg[kc] === DEFAULT ? PAPER : CSS(g.bg[kc]))
              : (g.fg[kc] === DEFAULT ? INK : CSS(g.fg[kc]));
            if (f2 !== fg) break;
            chars.push(String.fromCharCode(c));
            x++;
          }
          if (!chars.length) { x++; continue; }
          ctx.fillStyle = fg;
          ctx.font = this.styled(at);
          ctx.fillText(chars.join(''), ox + start * cw,
                       rowY + this.baseline);
          this.decorate(at, ox + start * cw, rowY,
                        (x - start) * cw, fg);
        }
        if (kp.length) this.kittyRow(y, false);
      }

      if (n) {
        this.painted++;
        this.paintMs += performance.now() - t0;
        this._since++;
      }
    },

    styled: function (at) {
      var f = '';
      if (at & ITALIC) f += 'italic ';
      if (at & BOLD) f += 'bold ';
      return f + this.fontPx + 'px ' + FAMILY;
    },

    decorate: function (at, x, y, w, fg) {
      if (!(at & (UNDER | STRIKE))) return;
      var ctx = this.ctx, t = Math.max(1, Math.round(this.fontPx / 14));
      ctx.fillStyle = fg;
      if (at & UNDER) ctx.fillRect(x, y + this.baseline + t, w, t);
      if (at & STRIKE)
        ctx.fillRect(x, y + this.baseline - this.fontPx * 0.3, w, t);
    },

    /* A rectangle filled out to whole device pixels, its edges rounded
     * each on its own, so two that share an edge share the pixel it falls
     * in and neither blurs it: blocks, grounds and diagonal halves meet
     * with no seam and no soft line between them. */
    snapped: function (x0, y0, x1, y1) {
      var ctx = this.ctx, dpr = this.dpr || 1;
      var X0 = Math.round(x0 * dpr), X1 = Math.round(x1 * dpr);
      var Y0 = Math.round(y0 * dpr), Y1 = Math.round(y1 * dpr);
      if (X1 <= X0 || Y1 <= Y0) return;
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillRect(X0, Y0, X1 - X0, Y1 - Y0);
      ctx.restore();
    },

    /* Blocks as rectangles and box characters as lines, so a wall of them
     * has no seam and a maze drawn in box characters has walls that join. */
    geometry: function (cp, x, y, w, h, fg) {
      var ctx = this.ctx, r, i, rects = BLOCK[cp];
      if (cp > 0x2800 && cp < 0x2900) {
        /* Braille's dots all one whole number of pixels, centred in their
         * slots, so a screen of them is an even grid rather than a ragged
         * one where each dot's edges were rounded on their own. */
        var dw = Math.max(1, Math.round(w * 0.3)), dh = Math.max(1, Math.round(h * 0.15));
        ctx.fillStyle = fg;
        for (i = 0; i < rects.length; i++) {
          r = rects[i];
          ctx.fillRect(Math.round(x + (r[0] + 0.15) * w) - (dw >> 1),
                       Math.round(y + (r[1] + 0.075) * h) - (dh >> 1), dw, dh);
        }
        return;
      }
      var which = TRI_WHICH[cp];
      if (which !== undefined) {
        /* In whole device pixels, a row at a time, each pixel taken by
         * its centre as the Pi painter's tri_in() takes it: a triangle and
         * the one completing its cell share every pixel, one each, and
         * nothing is blurred, so a slope of them has no seam. */
        var dpr = this.dpr || 1;
        var X0 = Math.round(x * dpr), X1 = Math.round((x + w) * dpr);
        var Y0 = Math.round(y * dpr), Y1 = Math.round((y + h) * dpr);
        var W = X1 - X0, H = Y1 - Y0, py, px, lo, hi, u, v, inside;
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = fg;
        for (py = 0; py < H; py++) {
          v = (py + 0.5) / H;
          lo = -1; hi = -1;
          for (px = 0; px < W; px++) {
            u = (px + 0.5) / W;
            inside = which === 0 ? u + v >= 1 : which === 1 ? v >= u
              : which === 2 ? u + v < 1 : v < u;
            if (inside) { if (lo < 0) lo = px; hi = px; }
          }
          if (lo >= 0) ctx.fillRect(X0 + lo, Y0 + py, hi - lo + 1, 1);
        }
        ctx.restore();
        return;
      }
      var arc = ARC_WHICH[cp];
      if (arc !== undefined) {
        /* ╭ down and right, ╮ down and left, ╯ up and left, ╰ up and
         * right: centred on that corner, as thick as a light line and
         * through the light lines' centres, which are the cell's. */
        var cxr = arc === 0 || arc === 3 ? x + w : x;
        var cyr = arc < 2 ? y + h : y;
        var from = [Math.PI, 1.5 * Math.PI, 0, 0.5 * Math.PI][arc];
        ctx.save();
        ctx.beginPath();
        ctx.rect(x, y, w, h);
        ctx.clip();
        ctx.beginPath();
        ctx.ellipse(cxr, cyr, w / 2, h / 2, 0, from, from + Math.PI / 2);
        ctx.lineWidth = Math.max(1, Math.round(this.fontPx / 12));
        ctx.strokeStyle = fg;
        ctx.stroke();
        ctx.restore();
        return;
      }
      if (rects) {
        ctx.fillStyle = fg;
        for (i = 0; i < rects.length; i++) {
          r = rects[i];
          this.snapped(x + r[0] * w, y + r[1] * h,
                       x + (r[0] + r[2]) * w, y + (r[1] + r[3]) * h);
        }
        return;
      }
      var a = SHADE[cp];
      if (a !== undefined) {
        ctx.save();
        ctx.globalAlpha = a;
        ctx.fillStyle = fg;
        ctx.fillRect(x, y, w + 0.5, h + 0.5);
        ctx.restore();
        return;
      }
      var arms = BOX[cp];
      if (!arms) return;
      var light = Math.max(1, Math.round(this.fontPx / 12));
      var heavy = Math.max(2, Math.round(this.fontPx / 5));
      if (arms.indexOf(3) >= 0) {
        this.doubled(arms, x, y, w, h, fg, light, heavy);
        return;
      }
      var cx = x + w / 2, cy = y + h / 2;
      ctx.fillStyle = fg;
      /* Each arm runs from the centre to its edge, and is as thick as its
       * weight. Drawing from the centre is what makes two cells meet. */
      var t;
      if (arms[0]) {                                   /* up */
        t = arms[0] === 2 ? heavy : light;
        ctx.fillRect(cx - t / 2, y, t, cy - y + (arms[2] ? 0 : t / 2));
      }
      if (arms[2]) {                                   /* down */
        t = arms[2] === 2 ? heavy : light;
        ctx.fillRect(cx - t / 2, cy - (arms[0] ? 0 : t / 2), t,
                     y + h - cy + (arms[0] ? 0 : t / 2));
      }
      if (arms[3]) {                                   /* left */
        t = arms[3] === 2 ? heavy : light;
        ctx.fillRect(x, cy - t / 2, cx - x + (arms[1] ? 0 : t / 2), t);
      }
      if (arms[1]) {                                   /* right */
        t = arms[1] === 2 ? heavy : light;
        ctx.fillRect(cx - (arms[3] ? 0 : t / 2), cy - t / 2,
                     x + w - cx + (arms[3] ? 0 : t / 2), t);
      }
      /* A junction of mixed weights leaves a notch at the centre; the
       * heaviest arm's square fills it. */
      var hv = Math.max(arms[0], arms[1], arms[2], arms[3]);
      t = hv === 2 ? heavy : light;
      ctx.fillRect(cx - t / 2, cy - t / 2, t, t);
    },

    /* A character with a double line in it, as the Pi painter's box_px()
     * draws it: each line of each arm from its edge in to where reach()
     * says it stops. */
    doubled: function (arms, x, y, w, h, fg, light, heavy) {
      var ctx = this.ctx, arm, j;
      ctx.fillStyle = fg;
      for (arm = 0; arm < 4; arm++) {
        if (!arms[arm]) continue;
        var vertical = arm === 0 || arm === 2, towards = arm === 0 || arm === 3;
        var first = vertical ? 3 : 0, second = vertical ? 1 : 2;
        var len = vertical ? h : w;
        var sp = strokes(arms[arm], vertical ? w : h, light, heavy);
        for (j = 0; j < sp.length; j++) {
          var side = j === 0 ? first : second, other = j === 0 ? second : first;
          var at = reach(arms, arm, side, other, sp.length, len, light, heavy, towards);
          var lo = towards ? 0 : at, hi = towards ? at : len;
          if (hi <= lo) continue;
          if (vertical) ctx.fillRect(x + sp[j][0], y + lo, sp[j][1] - sp[j][0], hi - lo);
          else ctx.fillRect(x + lo, y + sp[j][0], hi - lo, sp[j][1] - sp[j][0]);
        }
      }
    },

    /* -- kitty graphics in the base's cells ---------------------------- */

    /* A picture the wall sent: PNG decoded by the browser, raw RGB or RGBA
     * made into pixels, and in both cases a bitmap later. Every picture
     * has a key of its own, so one that finishes decoding after it was
     * replaced or deleted is let go rather than drawn. */
    kittyImage: function (h, bytes) {
      var key = h.key, self = this;
      if (this.kimgs[key]) return;
      var rec = this.kimgs[key] = { bmp: null, w: h.w | 0, h: h.h | 0 };
      var made;
      try {
        if (h.f === 100) {
          made = createImageBitmap(new Blob([bytes], { type: 'image/png' }));
        } else {
          var n = rec.w * rec.h, px = new Uint8ClampedArray(n * 4), i;
          if (h.f === 32) {
            px.set(bytes.subarray(0, n * 4));
          } else {
            for (i = 0; i < n; i++) {
              px[i * 4] = bytes[i * 3];
              px[i * 4 + 1] = bytes[i * 3 + 1];
              px[i * 4 + 2] = bytes[i * 3 + 2];
              px[i * 4 + 3] = 255;
            }
          }
          made = createImageBitmap(new ImageData(px, rec.w, rec.h));
        }
      } catch (e) {
        return;
      }
      this._kdecoding++;
      made.then(function (bmp) {
        if (self.kimgs[key] !== rec) { bmp.close(); return; }
        rec.bmp = bmp;
        self.kittyDirty(self.kplaces, key);
      }, function () {}).then(function () {
        self._kdecoding--;
        if (!self._kdecoding) {
          var w = self._kwaiting;
          self._kwaiting = [];
          w.forEach(function (f) { f(); });
        }
      });
    },

    kittyForget: function (key) {
      var rec = this.kimgs[key];
      if (!rec) return;
      delete this.kimgs[key];
      if (rec.bmp) rec.bmp.close();
      this.kittyDirty(this.kplaces, key);
    },

    kittyPlaces: function (list) {
      this.kittyDirty(this.kplaces);
      this.kplaces = Array.isArray(list) ? list : [];
      this.kittyDirty(this.kplaces);
    },

    kittyWipe: function () {
      for (var key in this.kimgs) if (this.kimgs[key].bmp) this.kimgs[key].bmp.close();
      this.kimgs = {};
      this.kittyDirty(this.kplaces);
      this.kplaces = [];
    },

    /* A placement on the glass, in CSS pixels, from the cell now: c and r
     * both given, stretched to fill them; one given, the other from the
     * picture's shape; neither, its own pixels. As the painter has it. */
    kittyBox: function (p) {
      var dpr = this.dpr || 1, cwd = Math.round(this.cw * dpr), chd = Math.round(this.chh * dpr);
      var c = p[3], r = p[4], iw = p[6], ih = p[7], w, h;
      if (c && r) { w = c * cwd; h = r * chd; }
      else if (c) { w = c * cwd; h = Math.floor(w * ih / iw + 0.5); }
      else if (r) { h = r * chd; w = Math.floor(h * iw / ih + 0.5); }
      else { w = iw; h = ih; }
      w = Math.max(1, w); h = Math.max(1, h);
      return { x: this.ox + p[1] * this.cw, y: this.oy + p[2] * this.chh,
               w: w / dpr, h: h / dpr, row0: p[2], row1: p[2] + Math.ceil(h / chd) - 1 };
    },

    /* The rows placements cover drawn again: all of them, or those of one
     * picture. */
    kittyDirty: function (list, key) {
      if (!this.cols) return;
      var d = this.grid.dirty, rows = this.rows;
      for (var i = 0; i < list.length; i++) {
        if (key !== undefined && list[i][0] !== key) continue;
        var b = this.kittyBox(list[i]);
        for (var y = Math.max(0, b.row0); y <= Math.min(rows - 1, b.row1); y++) d[y] = 1;
      }
      this.schedule();
    },

    /* The placements on one row as it is drawn, those below the text
     * between its ground and its ink, the rest over the ink: by z, then
     * as placed, which is the order the wall sends them in. Each is
     * clipped to the row, the top and bottom rows reaching to the glass's
     * edges, so a picture is drawn a row at a time as its rows are. */
    kittyRow: function (y, below) {
      var list = this.kplaces, ctx = this.ctx, clipped = false;
      for (var i = 0; i < list.length; i++) {
        var p = list[i];
        if ((p[5] < 0) !== below) continue;
        var rec = this.kimgs[p[0]];
        if (!rec || !rec.bmp) continue;
        var b = this.kittyBox(p);
        if (y < b.row0 || y > b.row1) continue;
        if (!clipped) {
          var top = y === 0 ? 0 : this.oy + y * this.chh;
          var bottom = y === this.rows - 1 ? this.boxH : this.oy + (y + 1) * this.chh;
          ctx.save();
          ctx.beginPath();
          ctx.rect(0, top, this.boxW, bottom - top);
          ctx.clip();
          clipped = true;
        }
        ctx.drawImage(rec.bmp, b.x, b.y, b.w, b.h);
      }
      if (clipped) ctx.restore();
    },

    /* -- a layer above the base --------------------------------------- */

    /* A video frame put up on its layer in place of the one before, at
     * once rather than waiting for the base: the browser stacks the
     * canvases, so a layer redraws the glass by itself. `w` and `h` are
     * the box it is drawn to. A QOI at its own size is put straight in;
     * anything else -- a PNG, a JPEG, a QOI scaled -- the browser decodes
     * and scales, out of order with what comes after, so each frame is
     * numbered on its layer and one that finishes decoding after a later
     * frame, or a clear, is let go. */
    putFrame: function (f, bytes) {
      var z = f.z | 0;
      if (z < 0 || z >= LAYERS) return;
      var t0 = performance.now(), self = this;
      var seq = this.fseq[z] = (this.fseq[z] || 0) + 1;
      var codec = f.codec || 'qoi', made, img = null;
      if (codec === 'qoi') {
        img = B.qoiDecode(bytes);
        if (!img) return;
        if (img.w === (f.w | 0) && img.h === (f.h | 0)) {
          var c = this.layers[z], x = c.getContext('2d');
          x.clearRect(0, 0, c.width, c.height);
          x.putImageData(new ImageData(img.data, img.w, img.h), f.x | 0, f.y | 0);
          this.framed(z, t0);
          return;
        }
      }
      try {
        made = img ? createImageBitmap(new ImageData(img.data, img.w, img.h))
          : createImageBitmap(new Blob([bytes], { type: 'image/' + codec }));
      } catch (e) {
        return;
      }
      made.then(function (bmp) {
        if (self.fseq[z] !== seq) { bmp.close(); return; }
        var c = self.layers[z], x = c.getContext('2d');
        x.clearRect(0, 0, c.width, c.height);
        x.imageSmoothingEnabled = true;
        x.imageSmoothingQuality = 'high';
        x.drawImage(bmp, f.x | 0, f.y | 0, f.w | 0, f.h | 0);
        bmp.close();
        self.framed(z, t0);
      }, function () {});
    },

    framed: function (z, t0) {
      this.shown[z] = true;
      var d = this.decodeMs[z] || (this.decodeMs[z] = [0, 0]);
      d[0] += performance.now() - t0; d[1]++;
      this.puts++;
      this._since++;
    },

    /* A cells layer at z, made, or made again at a new size or font, or
     * wiped. Kept where its size is the same, so a font that leaves the
     * grid as it was draws the same cells again. */
    setLayer: function (s) {
      var z = s.z | 0, cols = s.cols | 0, rows = s.rows | 0;
      if (z < 1 || z >= LAYERS || cols < 1 || rows < 1) return;
      var L = this.cl[z];
      if (!L) {
        this.clearLayer(z);
        var g = new Grid(cols, rows), p = new Parser(g);
        p.layer = true;
        L = this.cl[z] = { grid: g, parser: p, font: 0 };
      } else if (L.grid.cols !== cols || L.grid.rows !== rows) {
        L.grid.resize(cols, rows);
        L.parser.reset();
      } else if (s.wipe) {
        L.grid.clear();
      }
      L.font = Math.min(FONT_HIGH, Math.max(FONT_LOW, s.font | 0));
      L.grid.dirty.fill(1);
      L.whole = true;
      this.shown[z] = true;
      this.schedule();
    },

    dropLayer: function (z) {
      if (!this.cl[z]) return;
      this.cl[z] = null;
      this.clearLayer(z);
    },

    /* A cells layer's rows that changed, on its own canvas: its grid
     * centred on the glass by its own cell, each row cleared and only the
     * grounds and ink it drew put back, so what lies beneath shows through
     * every cell it left alone. The base's helpers draw it, given its
     * canvas and its font while they do. */
    paintLayer: function (z) {
      var L = this.cl[z], c = this.layers[z];
      if (!L || !c || !c.width || !this.table) return;
      var g = L.grid, cols = g.cols, rows = g.rows;
      var dpr = this.tableDpr || 1, cell = this.table[L.font - FONT_LOW];
      var m = this.measure(L.font);
      var cw = cell[0] / dpr, ch = cell[1] / dpr;
      var ox = Math.floor((c.width - cols * cell[0]) / 2) / dpr;
      var oy = Math.floor((c.height - rows * cell[1]) / 2) / dpr;
      var ctx = c.getContext('2d'), W = c.width / dpr;
      var keep = { ctx: this.ctx, fontPx: this.fontPx, baseline: this.baseline, dpr: this.dpr };
      this.ctx = ctx;
      this.fontPx = L.font;
      this.baseline = m.baseline + (ch - m.ch) / 2;
      this.dpr = dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.textBaseline = 'alphabetic';
      if (L.whole) {
        L.whole = false;
        ctx.clearRect(0, 0, W, c.height / dpr);
        g.dirty.fill(1);
      }
      var y, rowY, n = 0;
      for (y = 0; y < rows; y++) {
        if (!g.dirty[y]) continue;
        g.dirty[y] = 0;
        n++;
        rowY = oy + y * ch;
        ctx.clearRect(ox, rowY, cols * cw, ch);
        this.layerRow(g, y, ox, rowY, cw, ch);
      }
      this.ctx = keep.ctx;
      this.fontPx = keep.fontPx;
      this.baseline = keep.baseline;
      this.dpr = keep.dpr;
      if (n) this._since++;
    },

    /* One row of a cells layer: the grounds it set, then its ink. A cell
     * left in the default colours is transparent, and a colour with an
     * alpha is blended, as the layers stack. The control page draws a
     * wall's picture in cells with it too. */
    layerRow: function (g, y, ox, rowY, cw, ch) {
      var ctx = this.ctx, cols = g.cols, base = y * cols, x, k;
      /* grounds: none where it left the default, or reversed none */
      for (x = 0; x < cols; x++) {
        k = base + x;
        var rev = g.at[k] & REVERSE;
        var bg = ghosted(g.ch[k], g.at[k]) ? null : rev
          ? (g.fg[k] === DEFAULT ? INK : tint(g.fg[k], g.fa[k]))
          : (g.bg[k] === DEFAULT ? null : tint(g.bg[k], g.ba[k]));
        if (!bg) continue;
        ctx.fillStyle = bg;
        this.snapped(ox + x * cw, rowY, ox + (x + 1) * cw, rowY + ch);
      }
      /* ink */
      for (x = 0; x < cols; x++) {
        k = base + x;
        var cp = g.ch[k];
        if (cp === 32 || cp === 0) continue;
        var at = g.at[k];
        var fg = (at & REVERSE)
          ? (g.bg[k] === DEFAULT ? null : tint(g.bg[k], g.ba[k]))
          : (g.fg[k] === DEFAULT ? (g.fa[k] === 255 ? INK : tint(0xd0d0d0, g.fa[k]))
                                 : tint(g.fg[k], g.fa[k]));
        if (ghosted(cp, at)) {
          var dim = g.bg[k] === DEFAULT ? null : tint(g.bg[k], g.ba[k]);
          if (dim) this.geometry(0x28ff, ox + x * cw, rowY, cw, ch, dim);
          if (fg && cp !== 0x2800) this.geometry(cp, ox + x * cw, rowY, cw, ch, fg);
          continue;
        }
        if (!fg) continue;
        if (isGeometry(cp)) {
          this.geometry(cp, ox + x * cw, rowY, cw, ch, fg);
          continue;
        }
        ctx.fillStyle = fg;
        ctx.font = this.styled(at);
        ctx.fillText(String.fromCodePoint(cp), ox + x * cw, rowY + this.baseline);
        this.decorate(at, ox + x * cw, rowY, cw, fg);
      }
    },

    clearLayer: function (z) {
      var c = this.layers[z];
      this.fseq[z] = (this.fseq[z] || 0) + 1;
      if (!c) return;
      c.getContext('2d').clearRect(0, 0, c.width, c.height);
      if (this.shown[z]) this._since++;
      this.shown[z] = false;
    },

    /* Whether any layer above the base shows a frame. */
    videoUp: function () {
      for (var z = 1; z < LAYERS; z++) if (this.shown[z]) return true;
      return false;
    },

    /* The glass as it shows, the layers stacked and the overlay left out,
     * at w by h, handed back to the wall as QOI, or as JPEG at quality q,
     * 1 to 100, where q is said: what `picture glass` and `mirror glass`
     * are. It is the canvases, drawn whether the wall is dark or not, so a
     * dark wall is pictured as though it were lit. A JPEG is the
     * browser's own encoder's, made off this turn, and its line goes with
     * its bytes, so whatever is said meanwhile cannot come between. */
    look: function (id, w, h, q) {
      if (!this.ws || this.ws.readyState !== 1) return;
      if (this._kdecoding) {
        var self = this;
        this._kwaiting.push(function () { self.look(id, w, h, q); });
        return;
      }
      if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; this.paint(); }
      var c = document.createElement('canvas');
      c.width = w; c.height = h;
      var x = c.getContext('2d');
      x.imageSmoothingQuality = 'high';
      x.fillStyle = PAPER;
      x.fillRect(0, 0, w, h);
      x.drawImage(this.canvas, 0, 0, w, h);
      for (var z = 0; z < LAYERS; z++)
        if (this.shown[z]) x.drawImage(this.layers[z], 0, 0, w, h);
      if (q) {
        var ws = this.ws;
        c.toBlob(function (blob) {
          if (ws.readyState !== 1) return;
          if (!blob) { ws.send(JSON.stringify({ t: 'look', id: id, none: true })); return; }
          ws.send(JSON.stringify({ t: 'look', id: id, w: w, h: h, f: 'jpeg' }));
          ws.send(blob);
        }, 'image/jpeg', q / 100);
        return;
      }
      var px = x.getImageData(0, 0, w, h).data;
      for (var i = 3; i < px.length; i += 4) px[i] = 255;
      this.ws.send(JSON.stringify({ t: 'look', id: id, w: w, h: h }));
      this.ws.send(B.qoiEncode(px, w, h));
    },

    /* What a film layer shows, at the size it is drawn, and where: what
     * `picture <layer>` is for a film, whose frames only the tab has. */
    lookLayer: function (id, z) {
      if (!this.ws || this.ws.readyState !== 1) return;
      var F = this.films[z];
      if (!F || !F.box || !this.shown[z]) {
        this.ws.send(JSON.stringify({ t: 'look', id: id, none: true }));
        return;
      }
      var b = F.box, c = this.layers[z];
      var px = c.getContext('2d').getImageData(b[0], b[1], b[2], b[3]).data;
      for (var i = 3; i < px.length; i += 4) px[i] = 255;
      this.ws.send(JSON.stringify({ t: 'look', id: id, w: b[2], h: b[3], x: b[0], y: b[1] }));
      this.ws.send(B.qoiEncode(px, b[2], b[3]));
    },

    /* -- films -------------------------------------------------------- */

    /* Which films this browser decodes, and which of them it says it
     * decodes in hardware, said to the wall, which offers those the
     * deciding tab says it can and no others. Hardware is the browser's
     * word for it, asked with `prefer-hardware`, which a browser may
     * answer from what it has rather than what it would use. */
    sayCodecs: function () {
      var self = this;
      if (this.codecs) {
        if (this.ws && this.ws.readyState === 1)
          this.ws.send(JSON.stringify({ t: 'codecs', list: this.codecs, hw: this.codecsHw }));
        return;
      }
      if (typeof VideoDecoder === 'undefined') {
        this.codecs = []; this.codecsHw = []; this.sayCodecs(); return;
      }
      var tries = [['h264', 'avc1.640033'], ['hevc', 'hev1.1.6.L153.B0']];
      function ask(config, name) {
        return VideoDecoder.isConfigSupported(config)
          .then(function (r) { return r.supported ? name : null; }, function () { return null; });
      }
      Promise.all(tries.map(function (t) {
        return Promise.all([ask({ codec: t[1] }, t[0]),
                            ask({ codec: t[1], hardwareAcceleration: 'prefer-hardware' }, t[0])]);
      })).then(function (got) {
        self.codecs = got.map(function (g) { return g[0]; }).filter(Boolean);
        self.codecsHw = got.map(function (g) { return g[0] && g[1]; }).filter(Boolean);
        self.sayCodecs();
      });
    },

    /* A film starting on layer z, or said again: the same stream said
     * again is the same film, and only learns its clock if it had none. */
    filmStart: function (s) {
      var z = s.z | 0;
      if (z < 0 || z >= LAYERS) return;
      var F = this.films[z];
      if (F && F.s === s.s) {
        if (F.t0 == null && s.t0 != null) F.t0 = s.t0;
        return;
      }
      this.filmDrop(z);
      this.films[z] = {
        z: z, s: s.s, codec: s.codec, fps: +s.fps || 0, t0: s.t0 == null ? null : +s.t0,
        a: +s.a || 0, b: s.b || null,
        dec: null, base: 0, nOut: 0, last: -1, chain: Promise.resolve(),
        queue: [], ended: false, flushed: false, box: null, lastDue: -1,
        joined: s.t0 == null || !s.fps ? 0 : Math.floor((Date.now() - s.t0) * s.fps / 1000),
        sent: {}
      };
    },

    /* The codec string a decoder is configured with, from the stream's
     * own parameter set where it can be read, else one broad enough. */
    filmCodec: function (codec, bytes) {
      var n = bytes.length, i, t;
      for (i = 0; i + 4 < n; i++) {
        if (bytes[i] !== 0 || bytes[i + 1] !== 0 || bytes[i + 2] !== 1) continue;
        var p = i + 3;
        if (codec === 'h264') {
          t = bytes[p] & 0x1f;
          if (t === 7 && p + 3 < n) {
            var hex = function (v) { return (v < 16 ? '0' : '') + v.toString(16); };
            return 'avc1.' + hex(bytes[p + 1]) + hex(bytes[p + 2]) + hex(bytes[p + 3]);
          }
        } else {
          t = (bytes[p] >> 1) & 0x3f;
          if (t === 33) {
            // The emulation prevention bytes out, then profile_tier_level.
            var r = [], z0 = 0;
            for (var k = p + 2; k < n && r.length < 16; k++) {
              if (z0 >= 2 && bytes[k] === 3) { z0 = 0; continue; }
              z0 = bytes[k] === 0 ? z0 + 1 : 0;
              r.push(bytes[k]);
            }
            if (r.length < 13) break;
            var space = r[1] >> 6, tier = (r[1] >> 5) & 1, prof = r[1] & 31;
            var compat = ((r[2] << 24) | (r[3] << 16) | (r[4] << 8) | r[5]) >>> 0, rev = 0;
            for (var bit = 0; bit < 32; bit++) rev = ((rev << 1) | ((compat >>> bit) & 1)) >>> 0;
            var cons = r.slice(6, 12);
            while (cons.length > 1 && cons[cons.length - 1] === 0) cons.pop();
            return 'hev1.' + (['', 'A', 'B', 'C'][space]) + prof + '.' + rev.toString(16) +
              '.' + (tier ? 'H' : 'L') + r[12] + '.' +
              cons.map(function (v) { return v.toString(16).toUpperCase(); }).join('.');
          }
        }
      }
      return codec === 'h264' ? 'avc1.640033' : 'hev1.1.6.L153.B0';
    },

    /* One frame of a film, decoded in turn; nothing before the first key,
     * and nothing the tab already has, when a catch-up and the frames
     * after it cross on the way. */
    filmChunk: function (h, bytes) {
      var F = this.films[h.z | 0], self = this;
      if (!F || F.s !== h.s || h.i <= F.last || F.failed) return;
      F.last = h.i;
      if (!F.dec) {
        if (!h.key) return;
        if (typeof VideoDecoder === 'undefined') { this.filmFail(F); return; }
        F.dec = new VideoDecoder({
          output: function (frame) { self.filmOut(F, frame); },
          error: function () { self.filmFail(F); }
        });
        try {
          F.dec.configure({ codec: this.filmCodec(F.codec, bytes) });
        } catch (e) { this.filmFail(F); return; }
        F.base = h.i;
        F.nOut = 0;
      }
      F.sent[h.i] = performance.now();
      try {
        F.dec.decode(new EncodedVideoChunk({
          type: h.key ? 'key' : 'delta', timestamp: h.i, data: bytes }));
      } catch (e) { this.filmFail(F); }
    },

    /* A decoded frame, in the order shown: frame base + n. Held as a
     * bitmap so the decoder has its own frame back at once. */
    filmOut: function (F, frame) {
      var i = F.base + F.nOut++, self = this, t = F.sent[frame.timestamp];
      delete F.sent[frame.timestamp];
      if (t !== undefined) {
        var d = this.decodeMs[F.z] || (this.decodeMs[F.z] = [0, 0]);
        d[0] += performance.now() - t; d[1]++;
      }
      F.chain = F.chain.then(function () {
        return createImageBitmap(frame).then(function (bmp) {
          frame.close();
          if (self.films[F.z] !== F) { bmp.close(); return; }
          F.queue.push({ i: i, bmp: bmp });
          self.filmPump();
        }, function () { frame.close(); });
      });
    },

    /* The decoder gave up. A film it has shown frames of was damaged on
     * the way, and is picked up again at its next key, as a decoder does;
     * one it never showed a frame of does not decode, and the wall is
     * told. */
    filmFail: function (F) {
      if (F.failed || this.films[F.z] !== F) return;
      if (F.lastDue >= 0) {
        if (F.dec && F.dec.state !== 'closed') { try { F.dec.close(); } catch (e) {} }
        F.dec = null;
        F.sent = {};
        return;
      }
      F.failed = true;
      if (this.ws && this.ws.readyState === 1)
        this.ws.send(JSON.stringify({ t: 'verror', z: F.z, s: F.s }));
    },

    /* The stream is over: what is decoded plays out, then the layer
     * clears. */
    filmEnd: function (z, s) {
      var F = this.films[z], self = this;
      if (!F || F.s !== s) return;
      F.ended = true;
      var done = function () { F.flushed = true; self.filmPump(); };
      if (F.dec && F.dec.state === 'configured') F.dec.flush().then(done, done);
      else done();
    },

    filmDrop: function (z) {
      var F = this.films[z];
      if (!F) return;
      this.films[z] = null;
      if (F.dec && F.dec.state !== 'closed') { try { F.dec.close(); } catch (e) {} }
      for (var k = 0; k < F.queue.length; k++) F.queue[k].bmp.close();
      F.queue = [];
    },

    /* A film whose layer moved up or down the stack, picture and all. */
    filmMove: function (z, to) {
      var F = this.films[z];
      if (!F || to < 0 || to >= LAYERS) return;
      this.filmDrop(to);
      this.films[to] = F; this.films[z] = null; F.z = to;
      var from = this.layers[z], c = this.layers[to], x = c.getContext('2d');
      x.clearRect(0, 0, c.width, c.height);
      x.drawImage(from, 0, 0);
      from.getContext('2d').clearRect(0, 0, from.width, from.height);
      this.shown[to] = this.shown[z]; this.shown[z] = false;
    },

    /* Each film's frame for now, up: the latest whose time has come, and
     * any older let go, counted as late unless the tab joined after it.
     * Without a rate a frame goes up as it is decoded. Woken again at
     * the next frame's time. */
    filmPump: function () {
      if (this._pump) { clearTimeout(this._pump); this._pump = 0; }
      var now = Date.now(), next = Infinity, self = this;
      for (var z = 0; z < LAYERS; z++) {
        var F = this.films[z];
        if (!F) continue;
        var q = F.queue, timed = F.fps > 0 && F.t0 != null;
        var due = timed ? Math.floor((now - F.t0) * F.fps / 1000) : Infinity;
        var k = -1;
        for (var j = 0; j < q.length; j++) if (q[j].i <= due) k = j;
        for (j = 0; j < k; j++) {
          if (timed && q[j].i >= F.joined) this.vlate[z] = (this.vlate[z] || 0) + 1;
          q[j].bmp.close();
        }
        if (k >= 0) {
          this.filmShow(F, q[k]);
          q.splice(0, k + 1);
        }
        if (q.length && timed) next = Math.min(next, F.t0 + q[0].i * 1000 / F.fps);
        if (F.ended && F.flushed && !q.length) {
          var end = timed ? F.t0 + (F.lastDue + 1) * 1000 / F.fps : now;
          if (now >= end) {
            this.filmDrop(z);
            this.clearLayer(z);
          } else {
            next = Math.min(next, end);
          }
        }
      }
      if (next < Infinity)
        this._pump = setTimeout(function () { self.filmPump(); }, Math.max(4, next - now));
    },

    /* A film's frame on its layer, filling its box `b` (x, y, w, h), or the
     * glass where it has none, with its shape kept: centred, or against
     * the box's top (`a` -1) or its bottom (1). */
    filmShow: function (F, f) {
      var c = this.layers[F.z], x = c.getContext('2d'), bmp = f.bmp;
      var b = F.b || [0, 0, c.width, c.height];
      var k = Math.min(b[2] / bmp.width, b[3] / bmp.height);
      var w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
      var px = b[0] + ((b[2] - w) >> 1);
      var py = b[1] + (F.a < 0 ? 0 : F.a > 0 ? b[3] - h : (b[3] - h) >> 1);
      x.clearRect(0, 0, c.width, c.height);
      x.imageSmoothingEnabled = true;
      x.imageSmoothingQuality = 'high';
      x.drawImage(bmp, px, py, w, h);
      bmp.close();
      F.box = [px, py, w, h];
      F.lastDue = f.i;
      this.shown[F.z] = true;
      this.vshown[F.z] = (this.vshown[F.z] || 0) + 1;
      this.puts++;
      this._since++;
    },

    /* -- the connection ---------------------------------------------- */

    connect: function () {
      var name = this.getAttribute('name');
      if (!name) return;
      var url = (location.protocol === 'https:' ? 'wss://' : 'ws://') +
        location.host + '/ws/' + encodeURIComponent(name);
      var ws = this.ws = new WebSocket(url);
      ws.binaryType = 'arraybuffer';
      var self = this;

      ws.onopen = function () { self.sendBox(); self.presence(); self.sayCodecs(); };
      ws.onmessage = function (ev) {
        if (typeof ev.data === 'string') { self.onText(ev.data); return; }
        var bytes = new Uint8Array(ev.data);
        if (self._vchunk) {
          var v = self._vchunk;
          self._vchunk = null;
          self.filmChunk(v, bytes);
          return;
        }
        if (self._kimg) {
          var k = self._kimg;
          self._kimg = null;
          self.kittyImage(k, bytes);
          return;
        }
        if (self._frame) {
          var f = self._frame;
          self._frame = null;
          self.putFrame(f, bytes);
          return;
        }
        if (self._lcells) {
          var L = self.cl[self._lcells];
          self._lcells = 0;
          if (L) {
            var before = L.parser.frames;
            L.parser.feed(bytes);
            if (L.parser.frames !== before) self.schedule();
          }
          return;
        }
        self.bytes += bytes.length;
        self.frames++;
        var tp = performance.now();
        self.parser.feed(bytes);
        self.parseMs += performance.now() - tp;
        // The pipe hands over a big repaint in pieces. Painting each
        // piece as it lands is what a viewer sees as a band sweeping
        // down the wall, so the grid is only shown between frames.
        if (self.parser.frames !== self._seenFrames) {
          self._seenFrames = self.parser.frames;
          self.schedule();
        } else {
          self.guard();
        }
      };
      ws.onclose = function () {
        self.ws = null;
        self._frame = null;
        self._kimg = null;
        self._vchunk = null;
        for (var z = 0; z < LAYERS; z++) self.filmDrop(z);
        self.kittyWipe();
        self.held = true;
        self.status('receiver disconnected');
        setTimeout(function () { self.connect(); }, 1000);
      };
    },

    onText: function (text) {
      var s;
      try { s = JSON.parse(text); } catch (e) { return; }
      if (s.t === 'frame') { this._frame = s; return; }
      if (s.t === 'kimg') { this._kimg = s; return; }
      if (s.t === 'kgone') { this.kittyForget(s.key); return; }
      if (s.t === 'kplaces') { this.kittyPlaces(s.list); return; }
      if (s.t === 'clear') { this.clearLayer(s.z | 0); return; }
      if (s.t === 'lgrid') { this.setLayer(s); return; }
      if (s.t === 'lcells') { this._lcells = s.z | 0; return; }
      if (s.t === 'lgone') { this.dropLayer(s.z | 0); return; }
      if (s.t === 'stats') { this.setOverlay(s); return; }
      if (s.t === 'vchunk') { this._vchunk = s; return; }
      if (s.t === 'vstart') { this.filmStart(s); return; }
      if (s.t === 'vend') { this.filmEnd(s.z | 0, s.s); return; }
      if (s.t === 'valign') { var VF = this.films[s.z | 0]; if (VF) VF.a = +s.a || 0; return; }
      if (s.t === 'vbox') { var BF = this.films[s.z | 0]; if (BF) BF.b = s.b || null; return; }
      if (s.t === 'vgone') { this.filmDrop(s.z | 0); this.clearLayer(s.z | 0); return; }
      if (s.t === 'vmove') { this.filmMove(s.z | 0, s.to | 0); return; }
      if (s.t === 'look') {
        if (typeof s.z === 'number') this.lookLayer(s.id, s.z | 0);
        else this.look(s.id, s.w | 0, s.h | 0, s.q | 0);
        return;
      }
      if (s.t === 'wipe') {
        this.grid.clear();
        this.parser.reset();
        this._seenFrames = 0;
        this.kittyWipe();
        for (var z = 0; z < LAYERS; z++) { this.filmDrop(z); this.dropLayer(z); this.clearLayer(z); }
        this.schedule();
        return;
      }
      if (s.t !== 'state') return;
      var was = this.state;
      this.state = s;
      this.decider = !!s.decider;
      this.cover.style.display = s.dark || s.dozing ? 'block' : 'none';
      if (!s.sender && !this.held) {
        this.held = true;
        this.grid.clear();
        this.status('no sender connected');
      } else if (s.sender && this.held) {
        this.settled();
        this.held = false;
        this.wipe();
        this.grid.dirty.fill(1);
      }
      this.layout();
      if (!was || JSON.stringify(was.want) !== JSON.stringify(s.want)) this.sendBox();
      this.dispatchEvent(new CustomEvent('bitwall-state', { detail: s }));
    },

    /* -- the glass: what only the painting knows ---------------------- */

    /* Once a second: how often the glass was put up, by any layer, and per
     * frame the time taken to read what the base sent and to draw it, and
     * each layer's time to decode and put up a frame, sent up through the
     * server as the wall's own. A canvas has no swap. */
    second: function () {
      var now = performance.now(), dt = (now - this._statsAt) / 1000;
      if (dt <= 0) return;
      this.fps = this._since / dt;
      this.rate = (this.bytes - this._rateWas) / dt;
      this._rateWas = this.bytes;
      this._since = 0;
      this._statsAt = now;
      var g = this._glassWas || { painted: 0, paintMs: 0, parsed: 0, parseMs: 0 };
      var painted = this.painted - g.painted, parsed = this.parser.frames - g.parsed;
      var msg = { t: 'glass', fps: this.fps, decode: {} };
      if (painted > 0) msg.draw = (this.paintMs - g.paintMs) / painted;
      if (parsed > 0) msg.frame = (this.parseMs - g.parseMs) / parsed;
      msg.shown = {}; msg.late = {};
      for (var z = 0; z < LAYERS; z++) {
        var d = this.decodeMs[z];
        if (d && d[1]) { msg.decode[z] = d[0] / d[1]; this.decodeMs[z] = [0, 0]; }
        if (this.vshown[z]) { msg.shown[z] = this.vshown[z]; this.vshown[z] = 0; }
        if (this.vlate[z]) { msg.late[z] = this.vlate[z]; this.vlate[z] = 0; }
      }
      this._glassWas = { painted: this.painted, paintMs: this.paintMs,
                         parsed: this.parser.frames, parseMs: this.parseMs };
      if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg));
      this.dispatchEvent(new CustomEvent('bitwall-stats', {
        detail: { fps: this.fps, bytes: this.bytes, frames: this.frames, rate: this.rate,
                  paintMs: this.paintMs / Math.max(1, this.painted),
                  cols: this.cols, rows: this.rows, font: this.fontPx }
      }));
    },

    /* The receiver's own numbers on its own glass, as the wall sends them:
     * on a canvas of their own over the layers, never in them, so every
     * cell stays what the sender composed and a picture of the glass
     * leaves them out. */
    setOverlay: function (s) {
      this.overlay = s.on && s.lines && s.lines.length ? s : null;
      this.drawOverlay();
    },

    /* Ask the wall to turn its overlay on or off, for everyone watching,
     * as a controller's `stats 1` would. */
    toggleStats: function () {
      if (!this.ws || this.ws.readyState !== 1) return;
      this.ws.send(JSON.stringify({ t: 'stats', on: !this.overlay }));
    },

    /* As the painter draws it: white on a dark box, inset 8 pixels from the
     * edge or corner asked for, half the font's size of padding. */
    drawOverlay: function () {
      var o = this.overlay, ctx = this.octx, dpr = this.dpr || 1;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, this.over.width, this.over.height);
      if (!o) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var px = o.size, line = Math.round(px * 1.25), pad = Math.round(px / 2), inset = 8;
      ctx.font = px + 'px ' + FAMILY;
      ctx.textBaseline = 'top';
      var w = 0;
      o.lines.forEach(function (t) { w = Math.max(w, ctx.measureText(t).width); });
      w = Math.ceil(w + 2 * pad);
      var h = o.lines.length * line + 2 * pad;
      var W = this.boxW, H = this.boxH, at = o.at;
      var x = at.indexOf('w') >= 0 ? inset : at.indexOf('e') >= 0 ? W - w - inset : (W - w) / 2;
      var y = at.indexOf('n') >= 0 ? inset : at.indexOf('s') >= 0 ? H - h - inset : (H - h) / 2;
      x = Math.max(0, Math.round(x)); y = Math.max(0, Math.round(y));
      ctx.fillStyle = 'rgba(0,0,0,0.72)';
      ctx.fillRect(x, y, w, h);
      ctx.fillStyle = '#ffffff';
      for (var i = 0; i < o.lines.length; i++)
        ctx.fillText(o.lines[i], x + pad, y + pad + i * line);
    },

    /* -- what a viewer may give the wall ------------------------------ */

    /* What is on, and what this page could do if asked.
     *
     * Two things have to agree: the page was started willing to pass a
     * kind on, and a sender has asked for it. Either alone is nothing --
     * a wall nobody asked to listen should not be listening. */
    allowed: function (kind) {
      var want = this.state && this.state.want;
      return this.hasAttribute(kind) && !!(want && want[kind] === true);
    },

    input: function (payload) {
      if (!this.ws || this.ws.readyState !== 1) return;
      payload.t = 'input';
      this.ws.send(JSON.stringify(payload));
    },

    /* Where on the grid, in cells. This is the only thing that knows the
     * mapping, and a sender thinks in cells. The area goes with it so a
     * press that crosses a resize can be told from a current one. */
    onPress: function (ev) {
      if (!this.allowed('touch') || !this.cw || !this.chh) return;
      var r = this.getBoundingClientRect();
      var x = Math.floor((ev.clientX - r.left - this.ox) / this.cw);
      var y = Math.floor((ev.clientY - r.top - this.oy) / this.chh);
      if (x >= 0 && x < this.cols && y >= 0 && y < this.rows) {
        this.input({ kind: 'tap', cell: [x, y], area: [this.cols, this.rows] });
      }
    },

    /* A keystroke as what was pressed, never as bytes. Turning a name
     * into a terminal sequence is the sender's business. */
    onKey: function (ev) {
      if (!this.allowed('keyboard')) return;
      var k = ev.key;
      if (!k || k === 'Shift' || k === 'Control' || k === 'Alt' ||
          k === 'Meta') return;
      k = KEYNAME[k] || k;
      var mods = [];
      if (ev.ctrlKey) mods.push('ctrl');
      if (ev.altKey) mods.push('alt');
      if (ev.shiftKey && k.length > 1) mods.push('shift');
      var msg = { kind: 'key', key: k };
      if (mods.length) msg.mods = mods;
      this.input(msg);
      // Taken, and said to be taken. A key event that reaches the end of
      // the responder chain unhandled is what makes macOS ring the bell.
      // Command combinations are left alone: those belong to the host.
      if (!ev.metaKey) ev.preventDefault();
    },

    presence: function () {
      if (!this.ws || this.ws.readyState !== 1) return;
      this.ws.send(JSON.stringify({
        t: 'presence', visible: document.visibilityState === 'visible'
      }));
    },

    /* What this receiver is, and that nothing is feeding it, on the base's
     * canvas under any layer still up, as the painter's status page is.
     *
     * A wall still showing the last frame it was sent looks like a wall
     * that is working, so silence has to say so on the screen. The clock
     * is the part that proves the receiver itself is alive. */
    status: function (reason) {
      if (this._waitTimer) { clearTimeout(this._waitTimer); }
      this._waiting = reason;
      if (!this.cols) return;
      this.wipe();

      var ctx = this.ctx, s = this.state || {};
      var cw = this.cw || 8, ch = this.chh || 16;
      var rows = [
        ['', ''],
        ['bitwall', 'disconnected'],
        ['', ''],
        ['this display', (location.hostname || 'browser') + (s.name ? ':' + s.name : '')],
        ['font size', String(this.fontPx || '')],
        ['area', this.cols + 'x' + this.rows + ' cells'],
        ['stream', reason],
        ['', ''],
        ['', clock()]
      ];

      ctx.font = (this.fontPx || 14) + 'px ' + FAMILY;
      ctx.textBaseline = 'top';
      for (var i = 0; i < rows.length; i++) {
        var y = this.oy + (1 + i) * ch;
        if (rows[i][0]) {
          ctx.fillStyle = '#8a8a8a';
          ctx.fillText(rows[i][0], this.ox + 3 * cw, y);
        }
        if (rows[i][1]) {
          ctx.fillStyle = rows[i][0] === 'stream' ? '#d75f5f' : '#bcbcbc';
          ctx.fillText(rows[i][1], this.ox + 18 * cw, y);
        }
      }
      ctx.textBaseline = 'alphabetic';
      this._since++;

      var self = this;
      this._waitTimer = setTimeout(function () {
        if (self._waiting && self.held) self.status(self._waiting);
      }, 1000);
    },

    /* Nothing is waiting any more: a frame is about to land. */
    settled: function () {
      this._waiting = null;
      if (this._waitTimer) {
        clearTimeout(this._waitTimer);
        this._waitTimer = 0;
      }
    },

    /* -- the viewer's zoom -------------------------------------------- */

    /* A multiplier on the font the wall is asked for, said with the box:
     * the wall works out the area, and says it. */
    zoom: function (mult) {
      this._zoomMult = Math.max(0.25, Math.min(8, mult || 1));
      this.sendBox();
      return this._zoomMult;
    }
  };

  var Screen = class extends HTMLElement {
    connectedCallback() { if (!this.canvas) this.init(); }
    disconnectedCallback() {
      if (this._ro) this._ro.disconnect();
      if (this._tick) clearInterval(this._tick);
      if (this.ws) { this.ws.onclose = null; this.ws.close(); }
      if (this._raf) cancelAnimationFrame(this._raf);
    }
  };
  Object.keys(proto).forEach(function (k) {
    Screen.prototype[k] = proto[k];
  });
  customElements.define('bitwall-screen', Screen);
})();
