//! `qoi` held to the bytes `bitwall.video.qoi` writes for the same pixels
//! (`tests/golden/layer_qoi.py` makes `layer_qoi.bin`).

use bitwall::qoi;

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
    fn u16(&mut self) -> usize {
        u16::from_le_bytes(self.take(2).try_into().unwrap()) as usize
    }
    fn u32(&mut self) -> usize {
        u32::from_le_bytes(self.take(4).try_into().unwrap()) as usize
    }
}

#[test]
fn golden() {
    let d = std::fs::read(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/layer_qoi.bin"))
        .expect("layer_qoi.bin: run tests/golden/layer_qoi.py");
    assert_eq!(&d[..4], b"BWQO");
    let mut r = Reader { d: &d, o: 4 };
    let cases = r.u32();
    let mut failed = Vec::new();
    for _ in 0..cases {
        let len = r.u16();
        let name = String::from_utf8(r.take(len).to_vec()).unwrap();
        let (w, h) = (r.u32(), r.u32());
        let rgba = r.take(w * h * 4).to_vec();
        let len = r.u32();
        let want = r.take(len).to_vec();
        if qoi::encode(&rgba, w as u32, h as u32) != want {
            failed.push(name);
        }
    }
    assert_eq!(r.o, d.len());
    assert!(cases >= 20);
    assert!(failed.is_empty(), "differ from Python: {failed:?}");
}
