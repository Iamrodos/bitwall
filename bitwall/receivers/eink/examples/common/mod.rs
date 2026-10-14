//! The sample wall both examples draw: every kind of cell, and a picture.

use std::path::PathBuf;

use bitwall_eink::panel::read_png;
use bitwall_eink::scene::{BOLD, FAINT, ITALIC, REVERSE, STRIKE, UNDER};
use bitwall_eink::{Cell, Cells};

pub const D: i64 = -1;

pub fn rgb(r: u8, g: u8, b: u8) -> i64 {
    (r as i64) << 16 | (g as i64) << 8 | b as i64
}

fn put(g: &mut Cells, x: usize, y: usize, ch: char, fg: i64, bg: i64) {
    g.set(x, y, Cell::new(ch, fg, bg, 0));
}

fn boxed(g: &mut Cells, x: usize, y: usize, w: usize, h: usize, set: [char; 6], fg: i64) {
    let [hz, vt, tl, tr, bl, br] = set;
    for i in 1..w - 1 {
        put(g, x + i, y, hz, fg, D);
        put(g, x + i, y + h - 1, hz, fg, D);
    }
    for j in 1..h - 1 {
        put(g, x, y + j, vt, fg, D);
        put(g, x + w - 1, y + j, vt, fg, D);
    }
    put(g, x, y, tl, fg, D);
    put(g, x + w - 1, y, tr, fg, D);
    put(g, x, y + h - 1, bl, fg, D);
    put(g, x + w - 1, y + h - 1, br, fg, D);
}

/// The sample grid: what a sender might draw, every kind of cell in it.
pub fn grid(cols: usize, rows: usize, font: u32, tick: u32) -> Cells {
    let mut g = Cells::new(font, cols, rows, Cell::BLANK);
    // A header band with its corners cut, two rows deep: arrow ends.
    let band = rgb(40, 90, 160);
    for y in 0..2 {
        for x in 2..cols - 2 {
            put(&mut g, x, y, ' ', D, band);
        }
    }
    put(&mut g, 2, 0, '◢', band, D);
    put(&mut g, cols - 3, 0, '◣', band, D);
    put(&mut g, 2, 1, '◥', band, D);
    put(&mut g, cols - 3, 1, '◤', band, D);
    g.text(5, 0, "bitwall-eink  sample wall", rgb(255, 255, 255), band, BOLD);
    g.text(5, 1, &format!("{cols}x{rows} at font {font}, paper or screen, tick {tick}"), rgb(200, 220, 255), band, 0);

    // Boxes: light, heavy, double, rounded, joined.
    boxed(&mut g, 2, 3, 22, 8, ['─', '│', '┌', '┐', '└', '┘'], D);
    g.text(4, 4, "light ├─┼─┤", D, D, 0);
    boxed(&mut g, 25, 3, 22, 8, ['━', '┃', '┏', '┓', '┗', '┛'], rgb(255, 200, 0));
    g.text(27, 4, "heavy ┣━╋━┫", rgb(255, 200, 0), D, 0);
    boxed(&mut g, 48, 3, 22, 8, ['═', '║', '╔', '╗', '╚', '╝'], rgb(0, 255, 160));
    g.text(50, 4, "double ╠═╬═╣", rgb(0, 255, 160), D, 0);
    boxed(&mut g, 71, 3, 22, 8, ['─', '│', '╭', '╮', '╰', '╯'], rgb(255, 120, 200));
    g.text(73, 4, "rounded ╭─╮", rgb(255, 120, 200), D, 0);
    g.text(4, 6, "┌┬┐ ┏┳┓ ╔╦╗", D, D, 0);
    g.text(4, 7, "├┼┤ ┣╋┫ ╠╬╣", D, D, 0);
    g.text(4, 8, "└┴┘ ┗┻┛ ╚╩╝", D, D, 0);
    g.text(27, 6, "╒═╕ ╓─╖ ┍━┑", D, D, 0);
    g.text(27, 7, "╞═╡ ╟─╢ ┝━┥", D, D, 0);
    g.text(27, 8, "╘═╛ ╙─╜ ┕━┙", D, D, 0);

    // Blocks and eighths: a bar chart.
    let blocks = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    for x in 0..40 {
        let v = ((x as f64 * 0.37 + tick as f64 * 0.3).sin() * 0.5 + 0.5) * 32.0;
        for row in 0..4 {
            let level = (v - (3 - row) as f64 * 8.0).clamp(0.0, 8.0) as usize;
            put(&mut g, 2 + x, 12 + row, blocks[level], rgb(120, 220, 255), D);
        }
    }
    g.text(2, 16, "eighths ▏▎▍▌▋▊▉█ ▔▕ quadrants ▖▗▘▝▙▚▛▜▞▟ halves ▀▄▌▐", D, D, 0);
    // Shades.
    g.text(2, 17, "shades ░░▒▒▓▓██", D, D, 0);
    g.text(20, 17, "░░▒▒▓▓██", rgb(255, 80, 80), rgb(0, 0, 120), 0);

    // Braille: a sparkline, then faint braille with its unlit dots.
    let mut line = String::new();
    for x in 0..40 {
        let a = ((x as f64 * 0.5).sin() * 1.5 + 1.5) as u32;
        let b = (((x as f64 + 0.5) * 0.5).sin() * 1.5 + 1.5) as u32;
        let dot_l = [0x40, 0x04, 0x02, 0x01][a as usize];
        let dot_r = [0x80, 0x20, 0x10, 0x08][b as usize];
        line.push(char::from_u32(0x2800 + dot_l + dot_r).unwrap());
    }
    g.text(44, 12, &line, rgb(0, 255, 0), D, 0);
    g.text(44, 13, &line, rgb(255, 160, 0), rgb(60, 30, 0), FAINT);
    g.text(44, 14, "⣿⣿⣿⡇⠀⣿⣿ dense ⠿⠿⠿", D, D, 0);

    // Octants and sextants: a circle, two across and four down a cell.
    let (cx, cy, r) = (10.0, 14.0, 12.0);
    for row in 0..8 {
        for col in 0..12 {
            let mut bits = 0u32;
            for py in 0..4 {
                for px in 0..2 {
                    let x = (col * 2 + px) as f64 + 0.5;
                    let y = ((row * 4 + py) as f64 + 0.5) / 2.0 * 2.0;
                    let d = ((x - cx).powi(2) + (y / 1.0 - cy).powi(2) * 1.0).sqrt();
                    if d < r && d > r - 4.0 {
                        bits |= 1 << (py * 2 + px);
                    }
                }
            }
            put(&mut g, 2 + col, 19 + row, octant(bits), rgb(255, 255, 120), D);
        }
    }
    g.text(16, 19, "sextants 🬀🬁🬂🬃🬄🬅🬆🬇🬈🬉🬊🬋🬌🬍🬎🬏", D, D, 0);
    g.text(16, 20, "octants  \u{1CD00}\u{1CD01}\u{1CD02}\u{1CD10}\u{1CD20}\u{1CD40}\u{1CD80}\u{1CDE5}", D, D, 0);
    g.text(16, 21, "LEDs     ▪▪▪▪▪▪▪▪", rgb(255, 40, 40), D, 0);
    g.text(16, 22, "diagonal ◢◣◤◥ ◢◤◢◤◢◤", D, D, 0);

    // Letters and styles.
    g.text(16, 24, "The quick brown fox jumps over the lazy dog 0123456789", D, D, 0);
    g.text(16, 25, "bold", D, D, BOLD);
    g.text(21, 25, "italic", D, D, ITALIC);
    g.text(28, 25, "underline", D, D, UNDER);
    g.text(38, 25, "strike", D, D, STRIKE);
    g.text(45, 25, " reverse ", D, D, REVERSE);
    g.text(55, 25, "powerline \u{e0b0}\u{e0b2} nerd \u{f303} \u{f179}", D, D, 0);

    // Colours: the 16 named, a hue ramp, a grey ramp, as grounds.
    let named = [
        0x000000, 0xcd0000, 0x00cd00, 0xcdcd00, 0x0000ee, 0xcd00cd, 0x00cdcd, 0xe5e5e5, 0x7f7f7f, 0xff0000, 0x00ff00,
        0xffff00, 0x5c5cff, 0xff00ff, 0x00ffff, 0xffffff,
    ];
    for (i, c) in named.iter().enumerate() {
        for k in 0..3 {
            put(&mut g, 2 + i * 3 + k, 28, ' ', D, *c);
        }
    }
    for x in 0..48 {
        let h = x as f64 / 48.0 * 6.0;
        let f = |o: f64| ((((h + o) % 6.0) - 3.0).abs() - 1.0).clamp(0.0, 1.0);
        put(&mut g, 2 + x, 29, ' ', D, rgb((f(0.0) * 255.0) as u8, (f(4.0) * 255.0) as u8, (f(2.0) * 255.0) as u8));
        let v = (x * 255 / 47) as u8;
        put(&mut g, 2 + x, 30, ' ', D, rgb(v, v, v));
        put(&mut g, 2 + x, 31, '█', rgb(v, v, v), D);
    }
    g.text(51, 29, "hues as greys by luminance", D, D, 0);
    g.text(51, 30, "grey ramp as grounds", D, D, 0);
    g.text(51, 31, "grey ramp as ink", D, D, 0);

    // A counter in blocks, which changes between pushes.
    g.text(2, 33, &format!("counter {tick:>4}"), D, D, BOLD);
    for i in 0..tick.min(40) as usize {
        put(&mut g, 16 + i, 33, '█', D, D);
    }
    // Text everywhere else, so the grid is full.
    for y in 35..rows {
        let s: String = (0..cols - 4).map(|x| char::from_u32(0x21 + ((x * 7 + y * 3) % 94) as u32).unwrap()).collect();
        g.text(2, y, &s, if y % 2 == 0 { D } else { rgb(150, 150, 150) }, D, 0);
    }
    g
}

/// The character for a 2x4 pattern, bit (row * 2 + col).
fn octant(bits: u32) -> char {
    // Bits in octant order are dot n at row n/2, column n%2: the same.
    const DRAWN: [(u32, char); 26] = [
        (0x00, ' '), (0x01, '\u{1CEA8}'), (0x02, '\u{1CEAB}'), (0x03, '\u{1FB82}'), (0x05, '\u{2598}'),
        (0x0a, '\u{259D}'), (0x0f, '\u{2580}'), (0x14, '\u{1FBE6}'), (0x28, '\u{1FBE7}'), (0x3f, '\u{1FB85}'),
        (0x40, '\u{1CEA3}'), (0x50, '\u{2596}'), (0x55, '\u{258C}'), (0x5a, '\u{259E}'), (0x5f, '\u{259B}'),
        (0x80, '\u{1CEA0}'), (0xa0, '\u{2597}'), (0xa5, '\u{259A}'), (0xaa, '\u{2590}'), (0xaf, '\u{259C}'),
        (0xc0, '\u{2582}'), (0xf0, '\u{2584}'), (0xf5, '\u{2599}'), (0xfa, '\u{259F}'), (0xfc, '\u{2586}'),
        (0xff, '\u{2588}'),
    ];
    if let Some((_, c)) = DRAWN.iter().find(|(b, _)| *b == bits) {
        return *c;
    }
    let skipped = DRAWN.iter().filter(|(b, _)| *b < bits).count() as u32;
    char::from_u32(0x1CD00 + bits - skipped).unwrap()
}

/// A picture: the given PNG, or a made-up one of gradients and rings.
pub fn picture(path: Option<&PathBuf>) -> (Vec<u8>, u32, u32) {
    if let Some(p) = path {
        return read_png(p).expect("the picture reads");
    }
    let (w, h) = (480u32, 270u32);
    let mut px = Vec::with_capacity((w * h * 4) as usize);
    for y in 0..h {
        for x in 0..w {
            let (u, v) = (x as f64 / w as f64, y as f64 / h as f64);
            let d = ((u - 0.6).powi(2) + (v - 0.5).powi(2)).sqrt();
            let ring = ((d * 40.0).sin() * 0.5 + 0.5) * (1.0 - d).max(0.0);
            px.extend_from_slice(&[(u * 255.0) as u8, (ring * 255.0) as u8, (v * 255.0) as u8, 255]);
        }
    }
    (px, w, h)
}

