//! The sample wall on the IT8951 panel, timed: a photograph and the grid
//! put up clean after one INIT and after the deeper clean, one clean
//! step by step, then a small change pushed as the wall would
//! push it, then the whole panel again in GC16 so it is left clean.
//!
//!     cargo run --release --example panel -- [picture.png] [--four]

#[cfg(all(target_os = "linux", feature = "it8951"))]
mod common;

#[cfg(all(target_os = "linux", feature = "it8951"))]
fn main() -> std::io::Result<()> {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::Instant;

    use bitwall_eink::compose::Compositor;
    use bitwall_eink::geom::{Orient, Rect};
    use bitwall_eink::grey::{bw, Tones};
    use bitwall_eink::panel::it8951::{Depth, It8951};
    use bitwall_eink::tune::{ALIGN, FONT};
    use bitwall_eink::policy::Clean;
    use bitwall_eink::{Dither, Layer, Mode, Panel, Picture, Scene, Show};
    use common::{grid, picture};

    let args: Vec<String> = std::env::args().skip(1).collect();
    let pic = args.iter().find(|a| a.ends_with(".png")).map(PathBuf::from);
    let depth = if args.iter().any(|a| a == "--four") { Depth::Four } else { Depth::Eight };
    let ms = |t: Instant| t.elapsed().as_secs_f64() * 1e3;

    let t = Instant::now();
    let mut p = It8951::open(None, depth)?;
    let vcom = p.vcom()?;
    println!("panel {}x{} firmware {:?} lut {:?} vcom -{:.2} V, opened in {:.0} ms", p.w, p.h, p.firmware, p.lut, vcom as f64 / 1000.0, ms(t));
    let (pw, ph) = (p.w, p.h);
    let o = Orient::new(0, false, pw, ph);
    let mut c = Compositor::new(pw, ph, Tones { paper: true }, Dither::Diffuse);
    let (cols, rows) = c.face.area(FONT, pw, ph);
    let m = c.face.metrics(FONT);
    let (px, iw, ih) = picture(pic.as_ref());
    let bx = m.cw * 96;
    let bw_ = pw - bx - m.cw * 2;
    let pic = Picture { rgba: Arc::new(px), w: iw, h: ih, at: (bx as i32, (m.ch * 3) as i32), size: (bw_, bw_ * ih / iw) };
    let scene = |tick| Scene { base: Some(grid(cols, rows, FONT, tick)), layers: vec![Layer { id: 1, show: Show::Picture(pic.clone()) }] };

    let t = Instant::now();
    c.update(&scene(1));
    println!("composed {cols}x{rows} and the picture in {:.0} ms", ms(t));

    // Two pictures, the grid and the photograph over the whole panel,
    // each put up clean: first after one INIT, then after the deeper
    // clean, so the ghosts each leaves in the grid's white can be compared.
    let all = Rect::new(0, 0, pw, ph);
    let grid_px = o.read(&c.glass, &all);
    let mut photo = Compositor::new(pw, ph, Tones { paper: true }, Dither::Diffuse);
    let full = Picture { rgba: pic.rgba.clone(), w: iw, h: ih, at: (0, 0), size: (pw, ph) };
    photo.update(&Scene { base: None, layers: vec![Layer { id: 1, show: Show::Picture(full) }] });
    let photo_px = o.read(&photo.glass, &all);
    let hold: u64 = std::env::var("HOLD").ok().and_then(|v| v.parse().ok()).unwrap_or(20);
    for (name, steps) in [("INIT once", &[Clean::Init][..]), ("deep (INIT, black, white)", bitwall_eink::tune::CLEAN)] {
        for (what, px) in [("photograph", &photo_px), ("grid", &grid_px)] {
            let t = Instant::now();
            bitwall_eink::panel::clean(&mut p, steps, px)?;
            p.wait()?;
            println!("clean {name}, then the {what}: {:.0} ms", ms(t));
        }
        println!("holding {hold} s: the grid after {name}");
        std::thread::sleep(std::time::Duration::from_secs(hold));
    }
    // One clean step by step, timed: INIT, a whole-panel load, GC16.
    let t = Instant::now();
    p.clear()?;
    p.wait()?;
    let clear = ms(t);
    let t = Instant::now();
    p.load(&all, &grid_px)?;
    let load = ms(t);
    let t = Instant::now();
    p.display(&all, Mode::Gc16)?;
    p.wait()?;
    println!("INIT {clear:.0} ms, load {:.1} MB {load:.0} ms, GC16 {:.0} ms", grid_px.len() as f64 / 1e6 / if depth == Depth::Four { 2.0 } else { 1.0 }, ms(t));
    c.shown_all();

    // A small change: the counter, whose blocks are black and white.
    for tick in [2, 3] {
        let t = Instant::now();
        c.update(&scene(tick));
        let boxes = c.boxes();
        let found = ms(t);
        for g in boxes {
            let r = o.to_panel(&g).align_x(ALIGN, pw);
            let px = o.read(&c.glass, &r);
            let mode = if px.iter().all(|&v| bw(v)) { Mode::Du } else { Mode::Gc16 };
            let t = Instant::now();
            p.load(&r, &px)?;
            let load = ms(t);
            let t = Instant::now();
            p.display(&r, mode)?;
            p.wait()?;
            println!("tick {tick}: box {}x{} at {},{} {mode:?}: load {load:.0} ms, refresh {:.0} ms (found in {found:.1} ms)", r.w, r.h, r.x, r.y, ms(t));
            c.shown_now(&o.to_glass(&r));
        }
    }

    // Left clean: the whole panel in GC16.
    let px = o.read(&c.glass, &all);
    let t = Instant::now();
    p.push(&all, &px, Mode::Gc16)?;
    p.wait()?;
    println!("final GC16 of the whole panel: {:.0} ms", ms(t));
    p.sleep()
}

#[cfg(not(all(target_os = "linux", feature = "it8951")))]
fn main() {
    eprintln!("the IT8951 panel is on Linux, with the it8951 feature");
}
