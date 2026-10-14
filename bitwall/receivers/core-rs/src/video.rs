//! A video layer's pictures as the wall keeps them: each frame's line read
//! and checked, its payload's header and end checked against it, and the
//! latest one held, still encoded, for the glass to draw however it draws.
//! Nothing here decodes a pixel; `decode` does, where a receiver wants it.

use std::time::Instant;

/// A frame's own size and its box, at most this each way.
pub const SIDE: u32 = 4096;
/// The most a frame's payload may be.
pub const FRAME_MAX: usize = 16 << 20;
/// How long a frame stays up when its line says no `hold`, in ms.
pub const HOLD_MS: u32 = 1000;
/// What a frame may be, as `images=` says it.
pub const IMAGES: [&str; 3] = ["qoi", "png", "jpeg"];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Codec {
    Qoi,
    Png,
    Jpeg,
}

impl Codec {
    pub fn parse(word: &str) -> Option<Codec> {
        match word {
            "qoi" => Some(Codec::Qoi),
            "png" => Some(Codec::Png),
            "jpeg" => Some(Codec::Jpeg),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Codec::Qoi => "qoi",
            Codec::Png => "png",
            Codec::Jpeg => "jpeg",
        }
    }
}

/// The frame a video layer shows: the bytes it was sent in, the codec they
/// are in, the picture's own size, where its top left goes on the glass,
/// the box it is drawn to there where that was asked, how long it holds
/// (None for `still`), when it came, and a count that changes with every
/// frame, so a glass that decoded it once knows when to again.
#[derive(Clone, Debug)]
pub struct Frame {
    pub codec: Codec,
    pub w: u32,
    pub h: u32,
    pub x: i32,
    pub y: i32,
    pub size: Option<(u32, u32)>,
    pub hold: Option<u32>,
    pub data: Vec<u8>,
    pub came: Instant,
    pub seq: u64,
}

impl Frame {
    /// Where it is drawn on the glass: its top left and the box it fills.
    pub fn placed(&self) -> (i32, i32, u32, u32) {
        let (w, h) = self.size.unwrap_or((self.w, self.h));
        (self.x, self.y, w, h)
    }

    /// As a picture or a mirror of it says it: its codec, its own size,
    /// where it went, and the box it was drawn to where it was scaled.
    pub fn words(&self) -> String {
        let mut out = format!("{} {}x{} at={},{}", self.codec.name(), self.w, self.h, self.x, self.y);
        if let Some((w, h)) = self.size {
            out += &format!(" size={w}x{h}");
        }
        out
    }
}

/// A frame's line, read and found good: what its payload must be.
#[derive(Clone, Debug)]
pub struct Head {
    pub codec: Codec,
    pub w: u32,
    pub h: u32,
    pub x: i32,
    pub y: i32,
    pub size: Option<(u32, u32)>,
    pub hold: Option<u32>,
    pub tag: String,
}

/// `<w>x<h>`, each 1 to SIDE.
pub fn side_pair(word: &str) -> Option<(u32, u32)> {
    let (w, h) = word.split_once('x')?;
    let ok = |v: &str| !v.is_empty() && v.len() <= 4 && v.bytes().all(|b| b.is_ascii_digit());
    if !ok(w) || !ok(h) {
        return None;
    }
    let (w, h) = (w.parse().ok()?, h.parse().ok()?);
    if (1..=SIDE).contains(&w) && (1..=SIDE).contains(&h) {
        Some((w, h))
    } else {
        None
    }
}

/// `frame <codec> <w>x<h> [at=<x>,<y>] [size=<w>x<h>] [hold=<ms>|still]
/// <bytes>`, the words as they came less any id: how many bytes follow,
/// whatever is wrong with the line so long as it says, and the line's
/// head or why it is refused.
pub fn read_line(words: &[&str], tag: &str) -> (usize, Result<Head, String>) {
    let count = match words.last() {
        Some(c) if words.len() >= 2 && c.len() <= 18 && c.bytes().all(|b| b.is_ascii_digit()) => c.parse::<usize>().unwrap_or(usize::MAX),
        _ => return (0, Err("no count".into())),
    };
    if count > FRAME_MAX {
        return (count, Err(format!("{}: too big", words[words.len() - 1])));
    }
    let codec = if words.len() >= 3 { Codec::parse(words[1]) } else { None };
    if words.len() >= 3 && codec.is_none() {
        return (count, Err(format!("{}: not offered", words[1])));
    }
    if words.len() < 4 {
        return (count, Err("no size".into()));
    }
    let Some((w, h)) = side_pair(words[2]) else {
        return (count, Err(format!("{}: bad size", words[2])));
    };
    let (mut x, mut y, mut size, mut hold) = (0i32, 0i32, None, Some(HOLD_MS));
    for word in &words[3..words.len() - 1] {
        let (key, value) = word.split_once('=').unwrap_or((word, ""));
        let good = match key {
            "at" => match value.split_once(',').map(|(a, b)| (a.parse::<i32>(), b.parse::<i32>())) {
                Some((Ok(a), Ok(b))) if a.abs() < 65536 && b.abs() < 65536 => {
                    (x, y) = (a, b);
                    true
                }
                _ => false,
            },
            "size" => {
                size = side_pair(value);
                size.is_some()
            }
            "hold" => {
                if value == "still" {
                    hold = None;
                    true
                } else if !value.is_empty() && value.len() <= 8 && value.bytes().all(|b| b.is_ascii_digit()) && value.parse::<u32>().unwrap() > 0 {
                    hold = Some(value.parse().unwrap());
                    true
                } else {
                    false
                }
            }
            _ => return (count, Err(format!("{word}: unknown option"))),
        };
        if !good {
            return (count, Err(format!("{word}: bad value")));
        }
    }
    (count, Ok(Head { codec: codec.unwrap(), w, h, x, y, size, hold, tag: tag.to_string() }))
}

/// (width, height) a picture's own header says, or None where `data` is
/// not a whole picture of `codec` a wall draws: its header and its end are
/// checked, not its pixels. A JPEG in four components, CMYK, is None.
pub fn image_size(codec: Codec, data: &[u8]) -> Option<(u32, u32)> {
    let n = data.len();
    let be = |b: &[u8]| b.iter().fold(0u32, |v, &x| v << 8 | x as u32);
    match codec {
        Codec::Qoi => {
            if n < 22 || &data[..4] != b"qoif" || data[n - 8..] != [0, 0, 0, 0, 0, 0, 0, 1] {
                return None;
            }
            Some((be(&data[4..8]), be(&data[8..12])))
        }
        Codec::Png => {
            if n < 45 || &data[..8] != b"\x89PNG\r\n\x1a\n" || &data[12..16] != b"IHDR" || &data[n - 8..n - 4] != b"IEND" {
                return None;
            }
            Some((be(&data[16..20]), be(&data[20..24])))
        }
        Codec::Jpeg => {
            if n < 4 || data[..2] != [0xff, 0xd8] || data[n - 2..] != [0xff, 0xd9] {
                return None;
            }
            let mut p = 2;
            while p + 4 <= n {
                if data[p] != 0xff {
                    return None;
                }
                let marker = data[p + 1];
                if marker == 0xff {
                    p += 1;
                    continue;
                }
                if marker == 0x01 || (0xd0..=0xd7).contains(&marker) {
                    p += 2;
                    continue;
                }
                let length = be(&data[p + 2..p + 4]) as usize;
                if (0xc0..=0xcf).contains(&marker) && ![0xc4, 0xc8, 0xcc].contains(&marker) {
                    if length < 8 || p + 10 > n || ![1, 3].contains(&data[p + 9]) {
                        return None;
                    }
                    let (h, w) = (be(&data[p + 5..p + 7]), be(&data[p + 7..p + 9]));
                    return if w > 0 && h > 0 { Some((w, h)) } else { None };
                }
                if marker == 0xda || length < 2 {
                    return None;
                }
                p += 2 + length;
            }
            None
        }
    }
}

/// RGBA pixels, w by h, as a QOI image: what a picture of the glass is.
pub fn qoi_encode(w: u32, h: u32, rgba: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(14 + rgba.len() / 2 + 8);
    out.extend_from_slice(b"qoif");
    out.extend_from_slice(&w.to_be_bytes());
    out.extend_from_slice(&h.to_be_bytes());
    out.extend_from_slice(&[4, 0]);
    let mut index = [[0u8; 4]; 64];
    let mut prev = [0u8, 0, 0, 255];
    let mut run = 0u8;
    let px_count = (w as usize) * (h as usize);
    for i in 0..px_count {
        let px = [rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2], rgba[i * 4 + 3]];
        if px == prev {
            run += 1;
            if run == 62 || i == px_count - 1 {
                out.push(0xc0 | (run - 1));
                run = 0;
            }
            continue;
        }
        if run > 0 {
            out.push(0xc0 | (run - 1));
            run = 0;
        }
        let hash = (px[0] as usize * 3 + px[1] as usize * 5 + px[2] as usize * 7 + px[3] as usize * 11) % 64;
        if index[hash] == px {
            out.push(hash as u8);
        } else {
            index[hash] = px;
            if px[3] == prev[3] {
                let dr = px[0].wrapping_sub(prev[0]) as i8;
                let dg = px[1].wrapping_sub(prev[1]) as i8;
                let db = px[2].wrapping_sub(prev[2]) as i8;
                let (dr_dg, db_dg) = (dr.wrapping_sub(dg), db.wrapping_sub(dg));
                if (-2..=1).contains(&dr) && (-2..=1).contains(&dg) && (-2..=1).contains(&db) {
                    out.push(0x40 | ((dr + 2) as u8) << 4 | ((dg + 2) as u8) << 2 | (db + 2) as u8);
                } else if (-32..=31).contains(&dg) && (-8..=7).contains(&dr_dg) && (-8..=7).contains(&db_dg) {
                    out.push(0x80 | (dg + 32) as u8);
                    out.push(((dr_dg + 8) as u8) << 4 | (db_dg + 8) as u8);
                } else {
                    out.extend_from_slice(&[0xfe, px[0], px[1], px[2]]);
                }
            } else {
                out.extend_from_slice(&[0xff, px[0], px[1], px[2], px[3]]);
            }
        }
        prev = px;
    }
    out.extend_from_slice(&[0, 0, 0, 0, 0, 0, 0, 1]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_a_frame_line() {
        let (n, h) = read_line(&["frame", "qoi", "4x3", "at=10,20", "size=40x30", "hold=still", "88"], "");
        let h = h.unwrap();
        assert_eq!(n, 88);
        assert_eq!((h.w, h.h, h.x, h.y, h.size, h.hold), (4, 3, 10, 20, Some((40, 30)), None));
        assert_eq!(read_line(&["frame", "vp9", "2x2", "5"], "").1.unwrap_err(), "vp9: not offered");
        assert_eq!(read_line(&["frame", "qoi", "2x2", "pts=40", "3"], "").1.unwrap_err(), "pts=40: unknown option");
        assert_eq!(read_line(&["frame", "qoi", "9999x2", "3"], "").1.unwrap_err(), "9999x2: bad size");
        assert_eq!(read_line(&["frame", "qoi", "2x2", "hold=0", "3"], "").1.unwrap_err(), "hold=0: bad value");
        assert_eq!(read_line(&["frame", "qoi", "4x3", "at=1,2"], "").1.unwrap_err(), "no count");
        assert_eq!(read_line(&["frame", "qoi", "2x2", "99999999"], "").1.unwrap_err(), "99999999: too big");
    }

    #[test]
    fn a_qoi_made_here_says_its_size() {
        let rgba: Vec<u8> = (0..5 * 3).flat_map(|i| [i as u8 * 10, 7, 200, 255]).collect();
        let q = qoi_encode(5, 3, &rgba);
        assert_eq!(image_size(Codec::Qoi, &q), Some((5, 3)));
    }
}
