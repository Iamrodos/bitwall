//! Kitty graphics in the base's grid, read by the wall itself: PROTOCOL.md's
//! "A grid may carry kitty graphics", as `receivers/kitty.py` keeps them
//! for the web receiver and `bitwall-paint.c` for the painter, whose order
//! of checks, wording and sizing it follows. The receiver core hands over
//! each string whole with the cell its cursor was on; this puts the chunks
//! together, holds the limits and the ids, decodes each picture once to
//! RGBA, and hands back the refusals to say on the base's `out`. What is
//! placed becomes pictures in the base's cells for the compositor.

use std::collections::HashMap;
use std::sync::Arc;

use bitwall_eink::Picture;

/// An image received and its sides, as a video frame's; images and
/// placements the base holds; decoded pixels the wall holds.
const RECEIVED: usize = 16 << 20;
const SIDE: u32 = 4096;
const IMAGES: usize = 32;
const PLACES: usize = 64;
const WALL: usize = 64 << 20;

/// The keys of one string, as far as they are drawn. `other` is a key
/// besides `m` and `q`, which a chunk after the first never has.
#[derive(Clone, Default)]
struct Keys {
    a: u8,
    t: u8,
    d: u8,
    o: u8,
    f: i64,
    s: i64,
    v: i64,
    m: i64,
    u: i64,
    cap_c: i64,
    z: i64,
    c: u32,
    r: u32,
    i: u32,
    p: u32,
    other: bool,
}

fn num(v: &[u8]) -> i64 {
    let (neg, digits) = match v.first() {
        Some(b'-') => (true, &v[1..]),
        _ => (false, v),
    };
    let mut d: i64 = 0;
    for &ch in digits {
        if !ch.is_ascii_digit() {
            break;
        }
        if d < 100_000_000_000 {
            d = d * 10 + (ch - b'0') as i64;
        }
    }
    let d = if neg { -d } else { d };
    d.clamp(-(1 << 31), (1 << 31) - 1)
}

/// The keys, and the payload after them.
fn keys(body: &[u8]) -> (Keys, &[u8]) {
    let (head, payload) = match body.iter().position(|&b| b == b';') {
        Some(k) => (&body[..k], &body[k + 1..]),
        None => (body, &body[body.len()..]),
    };
    let mut k = Keys::default();
    for part in head.split(|&b| b == b',') {
        if part.len() < 2 || part[1] != b'=' {
            continue;
        }
        let (key, v) = (part[0], &part[2..]);
        if key != b'm' && key != b'q' {
            k.other = true;
        }
        let first = v.first().copied().unwrap_or(0);
        match key {
            b'a' => k.a = first,
            b't' => k.t = first,
            b'd' => k.d = first,
            b'o' => k.o = first,
            b'f' => k.f = num(v),
            b's' => k.s = num(v),
            b'v' => k.v = num(v),
            b'm' => k.m = num(v),
            b'U' => k.u = num(v),
            b'C' => k.cap_c = num(v),
            b'z' => k.z = num(v),
            b'c' => k.c = num(v).clamp(0, 1000) as u32,
            b'r' => k.r = num(v).clamp(0, 1000) as u32,
            b'i' => k.i = num(v).max(0) as u32,
            b'p' => k.p = num(v).max(0) as u32,
            _ => {}
        }
    }
    (k, payload)
}

fn sextet(b: u8) -> Option<u8> {
    Some(match b {
        b'A'..=b'Z' => b - b'A',
        b'a'..=b'z' => b - b'a' + 26,
        b'0'..=b'9' => b - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => return None,
    })
}

/// An image arriving in chunks: its first chunk's keys, what its base64
/// has decoded to so far and the sextets carried across a chunk.
struct Pending {
    k: Keys,
    data: Vec<u8>,
    carry: Vec<u8>,
    bad: bool,
    refused: bool,
}

impl Pending {
    fn new(k: Keys) -> Pending {
        Pending { k, data: Vec::new(), carry: Vec::new(), bad: false, refused: false }
    }

    /// Base64 onto the image; false once it is past RECEIVED.
    fn take(&mut self, payload: &[u8]) -> bool {
        for &b in payload {
            match b {
                b'=' => self.close(),
                b' ' | b'\n' | b'\r' => {}
                _ => match sextet(b) {
                    Some(s) => {
                        self.carry.push(s);
                        if self.carry.len() == 4 {
                            let c = &self.carry;
                            self.data.extend_from_slice(&[c[0] << 2 | c[1] >> 4, c[1] << 4 | c[2] >> 2, c[2] << 6 | c[3]]);
                            self.carry.clear();
                        }
                    }
                    None => self.bad = true,
                },
            }
            if self.data.len() > RECEIVED {
                return false;
            }
        }
        true
    }

    /// What is left in the carry, closed as padding would close it.
    fn close(&mut self) {
        let c = std::mem::take(&mut self.carry);
        if c.len() >= 2 {
            self.data.push(c[0] << 2 | c[1] >> 4);
        }
        if c.len() >= 3 {
            self.data.push(c[1] << 4 | c[2] >> 2);
        }
    }
}

struct Image {
    w: u32,
    h: u32,
    rgba: Arc<Vec<u8>>,
}

/// One placement: its image's id, its own, the cell it is at, the cells
/// asked for (0 where they are worked out), its z and when it was placed.
struct Place {
    image: u32,
    pid: u32,
    col: usize,
    row: usize,
    c: u32,
    r: u32,
    z: i64,
    seq: u64,
}

/// The base's kitty graphics.
#[derive(Default)]
pub struct Kitty {
    images: HashMap<u32, Image>,
    places: Vec<Place>,
    seq: u64,
    auto_id: u32,
    pending: Option<Pending>,
    total: usize,
    /// Whether what is placed changed since asked.
    pub changed: bool,
}

/// A PNG's sides from its header, before it is decoded.
fn png_sides(data: &[u8]) -> Option<(u32, u32)> {
    let d = png::Decoder::new(std::io::Cursor::new(data));
    let r = d.read_info().ok()?;
    let i = r.info();
    Some((i.width, i.height))
}

fn png_rgba(data: &[u8]) -> Option<Vec<u8>> {
    let mut d = png::Decoder::new(std::io::Cursor::new(data));
    d.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut r = d.read_info().ok()?;
    let mut buf = vec![0; r.output_buffer_size()];
    let info = r.next_frame(&mut buf).ok()?;
    let px = &buf[..info.buffer_size()];
    Some(match info.color_type {
        png::ColorType::Rgba => px.to_vec(),
        png::ColorType::Rgb => px.chunks_exact(3).flat_map(|c| [c[0], c[1], c[2], 255]).collect(),
        png::ColorType::GrayscaleAlpha => px.chunks_exact(2).flat_map(|c| [c[0], c[0], c[0], c[1]]).collect(),
        png::ColorType::Grayscale => px.iter().flat_map(|&v| [v, v, v, 255]).collect(),
        png::ColorType::Indexed => return None,
    })
}

impl Kitty {
    fn say(out: &mut Vec<String>, id: u32, why: Option<&str>) {
        out.push(match why {
            Some(w) => format!("error kitty i={id}: {w}"),
            None => format!("kitty i={id} OK"),
        });
    }

    /// One kitty string as the core hands it over, `ESC _G ... ESC \`, sent
    /// with the cursor at `col`, `row`; what is to be said on the base's
    /// `out` is added to `out`.
    pub fn string(&mut self, whole: &[u8], col: usize, row: usize, out: &mut Vec<String>) {
        let body = whole.strip_prefix(b"\x1b_G").unwrap_or(whole);
        let body = body.strip_suffix(b"\x1b\\").unwrap_or(body);
        let (k, payload) = keys(body);
        if let Some(mut pend) = self.pending.take() {
            if !k.other {
                if !pend.refused && !pend.take(payload) {
                    Self::say(out, pend.k.i, Some("too big"));
                    pend.refused = true;
                }
                if k.m != 0 {
                    self.pending = Some(pend);
                } else if !pend.refused {
                    self.finish(pend, col, row, out);
                }
                return;
            }
            // One whose chunks stopped coming is let go.
        }
        let a = if k.a == 0 { b't' } else { k.a };
        if k.u != 0 || k.o != 0 {
            return self.refuse(k, "not offered", out);
        }
        match a {
            b't' | b'T' | b'q' => {
                if k.t != 0 && k.t != b'd' {
                    return self.refuse(k, "not offered", out);
                }
                let mut pend = Pending::new(k.clone());
                if !pend.take(payload) {
                    Self::say(out, k.i, Some("too big"));
                    pend.refused = true;
                }
                if k.m != 0 {
                    self.pending = Some(pend);
                } else if !pend.refused {
                    self.finish(pend, col, row, out);
                }
            }
            b'p' => {
                if k.i != 0 && self.images.contains_key(&k.i) {
                    self.place(&k, k.i, col, row, out);
                } else {
                    Self::say(out, k.i, Some("no such image"));
                }
            }
            b'd' => self.delete(&k, out),
            _ => self.refuse(k, "not offered", out),
        }
    }

    /// A refusal on a string that said more chunks follow: they are let go.
    fn refuse(&mut self, k: Keys, why: &str, out: &mut Vec<String>) {
        Self::say(out, k.i, Some(why));
        if k.m != 0 {
            let mut p = Pending::new(k);
            p.refused = true;
            self.pending = Some(p);
        }
    }

    fn finish(&mut self, mut pend: Pending, col: usize, row: usize, out: &mut Vec<String>) {
        let k = pend.k.clone();
        let a = if k.a == 0 { b't' } else { k.a };
        let f = if k.f == 0 { 32 } else { k.f };
        pend.close();
        let data = std::mem::take(&mut pend.data);
        let old = if a != b'q' && k.i != 0 { self.images.get(&k.i).map(|im| (im.w * im.h * 4) as usize) } else { None };
        if a != b'q' && old.is_none() && self.images.len() >= IMAGES {
            return Self::say(out, k.i, Some("too many"));
        }
        if ![100, 24, 32].contains(&f) {
            return Self::say(out, k.i, Some("not offered"));
        }
        if pend.bad || data.is_empty() {
            return Self::say(out, k.i, Some("does not decode"));
        }
        let (w, h) = if f == 100 {
            match png_sides(&data) {
                None => return Self::say(out, k.i, Some("does not decode")),
                Some(s) => s,
            }
        } else {
            if k.s < 1 || k.v < 1 {
                return Self::say(out, k.i, Some("does not decode"));
            }
            (k.s as u32, k.v as u32)
        };
        if w > SIDE || h > SIDE {
            return Self::say(out, k.i, Some("too big"));
        }
        let rgba = if f == 100 {
            match png_rgba(&data) {
                Some(px) if px.len() == (w * h * 4) as usize => px,
                _ => return Self::say(out, k.i, Some("does not decode")),
            }
        } else {
            let n = if f == 24 { 3 } else { 4 };
            if data.len() != (w * h) as usize * n {
                return Self::say(out, k.i, Some("does not decode"));
            }
            if f == 24 {
                data.chunks_exact(3).flat_map(|c| [c[0], c[1], c[2], 255]).collect()
            } else {
                data
            }
        };
        let count = (w * h * 4) as usize;
        if self.total - old.unwrap_or(0) + count > WALL {
            return Self::say(out, k.i, Some("too big"));
        }
        if a == b'q' {
            return Self::say(out, k.i, None);
        }
        let id = if k.i != 0 {
            k.i
        } else {
            let id = 0xF000_0000 + (self.auto_id & 0x0FFF_FFFF);
            self.auto_id = self.auto_id.wrapping_add(1);
            id
        };
        self.total = self.total - old.unwrap_or(0) + count;
        self.images.insert(id, Image { w, h, rgba: Arc::new(rgba) });
        if a == b'T' {
            self.place(&k, id, col, row, out);
        }
        self.changed = true;
    }

    /// Image `id` placed at the cursor.
    fn place(&mut self, k: &Keys, id: u32, col: usize, row: usize, out: &mut Vec<String>) {
        let at = if k.p != 0 { self.places.iter().position(|pl| pl.image == id && pl.pid == k.p) } else { None };
        if at.is_none() && self.places.len() >= PLACES {
            return Self::say(out, k.i, Some("too many"));
        }
        self.seq += 1;
        let pl = Place { image: id, pid: k.p, col, row, c: k.c, r: k.r, z: k.z, seq: self.seq };
        match at {
            Some(n) => self.places[n] = pl,
            None => self.places.push(pl),
        }
        self.changed = true;
    }

    fn forget(&mut self, id: u32) {
        self.places.retain(|pl| pl.image != id);
        if let Some(im) = self.images.remove(&id) {
            self.total -= (im.w * im.h * 4) as usize;
        }
    }

    fn delete(&mut self, k: &Keys, out: &mut Vec<String>) {
        let d = if k.d == 0 { b'a' } else { k.d };
        let before = self.places.len();
        match d {
            b'a' | b'A' => {
                self.places.clear();
                if d == b'A' {
                    let ids: Vec<u32> = self.images.keys().copied().collect();
                    for id in ids {
                        self.forget(id);
                    }
                }
            }
            b'i' | b'I' | b'p' | b'P' => {
                if k.i == 0 || !self.images.contains_key(&k.i) || (matches!(d, b'p' | b'P') && k.p == 0) {
                    return;
                }
                self.places.retain(|pl| !(pl.image == k.i && (k.p == 0 || pl.pid == k.p)));
                if d == b'I' && !self.places.iter().any(|pl| pl.image == k.i) {
                    self.forget(k.i);
                }
            }
            _ => return Self::say(out, k.i, Some("not offered")),
        }
        if self.places.len() != before {
            self.changed = true;
        }
    }

    /// The placements as pictures over a grid whose cell is `cw` by `ch`
    /// and whose top left is at `ox`, `oy` on the glass, in the order they
    /// are drawn: by z, then as placed. Each is sized from the cell now: c
    /// and r both given, stretched to fill them; one given, the other from
    /// the picture's shape; neither, its own pixels.
    pub fn pictures(&self, (cw, ch): (u32, u32), (ox, oy): (u32, u32)) -> Vec<Picture> {
        let mut order: Vec<&Place> = self.places.iter().collect();
        order.sort_by_key(|pl| (pl.z, pl.seq));
        order
            .into_iter()
            .filter_map(|pl| {
                let im = self.images.get(&pl.image)?;
                let (w, h) = if pl.c > 0 && pl.r > 0 {
                    (pl.c * cw, pl.r * ch)
                } else if pl.c > 0 {
                    let w = pl.c * cw;
                    (w, (w as f64 * im.h as f64 / im.w as f64 + 0.5) as u32)
                } else if pl.r > 0 {
                    let h = pl.r * ch;
                    ((h as f64 * im.w as f64 / im.h as f64 + 0.5) as u32, h)
                } else {
                    (im.w, im.h)
                };
                let at = ((ox + pl.col as u32 * cw) as i32, (oy + pl.row as u32 * ch) as i32);
                Some(Picture { rgba: im.rgba.clone(), w: im.w, h: im.h, at, size: (w.max(1), h.max(1)) })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn b64(data: &[u8]) -> Vec<u8> {
        const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut out = Vec::new();
        for c in data.chunks(3) {
            let n = c.len();
            let v = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
            for k in 0..4 {
                out.push(if k <= n { T[(v >> (18 - 6 * k) & 63) as usize] } else { b'=' });
            }
        }
        out
    }

    fn string(keys: &str, data: &[u8]) -> Vec<u8> {
        let mut s = format!("\x1b_G{keys};").into_bytes();
        s.extend(b64(data));
        s.extend(b"\x1b\\");
        s
    }

    #[test]
    fn raw_rgb_in_chunks_is_placed_and_deleted() {
        let mut k = Kitty::default();
        let mut out = Vec::new();
        let px: Vec<u8> = [0u8, 255, 0].repeat(16);
        let all = b64(&px);
        let mut first = b"\x1b_Ga=T,f=24,s=4,v=4,i=2,c=3,r=1,q=2,m=1;".to_vec();
        first.extend(&all[..8]);
        first.extend(b"\x1b\\");
        k.string(&first, 5, 2, &mut out);
        let mut last = b"\x1b_Gm=0;".to_vec();
        last.extend(&all[8..]);
        last.extend(b"\x1b\\");
        k.string(&last, 5, 2, &mut out);
        assert!(out.is_empty(), "{out:?}");
        let p = k.pictures((10, 20), (1, 2));
        assert_eq!(p.len(), 1);
        assert_eq!((p[0].at, p[0].size, p[0].w), ((51, 42), (30, 20), 4));
        assert_eq!(&p[0].rgba[..4], &[0, 255, 0, 255]);
        k.string(&string("a=d,d=I,i=2,q=2", b""), 0, 0, &mut out);
        assert!(k.pictures((10, 20), (0, 0)).is_empty());
        k.string(&string("a=p,i=2,q=2", b""), 0, 0, &mut out);
        assert_eq!(out, ["error kitty i=2: no such image"]);
    }

    #[test]
    fn refusals_are_said() {
        let mut k = Kitty::default();
        let mut out = Vec::new();
        k.string(&string("a=T,f=100,i=12,q=2", b"not a png at all"), 0, 0, &mut out);
        k.string(&string("a=T,f=24,s=4,v=4,i=13,q=2", &[255; 47]), 0, 0, &mut out);
        k.string(&string("a=T,t=f,f=100,i=14,q=2", b"/etc/passwd"), 0, 0, &mut out);
        k.string(&string("a=d,d=I,i=77,q=2", b""), 0, 0, &mut out);
        assert_eq!(out, ["error kitty i=12: does not decode", "error kitty i=13: does not decode", "error kitty i=14: not offered"]);
    }
}
