//! `pixels` held to the cells Python makes of the same pictures
//! (`tests/golden/pixels.py` makes `pixels.bin`), and timed.

use bitwall::frame::{DEFAULT, Frame};
use bitwall::pixels::{self, MODES, Mode};
use std::time::Instant;

struct Reader<'a> {
    d: &'a [u8],
    o: usize,
}

impl Reader<'_> {
    fn take(&mut self, n: usize) -> &[u8] {
        let s = &self.d[self.o..self.o + n];
        self.o += n;
        s
    }
    fn u8(&mut self) -> u8 {
        self.take(1)[0]
    }
    fn u16(&mut self) -> usize {
        u16::from_le_bytes(self.take(2).try_into().unwrap()) as usize
    }
    fn u32(&mut self) -> usize {
        u32::from_le_bytes(self.take(4).try_into().unwrap()) as usize
    }
    fn words(&mut self, n: usize) -> Vec<i64> {
        self.take(4 * n).chunks(4).map(|b| i32::from_le_bytes(b.try_into().unwrap()) as i64).collect()
    }
}

#[test]
fn golden() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/pixels.bin");
    let d = std::fs::read(path).expect("pixels.bin: run tests/golden/pixels.py");
    assert_eq!(&d[..4], b"BWPX");
    let mut r = Reader { d: &d, o: 4 };
    let cases = r.u32();
    let mut failed = Vec::new();
    let mut cells = 0;
    let mut reused = Frame::new(0, 0);
    for _ in 0..cases {
        let len = r.u16();
        let name = String::from_utf8(r.take(len).to_vec()).unwrap();
        let mode = MODES[r.u8() as usize];
        let (w, h, cols, rows) = (r.u32(), r.u32(), r.u32(), r.u32());
        let rgb = r.take(w * h * 3).to_vec();
        let n = cols * rows;
        let (ch, fg, bg) = (r.words(n), r.words(n), r.words(n));
        let got = pixels::to_frame(&rgb, w, h, mode, cols, rows);
        pixels::to_frame_into(&mut reused, &rgb, w, h, mode, cols, rows);
        assert_eq!(got, reused, "{name}: into differs");
        cells += n;
        for i in 0..n {
            let g = (got.ch[i] as i64, got.fg[i] as i64, got.bg[i] as i64);
            let want = (ch[i] as u32 as i64, fg[i], bg[i]);
            if g != want {
                failed.push(format!(
                    "{name} cell {},{}: got {:x?} want {:x?}",
                    i % cols,
                    i / cols,
                    g,
                    want
                ));
                break;
            }
        }
    }
    assert_eq!(r.o, d.len());
    assert!(failed.is_empty(), "{} of {cases} cases differ:\n{}", failed.len(), failed.join("\n"));
    eprintln!("{cases} cases, {cells} cells, cell for cell");
}

/// A picture of another size is sampled, nearest pixel: one twice the
/// size is its even pixels.
#[test]
fn samples_other_sizes() {
    let (w, h) = (34, 20);
    let small: Vec<u8> = (0..w * h * 3).map(|i| (i * 37 % 251) as u8).collect();
    let mut big = vec![0u8; 4 * w * h * 3];
    for y in 0..2 * h {
        for x in 0..2 * w {
            let s = ((y / 2) * w + x / 2) * 3;
            let o = (y * 2 * w + x) * 3;
            big[o..o + 3].copy_from_slice(&small[s..s + 3]);
        }
    }
    for mode in MODES {
        let (tw, th) = mode.tile();
        let (cols, rows) = (w / tw, h / th);
        let a = pixels::to_frame(&small[..], w, h, mode, cols, rows);
        let b = pixels::to_frame(&big, 2 * w, 2 * h, mode, cols, rows);
        assert_eq!(a, b, "{mode:?}");
    }
}

#[test]
fn empty_and_reused() {
    let f = pixels::to_frame(&[], 0, 0, Mode::Octant, 3, 2);
    assert_eq!(f.ch, vec![' ' as u32; 6]);
    assert_eq!(f.bg, vec![DEFAULT; 6]);
    let f = pixels::to_frame(&[1, 2, 3], 1, 1, Mode::Octant, 0, 0);
    assert_eq!((f.cols, f.rows, f.ch.len()), (0, 0, 0));
    // A frame reused at another size is that size, attributes clear.
    let mut g = Frame::new(5, 5);
    g.attrs.fill(1);
    pixels::to_frame_into(&mut g, &[9, 9, 9], 1, 1, Mode::HalfBlock, 2, 1);
    assert_eq!((g.cols, g.rows), (2, 1));
    assert_eq!(g.attrs, vec![0, 0]);
    assert_eq!(g.bg, vec![0x090909, 0x090909]);
}

#[test]
fn tables() {
    // Every pattern one character, and every character one pattern.
    for t in [&pixels::OCTANT[..], &pixels::SEXTANT[..], &pixels::BRAILLE[..]] {
        let mut s = t.to_vec();
        s.sort();
        s.dedup();
        assert_eq!(s.len(), t.len());
    }
    assert_eq!(pixels::OCTANT[4], 0x1CD00);
    assert_eq!(pixels::OCTANT[254], 0x1CDE5);
    assert_eq!(pixels::SEXTANT[1], 0x1FB00);
    assert_eq!(pixels::SEXTANT[62], 0x1FB3B);
    assert_eq!(pixels::FINE[0b0101_0101], '▌' as u32);
}

#[test]
fn falls_back_to_what_the_wall_draws() {
    let only = |ranges: &'static [(u32, u32)]| {
        move |s: &str| s.chars().all(|c| ranges.iter().any(|&(l, h)| (l..=h).contains(&(c as u32))))
    };
    let all = |_: &str| true;
    assert_eq!(Mode::Octant.drawable(&all), Mode::Octant);
    // The painter's and web wall's drawn ranges without the octants.
    let legacy = only(&[(0x2580, 0x259F), (0x2800, 0x28FF), (0x1FB00, 0x1FB3B)]);
    assert_eq!(Mode::Octant.drawable(&legacy), Mode::Sextant);
    assert_eq!(Mode::Fine.drawable(&legacy), Mode::Fine);
    let blocks = only(&[(0x2580, 0x259F)]);
    assert_eq!(Mode::Octant.drawable(&blocks), Mode::Quadrant);
    assert_eq!(Mode::Braille.drawable(&blocks), Mode::HalfBlock);
    let half = only(&[(0x2580, 0x2580)]);
    assert_eq!(Mode::Octant.drawable(&half), Mode::HalfBlock);
    let nothing = only(&[(0x20, 0x7e)]);
    assert_eq!(Mode::Octant.drawable(&nothing), Mode::Flat);
    let drawn = only(&[(0x2580, 0x259F), (0x1CD00, 0x1CDE5), (0x1CEA0, 0x1CEAF), (0x1FB70, 0x1FB8B), (0x1FBE4, 0x1FBE7)]);
    assert_eq!(Mode::Octant.drawable(&drawn), Mode::Octant);
}

/// A picture something like a film's at `w * h`.
fn film(w: usize, h: usize) -> Vec<u8> {
    let mut v = Vec::with_capacity(w * h * 3);
    let mut seed = 1u32;
    for y in 0..h {
        for x in 0..w {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            let n = (seed >> 28) as usize;
            let disc = ((x as i64 - w as i64 / 2).pow(2) + (y as i64 - h as i64 / 2).pow(2)) < (h as i64 / 3).pow(2);
            v.push(((x * 255 / w + n) & 255) as u8);
            v.push(if disc { 240 } else { (y * 255 / h) as u8 });
            v.push((((x + y) * 3 + n) & 255) as u8);
        }
    }
    v
}

/// Noise over the whole range: no tile is flat, so every one is searched.
fn noise(w: usize, h: usize) -> Vec<u8> {
    let mut seed = 7u32;
    (0..w * h * 3)
        .map(|_| {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            (seed >> 24) as u8
        })
        .collect()
}

/// `cargo test --release --test pixels bench -- --ignored --nocapture`
#[test]
#[ignore]
fn bench() {
    let (cols, rows) = (1706, 480);
    let threads = rayon::current_num_threads();
    let one = rayon::ThreadPoolBuilder::new().num_threads(1).build().unwrap();
    for (picture, make) in [("film", film as fn(usize, usize) -> Vec<u8>), ("noise", noise)] {
    for mode in MODES {
        let (tw, th) = mode.tile();
        let (w, h) = (cols * tw, rows * th);
        let rgb = make(w, h);
        let mut f = Frame::new(0, 0);
        let mut run = |_label: &str, n: usize| {
            let start = Instant::now();
            for _ in 0..n {
                pixels::to_frame_into(&mut f, &rgb, w, h, mode, cols, rows);
            }
            start.elapsed().as_secs_f64() * 1000.0 / n as f64
        };
        run("warm", 1);
        let all = run("all", 20);
        let single = one.install(|| run("one", 5));
        eprintln!("{picture:<6} {:<10} {w:>4}x{h:<4} px  {single:7.2} ms one thread  {all:6.2} ms {threads} threads", format!("{mode:?}"));
    }
    }
}
