//! A wall with a glass of pixels and no screen: the least receiver of
//! pixels the core makes, for holding the core to PROTOCOL.md's clauses
//! about video layers and pictures of the glass, as tests/bitwall-contracts
//! does. Its glass is composed in software only when it is looked at: each
//! cell its ground, in the colour its alpha mixes, the grid centred; each
//! video layer's frame decoded once and drawn to its box over them. Text
//! is not drawn, which no clause looks at.
//!
//!     headless [--glass WxH] [--font <n>] [wall]      wall 0 unless named
//!
//! A line `glass WxH` on its stdin gives it a glass of that size, as the
//! painter's `--glass` is given one. BITWALL_RUN puts the walls elsewhere.
//!
//!     cargo build --release --features decode --example headless

use bitwall_receiver::decode::{self, Rgba};
use bitwall_receiver::grid::{Grid, DEFAULT, NONE, PAPER};
use bitwall_receiver::{pipe, sys, Ask, Glass, Surface, Wall};
use std::collections::HashMap;
use std::io::BufRead;
use std::sync::mpsc;
use std::time::{Duration, Instant};

const FONTS: std::ops::RangeInclusive<u32> = 4..=64;

/// A glass of pixels, its cell a fixed shape of the font.
struct Pixels {
    w: u32,
    h: u32,
    /// Each frame decoded, by the layer it is on, kept until the next.
    decoded: HashMap<usize, (u64, Option<Rgba>)>,
    decode_ms: HashMap<usize, f64>,
}

fn cell(font: u32) -> (u32, u32) {
    ((font * 4 + 3) / 7, (font * 8 + 3) / 7)
}

impl Glass for Pixels {
    fn area(&self, ask: Ask) -> (usize, usize, u32) {
        let fit = |f: u32| {
            let (cw, ch) = cell(f);
            ((self.w / cw).max(1) as usize, (self.h / ch).max(1) as usize, f)
        };
        match ask {
            Ask::Font(f) => fit(f.clamp(*FONTS.start(), *FONTS.end())),
            Ask::Grid(c, r) => FONTS.rev().map(fit).find(|a| a.0 >= c && a.1 >= r).unwrap_or_else(|| fit(*FONTS.start())),
        }
    }

    fn pixels(&self) -> Option<(u32, u32)> {
        Some((self.w, self.h))
    }

    fn cell(&self, font: u32) -> Option<(u32, u32)> {
        Some(cell(font))
    }

    fn colour(&self) -> &str {
        "true"
    }

    fn rate(&self) -> u32 {
        60
    }

    fn render(&self) -> &str {
        "software"
    }

    fn decode_ms(&self, z: usize) -> Option<f64> {
        self.decode_ms.get(&z).copied()
    }

    fn look(&mut self, wall: &Wall, w: u32, h: u32) -> Option<Vec<u8>> {
        let full = self.compose(wall);
        let mut out = vec![0u8; (w * h * 4) as usize];
        for y in 0..h {
            let sy = (y as u64 * self.h as u64 / h as u64) as u32;
            for x in 0..w {
                let sx = (x as u64 * self.w as u64 / w as u64) as u32;
                let s = ((sy * self.w + sx) * 4) as usize;
                let d = ((y * w + x) * 4) as usize;
                out[d..d + 4].copy_from_slice(&full[s..s + 4]);
            }
        }
        Some(out)
    }
}

/// `v` as rgb and the alpha grid.rs keeps above it.
fn rgba(v: i64) -> ([u8; 3], u32) {
    let a = if v >> 24 == 0 { 255 } else { ((v >> 24) - 1) as u32 };
    ([(v >> 16) as u8, (v >> 8) as u8, v as u8], a)
}

fn mix(px: &mut [u8], c: [u8; 3], a: u32) {
    for k in 0..3 {
        px[k] = ((c[k] as u32 * a + px[k] as u32 * (255 - a)) / 255) as u8;
    }
    px[3] = 255;
}

impl Pixels {
    /// Every layer composed, the overlay left out, as though lit.
    fn compose(&mut self, wall: &Wall) -> Vec<u8> {
        let (gw, gh) = (self.w, self.h);
        let mut out = vec![0u8; (gw * gh * 4) as usize];
        for p in out.chunks_exact_mut(4) {
            p[3] = 255;
        }
        let (cw, ch) = cell(wall.font);
        let ox = (gw as i64 - (wall.cols as u32 * cw) as i64) / 2;
        let oy = (gh as i64 - (wall.rows as u32 * ch) as i64) / 2;
        for layer in wall.stack() {
            match layer.surface {
                Surface::Cells(g) => self.cells(&mut out, g, layer.z == 0, (ox, oy), (cw, ch)),
                Surface::Video(Some(f)) => {
                    let had = self.decoded.get(&layer.z).map(|d| d.0);
                    if had != Some(f.seq) {
                        let t = Instant::now();
                        let got = decode::rgba(f).ok();
                        self.decode_ms.insert(layer.z, t.elapsed().as_secs_f64() * 1000.0);
                        self.decoded.insert(layer.z, (f.seq, got));
                    }
                    if let Some((_, Some(img))) = self.decoded.get(&layer.z) {
                        let (x, y, bw, bh) = f.placed();
                        blit(&mut out, (gw, gh), img, x, y, bw, bh);
                    }
                }
                Surface::Video(None) => {}
            }
        }
        out
    }

    fn cells(&self, out: &mut [u8], g: &Grid, base: bool, (ox, oy): (i64, i64), (cw, ch): (u32, u32)) {
        for cy in 0..g.rows {
            for cx in 0..g.cols {
                let c = g.at(cx, cy);
                if c.ch == NONE {
                    continue;
                }
                let (col, a) = if c.bg == DEFAULT {
                    if !base {
                        continue;
                    }
                    rgba(PAPER)
                } else {
                    rgba(c.bg)
                };
                for py in 0..ch as i64 {
                    let y = oy + cy as i64 * ch as i64 + py;
                    if y < 0 || y >= self.h as i64 {
                        continue;
                    }
                    for px in 0..cw as i64 {
                        let x = ox + cx as i64 * cw as i64 + px;
                        if x < 0 || x >= self.w as i64 {
                            continue;
                        }
                        let i = ((y as u32 * self.w + x as u32) * 4) as usize;
                        mix(&mut out[i..i + 4], col, a);
                    }
                }
            }
        }
    }
}

/// A picture drawn to its box at x, y, nearest pixel, blended by its alpha.
fn blit(out: &mut [u8], (gw, gh): (u32, u32), img: &Rgba, x: i32, y: i32, bw: u32, bh: u32) {
    for dy in 0..bh as i64 {
        let ty = y as i64 + dy;
        if ty < 0 || ty >= gh as i64 {
            continue;
        }
        let sy = (dy as u64 * img.h as u64 / bh as u64) as u32;
        for dx in 0..bw as i64 {
            let tx = x as i64 + dx;
            if tx < 0 || tx >= gw as i64 {
                continue;
            }
            let sx = (dx as u64 * img.w as u64 / bw as u64) as u32;
            let s = ((sy * img.w + sx) * 4) as usize;
            let d = ((ty as u32 * gw + tx as u32) * 4) as usize;
            let p = &img.px[s..s + 4];
            mix(&mut out[d..d + 4], [p[0], p[1], p[2]], p[3] as u32);
        }
    }
}

fn fail(msg: &str) -> ! {
    eprintln!("headless: {msg}");
    std::process::exit(1);
}

fn size(v: &str) -> Option<(u32, u32)> {
    let (w, h) = v.split_once('x')?;
    let (w, h) = (w.parse().ok()?, h.parse().ok()?);
    if w > 0 && h > 0 && w <= 8192 && h <= 8192 {
        Some((w, h))
    } else {
        None
    }
}

fn main() {
    let mut font: u32 = std::env::var("BITWALL_FONT").ok().and_then(|v| v.parse().ok()).unwrap_or(14);
    let (mut gw, mut gh) = (640, 360);
    let mut name = "0".to_string();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--font" => font = args.next().and_then(|v| v.parse().ok()).unwrap_or_else(|| fail("--font wants a size")),
            "--glass" => (gw, gh) = args.next().and_then(|v| size(&v)).unwrap_or_else(|| fail("--glass wants WxH")),
            _ if a.starts_with('-') => fail(&format!("{a}: unknown option")),
            _ => name = a,
        }
    }
    let run = std::env::var("BITWALL_RUN").ok().filter(|v| !v.is_empty()).unwrap_or_else(pipe::run_dir);
    let (dir, door) = pipe::claim(&run, &name).unwrap_or_else(|e| fail(&e));
    let receiver = format!("receiver bitwall-headless version=example started={}", sys::unix_now());
    let mut glass = Pixels { w: gw, h: gh, decoded: HashMap::new(), decode_ms: HashMap::new() };
    let mut wall = Wall::new(&name, dir, door, &glass, font, receiver);

    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            if tx.send(line).is_err() {
                break;
            }
            pipe::ring();
        }
    });
    loop {
        pipe::wait(Duration::from_millis(15));
        while let Ok(line) = rx.try_recv() {
            if let Some((w, h)) = line.strip_prefix("glass ").and_then(|v| size(v.trim())) {
                (glass.w, glass.h) = (w, h);
                glass.decoded.clear();
                wall.glass_changed(&mut glass);
            }
        }
        wall.serve(Instant::now(), &mut glass);
        // Nothing is drawn until looked at; what was decoded for a layer
        // taken down is let go.
        let live: Vec<usize> = wall.stack().iter().filter(|l| matches!(l.surface, Surface::Video(Some(_)))).map(|l| l.z).collect();
        glass.decoded.retain(|z, _| live.contains(z));
        wall.changed = false;
        wall.frame_closed = false;
    }
}
