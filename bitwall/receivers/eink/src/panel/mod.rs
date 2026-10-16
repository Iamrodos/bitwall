//! Where a push goes: a panel, behind one small trait. `file` writes each
//! push as a picture and a line of log, for tests and for a machine with
//! no panel; `it8951` is the Waveshare HAT on a Pi's SPI, `usb` the same
//! board on USB.

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::geom::Rect;
use crate::policy::{Clean, Mode};

#[cfg(all(target_os = "linux", feature = "it8951"))]
pub mod it8951;
#[cfg(feature = "usb")]
pub mod usb;

pub trait Panel {
    /// The panel's pixels, as it scans, seen from the front.
    fn size(&self) -> (u32, u32);
    /// Clear the whole panel to white through every level (INIT).
    fn clear(&mut self) -> io::Result<()>;
    /// Load greys for `r`, a panel rectangle whose left edge and width are
    /// aligned, row by row, then refresh `r` in `mode`.
    fn push(&mut self, r: &Rect, grey: &[u8], mode: Mode) -> io::Result<()>;
    /// Rest the controller between pushes; the picture stays.
    fn sleep(&mut self) -> io::Result<()> {
        Ok(())
    }
    /// Start the controller again after it stopped answering, as it was
    /// first opened; what the panel shows is then not to be trusted.
    fn reset(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// A clean refresh: each of `steps` over the whole panel, then `image`
/// (the whole panel's greys) in GC16.
pub fn clean<P: Panel + ?Sized>(p: &mut P, steps: &[Clean], image: &[u8]) -> io::Result<()> {
    let (w, h) = p.size();
    let all = Rect::new(0, 0, w, h);
    for s in steps {
        match s {
            Clean::Init => p.clear()?,
            Clean::Black => p.push(&all, &vec![0; image.len()], Mode::Gc16)?,
            Clean::White => p.push(&all, &vec![255; image.len()], Mode::Gc16)?,
        }
    }
    p.push(&all, image, Mode::Gc16)
}

/// A panel that is a directory: every push is `push-NNNN.png`, the whole
/// panel as it would look after it, and a line in `log` saying the box and
/// the mode.
pub struct FilePanel {
    dir: PathBuf,
    w: u32,
    h: u32,
    image: Vec<u8>,
    n: u32,
    log: BufWriter<File>,
    started: Instant,
}

impl FilePanel {
    pub fn new(dir: &Path, w: u32, h: u32) -> io::Result<FilePanel> {
        fs::create_dir_all(dir)?;
        let log = OpenOptions::new().create(true).write(true).truncate(true).open(dir.join("log"))?;
        Ok(FilePanel { dir: dir.to_path_buf(), w, h, image: vec![255; (w * h) as usize], n: 0, log: BufWriter::new(log), started: Instant::now() })
    }

    /// The panel as it now looks.
    pub fn image(&self) -> &[u8] {
        &self.image
    }

    fn write(&mut self, what: &str) -> io::Result<()> {
        self.n += 1;
        let path = self.dir.join(format!("push-{:04}.png", self.n));
        write_png(&path, self.w, self.h, &self.image)?;
        writeln!(self.log, "{:.3} {} {}", self.started.elapsed().as_secs_f64(), self.n, what)?;
        self.log.flush()
    }
}

impl Panel for FilePanel {
    fn size(&self) -> (u32, u32) {
        (self.w, self.h)
    }

    fn clear(&mut self) -> io::Result<()> {
        self.image.fill(255);
        self.write(&format!("box 0,0 {}x{} mode INIT", self.w, self.h))
    }

    fn push(&mut self, r: &Rect, grey: &[u8], mode: Mode) -> io::Result<()> {
        assert_eq!(grey.len(), r.area() as usize);
        for y in 0..r.h {
            let a = ((r.y + y) * self.w + r.x) as usize;
            self.image[a..a + r.w as usize].copy_from_slice(&grey[(y * r.w) as usize..((y + 1) * r.w) as usize]);
        }
        let mode = match mode {
            Mode::Init => "INIT",
            Mode::Du => "DU",
            Mode::Gc16 => "GC16",
        };
        self.write(&format!("box {},{} {}x{} mode {}", r.x, r.y, r.w, r.h, mode))
    }
}

/// Greys `w` by `h` as an 8-bit greyscale PNG.
pub fn write_png(path: &Path, w: u32, h: u32, grey: &[u8]) -> io::Result<()> {
    let f = BufWriter::new(File::create(path)?);
    let mut e = png::Encoder::new(f, w, h);
    e.set_color(png::ColorType::Grayscale);
    e.set_depth(png::BitDepth::Eight);
    e.set_compression(png::Compression::Fast);
    let mut wr = e.write_header().map_err(io::Error::other)?;
    wr.write_image_data(grey).map_err(io::Error::other)?;
    Ok(())
}

/// An RGBA picture from a PNG file, and its size.
pub fn read_png(path: &Path) -> io::Result<(Vec<u8>, u32, u32)> {
    let mut d = png::Decoder::new(io::BufReader::new(File::open(path)?));
    d.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut r = d.read_info().map_err(io::Error::other)?;
    let mut buf = vec![0; r.output_buffer_size()];
    let info = r.next_frame(&mut buf).map_err(io::Error::other)?;
    let (w, h) = (info.width, info.height);
    let px = &buf[..info.buffer_size()];
    let rgba = match info.color_type {
        png::ColorType::Rgba => px.to_vec(),
        png::ColorType::Rgb => px.chunks(3).flat_map(|c| [c[0], c[1], c[2], 255]).collect(),
        png::ColorType::GrayscaleAlpha => px.chunks(2).flat_map(|c| [c[0], c[0], c[0], c[1]]).collect(),
        png::ColorType::Grayscale => px.iter().flat_map(|&v| [v, v, v, 255]).collect(),
        png::ColorType::Indexed => return Err(io::Error::other("indexed PNG not expanded")),
    };
    Ok((rgba, w, h))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_file_panel_writes_each_push() {
        let dir = std::env::temp_dir().join(format!("eink-file-{}", std::process::id()));
        let mut p = FilePanel::new(&dir, 32, 16).unwrap();
        p.clear().unwrap();
        p.push(&Rect::new(8, 4, 8, 2), &[0; 16], Mode::Du).unwrap();
        assert_eq!(p.image()[4 * 32 + 8], 0);
        assert_eq!(p.image()[4 * 32 + 7], 255);
        let log = fs::read_to_string(dir.join("log")).unwrap();
        assert!(log.lines().nth(1).unwrap().ends_with("2 box 8,4 8x2 mode DU"), "{log}");
        let (rgba, w, h) = read_png(&dir.join("push-0002.png")).unwrap();
        assert_eq!((w, h), (32, 16));
        assert_eq!(&rgba[(4 * 32 + 8) * 4..(4 * 32 + 8) * 4 + 4], &[0, 0, 0, 255]);
        fs::remove_dir_all(dir).unwrap();
    }
}
