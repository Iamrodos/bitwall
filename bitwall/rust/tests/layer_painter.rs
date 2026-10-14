//! Video layers held to a real painter: one started here, with no screen,
//! in a run directory of its own, so no wall anybody is using is touched.
//! Skipped where the painter has not been built (`make PLATFORM=mac` in
//! `bitwall/receivers/pi`), or named by `BITWALL_PAINTER`; the JPEG and
//! the film are made with ffmpeg, and skipped without it.

use bitwall::layer::{Hold, Layer, Place};
use bitwall::qoi;
use bitwall::session::{wall_run, Controller, Link, Target};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const PAINTER: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../receivers/pi/bitwall-paint");

struct Painter {
    child: Child,
    run: PathBuf,
}

impl Drop for Painter {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.run);
    }
}

impl Painter {
    fn wall(&self) -> String {
        wall_run(self.run.to_str().unwrap(), "0")
    }

    fn controller(&self) -> Controller {
        let link = Link::new(&Target::parse("localhost").unwrap(), None, Some(&self.wall()));
        Controller::open_on(&link, None, None).expect("a controller")
    }
}

fn painter(tag: &str) -> Option<Painter> {
    let path = std::env::var("BITWALL_PAINTER").unwrap_or_else(|_| PAINTER.into());
    if !Path::new(&path).exists() {
        eprintln!("no painter at {path}: skipped");
        return None;
    }
    // Short: the painter's sockets live in it, and a socket's path is
    // at most 104 bytes on a Mac.
    let run = PathBuf::from(format!("/tmp/bwrs-{tag}-{}", std::process::id()));
    std::fs::create_dir_all(&run).unwrap();
    let child = Command::new(&path)
        .args(["--glass", "640x360", "--run"])
        .arg(&run)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let p = Painter { child, run };
    let door = p.run.join("bitwall/0/door.in");
    let until = Instant::now() + Duration::from_secs(10);
    while !door.exists() {
        assert!(Instant::now() < until, "the painter made no door");
        std::thread::sleep(Duration::from_millis(50));
    }
    Some(p)
}

fn ffmpeg(args: &[&str], out: &Path) -> Option<Vec<u8>> {
    let ok = Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-y"])
        .args(args)
        .arg(out)
        .stdin(Stdio::null())
        .status()
        .ok()?
        .success();
    if !ok {
        return None;
    }
    std::fs::read(out).ok()
}

/// A picture of `w` by `h`, a disc of `rgb` on a clear ground.
fn disc(w: u32, h: u32, rgb: [u8; 3]) -> Vec<u8> {
    let mut px = vec![0u8; (w * h * 4) as usize];
    let (cx, cy, r) = (w as i64 / 2, h as i64 / 2, (w.min(h) / 2) as i64);
    for y in 0..h as i64 {
        for x in 0..w as i64 {
            if (x - cx).pow(2) + (y - cy).pow(2) < r * r {
                let i = ((y * w as i64 + x) * 4) as usize;
                px[i..i + 4].copy_from_slice(&[rgb[0], rgb[1], rgb[2], 255]);
            }
        }
    }
    qoi::encode(&px, w, h)
}

/// The value of `key=` on the line `sessions` gives for session `name`.
fn listed(lines: &[String], name: &str) -> Option<String> {
    lines.iter().find(|l| l.split_whitespace().any(|w| w == name)).cloned()
}

/// The measure lines the wall says for `name` over `secs`.
fn measures(c: &mut Controller, name: &str, secs: u64) -> Vec<String> {
    c.send("measures 250").unwrap();
    let mut got = Vec::new();
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        if let Some(line) = c.read(Duration::from_millis(300)) {
            if opt(&line, "session") == Some(name) {
                got.push(line);
            }
        }
    }
    let _ = c.send("measures 0");
    got
}

fn opt<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    line.split_whitespace().find_map(|w| w.strip_prefix(key).and_then(|r| r.strip_prefix('=')))
}

#[test]
fn pictures_on_a_layer() {
    let Some(p) = painter("pic") else { return };
    let layer = Layer::new("localhost", Place::Above).unwrap().run(&p.wall());
    layer.open(Duration::from_secs(15)).unwrap();
    assert!(layer.open_now(), "the layer did not open: {:?}", layer.stopped());
    assert_eq!(layer.z(), Some(1));
    layer.describe("rust layer", "pictures from bitwall-rs");
    let until = Instant::now() + Duration::from_secs(5);
    while layer.said("caps").is_none() && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(layer.takes("qoi"));
    assert_eq!(layer.pixels(), Some((640, 360)));

    let mut c = p.controller();
    let name = c
        .ask("sessions", Duration::from_secs(3))
        .iter()
        .find(|l| l.contains("-l"))
        .and_then(|l| l.split_whitespace().nth(1).map(str::to_string))
        .expect("the layer in sessions");
    let mut sent = 0;
    for i in 0..10u8 {
        let q = disc(96, 96, [200, 40 + i * 20, 40]);
        if layer.frame(&q, (40 + 20 * i as i64, 100), None, Hold::Ms(2000)).unwrap() {
            sent += 1;
        }
        std::thread::sleep(Duration::from_millis(40));
    }
    assert!(sent >= 8, "{sent} of 10 QOI frames went");
    let jpeg = std::env::temp_dir().join(format!("bwrs-{}.jpg", std::process::id()));
    let jpeg_sent = if layer.takes("jpeg") {
        match ffmpeg(&["-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1"], &jpeg) {
            Some(data) => {
                let ok = layer.frame(&data, (0, 0), Some((160, 120)), Hold::Still).unwrap();
                assert!(ok, "the JPEG was dropped");
                true
            }
            None => false,
        }
    } else {
        false
    };
    let _ = std::fs::remove_file(&jpeg);
    let m = measures(&mut c, &name, 2);
    let s = c.ask("sessions", Duration::from_secs(3));
    eprintln!("sessions: {s:?}");
    eprintln!("measures: {:?}", m.iter().take(3).collect::<Vec<_>>());
    let line = listed(&s, &name).expect("the layer listed");
    assert!(line.contains("type=video") || line.contains("video"), "{line}");
    assert!(layer.errors().is_empty(), "the wall said {:?}", layer.errors());
    let (frames, dropped, bytes) = layer.sent();
    eprintln!("sent {frames} frames, dropped {dropped}, {bytes} bytes, jpeg {jpeg_sent}");
    assert_eq!(frames as usize, sent + jpeg_sent as usize);
    assert!(layer.clear());
    layer.close();
    assert!(!layer.open_now());
    std::thread::sleep(Duration::from_millis(300));
    let s = c.ask("sessions", Duration::from_secs(3));
    assert!(listed(&s, &name).is_none(), "still listed after close: {s:?}");
    c.close();
}

#[test]
fn a_film_on_data() {
    let Some(p) = painter("film") else { return };
    let film = std::env::temp_dir().join(format!("bwrs-{}.hevc", std::process::id()));
    let Some(data) = ffmpeg(
        &["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-t", "2", "-c:v", "libx265",
          "-x265-params", "log-level=error", "-pix_fmt", "yuv420p", "-f", "hevc"],
        &film,
    ) else {
        eprintln!("no ffmpeg with libx265: skipped");
        return;
    };
    let _ = std::fs::remove_file(&film);
    let layer = Layer::new("localhost", Place::Above).unwrap().data("hevc").fps(25.0).run(&p.wall());
    let lost = Arc::new(AtomicUsize::new(0));
    let l2 = lost.clone();
    layer.when_lost(move || {
        l2.fetch_add(1, Ordering::SeqCst);
    });
    layer.open(Duration::from_secs(15)).unwrap();
    assert!(layer.open_now(), "the layer did not open: {:?}", layer.stopped());
    assert!(layer.has_data(), "the wall said no data");
    let mut c = p.controller();
    let name = c
        .ask("sessions", Duration::from_secs(3))
        .iter()
        .find(|l| l.contains("-l"))
        .and_then(|l| l.split_whitespace().nth(1).map(str::to_string))
        .expect("the layer in sessions");
    c.send("measures 250").unwrap();
    let mut wrote = 0;
    for chunk in data.chunks(16384) {
        wrote += layer.data_write(chunk, Some(Duration::from_secs(5))).expect("a data write");
    }
    assert_eq!(wrote, data.len());
    // The film plays out on the wall's clock: 2 s, from its first frame.
    let mut shown = Vec::new();
    let end = Instant::now() + Duration::from_millis(2500);
    while Instant::now() < end {
        if let Some(line) = c.read(Duration::from_millis(300)) {
            if opt(&line, "session") == Some(name.as_str()) {
                shown.push(line);
            }
        }
    }
    layer.data_end(Duration::from_secs(5));
    let _ = c.send("measures 0");
    eprintln!("{} bytes of HEVC; measures: {:?}", data.len(), &shown[..shown.len().min(6)]);
    let fps: Vec<f64> = shown.iter().filter_map(|l| opt(l, "fps")).filter_map(|v| v.parse().ok()).collect();
    assert!(fps.iter().any(|&f| f > 10.0), "no frames shown: {shown:?}");
    // The layer stays for the next stream.
    assert!(layer.open_now());
    assert_eq!(lost.load(Ordering::SeqCst), 0);
    layer.close();
    c.close();
}

#[test]
fn a_layer_opens_again_when_the_wall_comes_back() {
    let Some(mut p) = painter("again") else { return };
    let layer = Layer::new("localhost", Place::Above).unwrap().run(&p.wall());
    let lost = Arc::new(AtomicUsize::new(0));
    let l2 = lost.clone();
    layer.when_lost(move || {
        l2.fetch_add(1, Ordering::SeqCst);
    });
    layer.open(Duration::from_secs(15)).unwrap();
    assert!(layer.open_now());
    let _ = p.child.kill();
    let _ = p.child.wait();
    let until = Instant::now() + Duration::from_secs(10);
    while layer.open_now() && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(!layer.open_now(), "still open with the wall gone");
    let path = std::env::var("BITWALL_PAINTER").unwrap_or_else(|_| PAINTER.into());
    let _ = std::fs::remove_dir_all(p.run.join("bitwall"));
    p.child = Command::new(&path)
        .args(["--glass", "640x360", "--run"])
        .arg(&p.run)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let until = Instant::now() + Duration::from_secs(20);
    while !layer.open_now() && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(layer.open_now(), "not open again: {:?}", layer.stopped());
    assert_eq!(lost.load(Ordering::SeqCst), 1);
    assert!(layer.frame(&disc(32, 32, [0, 200, 0]), (0, 0), None, Hold::Ms(500)).unwrap());
    layer.close();
}
