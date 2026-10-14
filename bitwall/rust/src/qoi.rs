//! A QOI image of RGBA pixels, byte for byte the one `bitwall.video.qoi`
//! makes (`tests/golden/layer_qoi.py` holds it to that).
//!
//! Every pixel is said against the one before it, as a run, a small
//! difference, a luma difference, or whole; QOI's colour index is not
//! used, as Python does not use it, since it is the one op that cannot be
//! worked out a row at a time. Any QOI decoder reads it, with alpha exact.

/// The QOI of `rgba`, `width` by `height` pixels of four bytes each, row
/// by row. Panics where `rgba` is not exactly that many bytes.
pub fn encode(rgba: &[u8], width: u32, height: u32) -> Vec<u8> {
    let n = width as usize * height as usize;
    assert_eq!(rgba.len(), n * 4, "bitwall: qoi: {width}x{height} is {} bytes, not {}", n * 4, rgba.len());
    let mut out = Vec::with_capacity(14 + n + n / 4 + 8);
    out.extend_from_slice(b"qoif");
    out.extend_from_slice(&width.to_be_bytes());
    out.extend_from_slice(&height.to_be_bytes());
    out.extend_from_slice(&[4, 0]);
    let mut prev = [0u8, 0, 0, 255];
    // How far into a run of repeats this pixel is, counted 0 to 61.
    let mut run: u8 = 0;
    let mut in_run = false;
    for i in 0..n {
        let px = [rgba[4 * i], rgba[4 * i + 1], rgba[4 * i + 2], rgba[4 * i + 3]];
        if px == prev {
            run = if in_run { (run + 1) % 62 } else { 0 };
            in_run = true;
            let next_same = i + 1 < n && rgba[4 * i + 4..4 * i + 8] == px;
            if run == 61 || !next_same {
                out.push(0xC0 | run);
            }
            continue;
        }
        in_run = false;
        if px[3] != prev[3] {
            out.extend_from_slice(&[0xFF, px[0], px[1], px[2], px[3]]);
        } else {
            let dr = px[0].wrapping_sub(prev[0]) as i8 as i16;
            let dg = px[1].wrapping_sub(prev[1]) as i8 as i16;
            let db = px[2].wrapping_sub(prev[2]) as i8 as i16;
            let (rg, bg) = (dr - dg, db - dg);
            if (-2..=1).contains(&dr) && (-2..=1).contains(&dg) && (-2..=1).contains(&db) {
                out.push(0x40 | ((dr + 2) as u8) << 4 | ((dg + 2) as u8) << 2 | (db + 2) as u8);
            } else if (-32..=31).contains(&dg) && (-8..=7).contains(&rg) && (-8..=7).contains(&bg) {
                out.push(0x80 | (dg + 32) as u8);
                out.push(((rg + 8) as u8) << 4 | (bg + 8) as u8);
            } else {
                out.extend_from_slice(&[0xFE, px[0], px[1], px[2]]);
            }
        }
        prev = px;
    }
    out.extend_from_slice(&[0, 0, 0, 0, 0, 0, 0, 1]);
    out
}

#[cfg(test)]
mod tests {
    use super::encode;

    #[test]
    fn one_pixel_the_same_as_the_start_is_a_run() {
        assert_eq!(encode(&[0, 0, 0, 255], 1, 1)[14], 0xC0);
    }

    #[test]
    fn a_long_run_is_cut_at_sixty_two() {
        let q = encode(&[0, 0, 0, 255].repeat(130), 130, 1);
        assert_eq!(&q[14..q.len() - 8], &[0xFD, 0xFD, 0xC5]);
    }

    #[test]
    fn its_size_is_in_the_header() {
        let q = encode(&[9u8; 4 * 6], 3, 2);
        assert_eq!(&q[..14], b"qoif\0\0\0\x03\0\0\0\x02\x04\x00");
        assert_eq!(crate::protocol::image_size("qoi", &q), Some((3, 2)));
    }
}
