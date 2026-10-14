//! A sample wall through the file panel: a grid with box lines, blocks,
//! braille, octants, letters and colours, a picture layer and a
//! translucent cells band over it, pushed as the wall would push it, then
//! changed twice. Writes the pushes to a directory and says how long each
//! part took.
//!
//!     cargo run --release --example sample -- <dir> [picture.png] [--screen] [--rotate N] [--ordered]

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bitwall_eink::panel::{write_png, FilePanel};
use bitwall_eink::scene::BOLD;
use bitwall_eink::{Cell, Cells, Dither, Eink, Layer, Options, Picture, Scene, Show};

mod common;
use common::{grid, picture, rgb, D};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let dir = PathBuf::from(args.first().expect("a directory to write to"));
    let pic = args.iter().skip(1).find(|a| a.ends_with(".png")).map(PathBuf::from);
    let paper = !args.iter().any(|a| a == "--screen");
    let ordered = args.iter().any(|a| a == "--ordered");
    let rotate = args.iter().position(|a| a == "--rotate").map(|i| args[i + 1].parse().unwrap()).unwrap_or(0);
    let _ = std::fs::remove_dir_all(&dir);
    let panel = FilePanel::new(&dir, 1872, 1404).unwrap();
    let o = Options { paper, rotate, dither: if ordered { Dither::Ordered } else { Dither::Diffuse }, ..Options::default() };
    let mut w = Eink::new(panel, o);
    let font = bitwall_eink::tune::FONT;
    let (cols, rows) = w.area(font);
    let (gw, gh) = w.glass();
    println!("glass {gw}x{gh}, font {font}: {cols}x{rows} cells of {:?}", w.cell(font));

    let (px, pw, ph) = picture(pic.as_ref());
    let rgba = Arc::new(px);
    let (cw, ch) = w.cell(font);
    // The picture sits right of the boxes, under a translucent band.
    let bx = (cw * 96) as i32;
    let by = (ch * 3) as i32;
    let bw = gw - bx as u32 - cw * 2;
    let bh = bw * ph / pw;
    let pic = Picture { rgba, w: pw, h: ph, at: (bx, by), size: (bw, bh) };
    let mut band = Cells::new(font, cols, rows, Cell::UNDRAWN);
    let band_row = ((by as u32 + bh) / ch) as usize - 2;
    band.text(96, band_row, &" ".repeat(cols - 98), D, 0x60000000 | 0x000000, 0);
    band.text(98, band_row, "a cells layer over the picture, three eighths opaque", rgb(255, 255, 255), 0x60000000, BOLD);

    let scene = |tick: u32| Scene {
        base: Some(grid(cols, rows, font, tick)),
        layers: vec![Layer { id: 1, show: Show::Picture(pic.clone()) }, Layer { id: 2, show: Show::Cells(band.clone()) }],
    };
    let t0 = Instant::now();
    let s = scene(1);
    let t = Instant::now();
    w.show(&s, t0);
    println!("compose, everything: {:.1} ms (picture scaled and dithered to {bw}x{bh} included)", t.elapsed().as_secs_f64() * 1e3);
    let p = w.tick(t0 + Duration::from_secs(1)).unwrap().unwrap();
    println!("push 1: {} {:?} ({:.0} ms to the file panel)", if p.clean { "clean" } else { "" }, p.boxes, p.secs * 1e3);

    // A small change: the counter's blocks.
    let s = scene(2);
    let t = Instant::now();
    w.show(&s, t0 + Duration::from_secs(2));
    let shown = t.elapsed().as_secs_f64() * 1e3;
    let t = Instant::now();
    let boxes = w.comp.boxes();
    let found = t.elapsed().as_secs_f64() * 1e3;
    println!("a few cells changed: compose {shown:.2} ms, boxes {found:.2} ms: {boxes:?}");
    let p = w.tick(t0 + Duration::from_secs(3)).unwrap().unwrap();
    println!("push 2: {:?}", p.boxes);

    // A bigger change: the bar chart and the counter.
    let s = scene(9);
    w.show(&s, t0 + Duration::from_secs(4));
    let p = w.tick(t0 + Duration::from_secs(5)).unwrap().unwrap();
    println!("push 3: {:?}", p.boxes);

    // The glass as composed, for looking at.
    write_png(&dir.join("glass.png"), gw, gh, &w.comp.glass).unwrap();

    // Dithering a whole glass of photograph.
    let full = Picture { rgba: pic.rgba.clone(), w: pw, h: ph, at: (0, 0), size: (gw, gh) };
    let mut c = bitwall_eink::compose::Compositor::new(gw, gh, bitwall_eink::grey::Tones { paper }, o.dither);
    let t = Instant::now();
    c.update(&Scene { base: None, layers: vec![Layer { id: 1, show: Show::Picture(full) }] });
    println!("a picture over the whole glass, scaled, dithered and composed: {:.1} ms", t.elapsed().as_secs_f64() * 1e3);
    write_png(&dir.join("photo.png"), gw, gh, &c.glass).unwrap();
}
