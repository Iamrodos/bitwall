//! `frame` held to the bytes Python writes for the same arrays
//! (`tests/golden/frame.py` makes `frame.bin`), and timed.

use bitwall::frame::{self, Depth, Frame};
use std::time::Instant;

const DEPTHS: [Depth; 5] = [Depth::True, Depth::Xterm256, Depth::Ansi16, Depth::Grey, Depth::Mono];

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
    fn frame(&mut self) -> Frame {
        let cols = self.u32();
        let rows = self.u32();
        let n = cols * rows;
        let ch = self.take(4 * n).chunks(4).map(|b| u32::from_le_bytes(b.try_into().unwrap())).collect();
        let fg = self.take(4 * n).chunks(4).map(|b| i32::from_le_bytes(b.try_into().unwrap())).collect();
        let bg = self.take(4 * n).chunks(4).map(|b| i32::from_le_bytes(b.try_into().unwrap())).collect();
        let attrs = self.take(n).to_vec();
        Frame { cols, rows, ch, fg, bg, attrs }
    }
}

#[test]
fn golden() {
    // BITWALL_FRAME_GOLDEN names another file in the same format, for a
    // check too large to keep.
    let path = std::env::var("BITWALL_FRAME_GOLDEN")
        .unwrap_or_else(|_| concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/frame.bin").into());
    let d = std::fs::read(&path).expect("frame.bin: run tests/golden/frame.py");
    assert_eq!(&d[..4], b"BWFR");
    let mut r = Reader { d: &d, o: 4 };
    let cases = r.u32();
    let mut outputs = 0;
    let mut failed = Vec::new();
    for _ in 0..cases {
        let len = r.u16();
        let name = String::from_utf8(r.take(len).to_vec()).unwrap();
        let cur = r.frame();
        let prev = if r.u8() != 0 { Some(r.frame()) } else { None };
        for _ in 0..r.u8() {
            let full = r.u8() != 0;
            let depth = DEPTHS[r.u8() as usize];
            let len = r.u32();
            let want = r.take(len);
            let got = frame::serialise(&cur, prev.as_ref(), full, depth);
            outputs += 1;
            if got != want {
                let at = got.iter().zip(want).position(|(a, b)| a != b).unwrap_or(got.len().min(want.len()));
                let lo = at.saturating_sub(40);
                failed.push(format!(
                    "{name} full={full} {depth:?}: differs at byte {at} of {} (want {})\n  got  {:?}\n  want {:?}",
                    got.len(),
                    want.len(),
                    String::from_utf8_lossy(&got[lo..(at + 40).min(got.len())]),
                    String::from_utf8_lossy(&want[lo..(at + 40).min(want.len())]),
                ));
            }
        }
    }
    assert_eq!(r.o, d.len());
    assert!(failed.is_empty(), "{} of {outputs} outputs differ:\n{}", failed.len(), failed.join("\n"));
    eprintln!("{cases} cases, {outputs} outputs, byte for byte");
}

#[test]
fn text_steps_wide_glyphs() {
    let mut f = Frame::new(6, 2);
    f.text(1, 0, "a漢b🌈", 0x112233, -1, frame::BOLD);
    assert_eq!(f.ch[..6], ['\u{20}' as u32, 'a' as u32, 0x6f22, 0, 'b' as u32, 0x1f308]);
    assert_eq!(f.fg[3], 0x112233);
    f.text(0, 5, "off the bottom", 0, 0, 0);
    f.text(5, 1, "xy", -1, -1, 0);
    assert_eq!(f.ch[6..], [32, 32, 32, 32, 32, 'x' as u32]);
}

#[test]
fn depth_names() {
    for d in DEPTHS {
        assert_eq!(Depth::from_name(d.name()), d);
    }
    assert_eq!(Depth::from_name("unheard of"), Depth::True);
}

/// A frame the size of a large wall, its picture something like a film's.
fn film(cols: usize, rows: usize, t: u32) -> Frame {
    let mut f = Frame::new(cols, rows);
    for y in 0..rows {
        for x in 0..cols {
            let i = y * cols + x;
            let r = (x as u32 * 255 / cols as u32 + t) & 255;
            let g = (y as u32 * 255 / rows as u32 + t / 2) & 255;
            let b = ((x + y) as u32 * 3 + t) & 255;
            f.ch[i] = '▀' as u32;
            f.fg[i] = ((r << 16) | (g << 8) | b) as i32;
            f.bg[i] = ((r << 16) | (g << 8) | (b ^ 0x40)) as i32;
        }
    }
    f
}

fn time(label: &str, mut f: impl FnMut() -> usize) {
    let mut bytes = f();
    let start = Instant::now();
    let n = 20;
    for _ in 0..n {
        bytes = f();
    }
    let ms = start.elapsed().as_secs_f64() * 1000.0 / n as f64;
    eprintln!("{label:<34} {ms:7.2} ms  {bytes} bytes");
}

/// `cargo test --release --test frame bench -- --ignored --nocapture`
#[test]
#[ignore]
fn bench() {
    let (cols, rows) = (1706, 480);
    let a = film(cols, rows, 0);
    let b = film(cols, rows, 3);
    let mut sparse = a.clone();
    for i in (0..cols * rows).step_by(97) {
        sparse.fg[i] ^= 0x010101;
    }
    let mut out = Vec::new();
    let mut run = |cur: &Frame, prev: Option<&Frame>, full, depth| {
        out.clear();
        frame::serialise_into(&mut out, cur, prev, full, depth);
        out.len()
    };
    time("full repaint, truecolour", || run(&a, None, true, Depth::True));
    time("diff, every cell changed", || run(&b, Some(&a), false, Depth::True));
    time("diff, one cell in 97", || run(&sparse, Some(&a), false, Depth::True));
    time("diff, nothing changed", || run(&a, Some(&a), false, Depth::True));
    time("diff, every cell changed, 256", || run(&b, Some(&a), false, Depth::Xterm256));
    time("diff, every cell changed, 16", || run(&b, Some(&a), false, Depth::Ansi16));
}
