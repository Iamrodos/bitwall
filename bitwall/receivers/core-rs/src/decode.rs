//! A video layer's frame decoded to RGBA, for a receiver that composes
//! the layers itself. Each codec has a small decoder in Rust of its own
//! and nothing else: `qoi`, `png` (with its inflate), and `zune-jpeg`,
//! which reads baseline and progressive JPEGs and is faster than
//! `jpeg-decoder`. Pixels are taken as sRGB: no gamma or profile is
//! applied, as PROTOCOL.md says of a frame.

use crate::video::{Codec, Frame};

/// A picture as RGBA, w by h, four bytes a pixel, alpha straight.
pub struct Rgba {
    pub w: u32,
    pub h: u32,
    pub px: Vec<u8>,
}

/// The frame's pixels, or why they could not be had.
pub fn rgba(frame: &Frame) -> Result<Rgba, String> {
    let got = match frame.codec {
        Codec::Qoi => qoi(&frame.data),
        Codec::Png => png(&frame.data),
        Codec::Jpeg => jpeg(&frame.data),
    }?;
    if (got.w, got.h) != (frame.w, frame.h) {
        return Err(format!("{}x{} where {}x{} was said", got.w, got.h, frame.w, frame.h));
    }
    Ok(got)
}

fn qoi(data: &[u8]) -> Result<Rgba, String> {
    let (head, px) = qoi::decode_to_vec(data).map_err(|e| e.to_string())?;
    let px = if head.channels.is_rgba() { px } else { px.chunks_exact(3).flat_map(|p| [p[0], p[1], p[2], 255]).collect() };
    Ok(Rgba { w: head.width, h: head.height, px })
}

fn png(data: &[u8]) -> Result<Rgba, String> {
    let mut d = png::Decoder::new(std::io::Cursor::new(data));
    // Palette and low bit depths opened out, sixteen bits taken to eight.
    d.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = d.read_info().map_err(|e| e.to_string())?;
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).map_err(|e| e.to_string())?;
    let n = (info.width * info.height) as usize;
    let src = &buf[..info.buffer_size()];
    let px: Vec<u8> = match info.color_type {
        png::ColorType::Rgba => src.to_vec(),
        png::ColorType::Rgb => src.chunks_exact(3).flat_map(|p| [p[0], p[1], p[2], 255]).collect(),
        png::ColorType::GrayscaleAlpha => src.chunks_exact(2).flat_map(|p| [p[0], p[0], p[0], p[1]]).collect(),
        png::ColorType::Grayscale => src.iter().flat_map(|&g| [g, g, g, 255]).collect(),
        png::ColorType::Indexed => return Err("a palette left unexpanded".into()),
    };
    if px.len() != n * 4 {
        return Err("short".into());
    }
    Ok(Rgba { w: info.width, h: info.height, px })
}

fn jpeg(data: &[u8]) -> Result<Rgba, String> {
    use zune_jpeg::zune_core::colorspace::ColorSpace;
    use zune_jpeg::zune_core::options::DecoderOptions;
    let opts = DecoderOptions::default().jpeg_set_out_colorspace(ColorSpace::RGBA);
    let mut d = zune_jpeg::JpegDecoder::new_with_options(data, opts);
    let px = d.decode().map_err(|e| format!("{e:?}"))?;
    let (w, h) = d.dimensions().ok_or("no size")?;
    Ok(Rgba { w: w as u32, h: h as u32, px })
}
