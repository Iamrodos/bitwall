//! The e-ink wall: what the receiver core calls. The core hands over the
//! scene whenever a frame closes and says when a full repaint happened and
//! whether anybody is watching; the wall composes at once, and pushes to
//! the panel when its policy says, from `tick`.

use std::io;
use std::time::Instant;

use crate::compose::Compositor;
use crate::geom::{Orient, Rect};
use crate::grey::{bw, Dither, Tones};
use crate::panel::Panel;
use crate::policy::{Mode, Plan, Policy};
use crate::scene::Scene;
use crate::tune::ALIGN;

/// How the wall is set up when it starts.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// Cells turned over in brightness: black grounds are white paper.
    pub paper: bool,
    /// Degrees clockwise the glass is turned on the panel.
    pub rotate: u32,
    /// Mirrored left to right after turning.
    pub mirror: bool,
    pub dither: Dither,
    /// The steps of a clean refresh before the picture.
    pub clean: &'static [crate::policy::Clean],
}

impl Default for Options {
    fn default() -> Options {
        Options { paper: true, rotate: 0, mirror: false, dither: Dither::Diffuse, clean: crate::tune::CLEAN }
    }
}

/// What a push did, for the log and the measures.
#[derive(Clone, Debug, Default)]
pub struct Pushed {
    pub clean: bool,
    pub boxes: Vec<(Rect, Mode)>,
    pub secs: f64,
}

pub struct Eink<P: Panel> {
    pub comp: Compositor,
    pub policy: Policy,
    pub orient: Orient,
    pub panel: P,
    font: Option<u32>,
    started: bool,
    clean: &'static [crate::policy::Clean],
}

impl<P: Panel> Eink<P> {
    pub fn new(panel: P, o: Options) -> Eink<P> {
        let (pw, ph) = panel.size();
        let orient = Orient::new(o.rotate, o.mirror, pw, ph);
        let (gw, gh) = orient.glass();
        Eink { comp: Compositor::new(gw, gh, Tones { paper: o.paper }, o.dither), policy: Policy::new(pw, ph), orient, panel, font: None, started: false, clean: o.clean }
    }

    /// The glass senders compose for, in pixels: `screen pixels=`.
    pub fn glass(&self) -> (u32, u32) {
        self.orient.glass()
    }

    /// One cell at a font, in pixels: `screen cell=`.
    pub fn cell(&self, font: u32) -> (u32, u32) {
        let m = self.comp.face.metrics(font);
        (m.cw, m.ch)
    }

    /// The grid a font gives on this glass: `area`.
    pub fn area(&self, font: u32) -> (usize, usize) {
        let (w, h) = self.glass();
        self.comp.face.area(font, w, h)
    }

    /// The largest font whose grid holds `cols` by `rows`: `grid`.
    pub fn font_for(&self, cols: usize, rows: usize) -> Option<u32> {
        let (w, h) = self.glass();
        self.comp.face.font_for(cols, rows, w, h)
    }

    /// Whether the face draws `cp`, or it is drawn as a shape: `glyphs=`.
    pub fn draws(&self, cp: u32) -> bool {
        crate::shapes::drawn(cp) || self.comp.face.has(cp)
    }

    /// The wall holds a new scene. The first, and a change of the base's
    /// font, is a full repaint, and pushed clean.
    pub fn show(&mut self, scene: &Scene, now: Instant) {
        if !self.started {
            self.started = true;
            self.policy.clean(now);
        }
        let font = scene.base.as_ref().map(|b| b.font);
        if self.font.is_some() && font.is_some() && font != self.font {
            self.policy.clean(now);
        }
        if font.is_some() {
            self.font = font;
        }
        if self.comp.update(scene) {
            self.policy.changed(now);
        }
    }

    /// A full repaint happened -- a sender attached or came back, the
    /// glass changed -- so the next push is clean.
    pub fn repaint(&mut self, now: Instant) {
        self.policy.clean(now);
    }

    /// The panel started again after pushes failed, and the next push a
    /// clean one, since what it shows is no longer known.
    pub fn recover(&mut self, now: Instant) -> io::Result<()> {
        self.panel.reset()?;
        self.policy.clean(now);
        Ok(())
    }

    /// Whether anybody is watching. While nobody is, nothing is pushed.
    pub fn watching(&mut self, on: bool) {
        self.policy.watching(on);
    }

    /// When `tick` next has something to do, or None until something
    /// changes.
    pub fn next(&self) -> Option<Instant> {
        self.policy.due()
    }

    /// Push if a push is due at `now`.
    pub fn tick(&mut self, now: Instant) -> io::Result<Option<Pushed>> {
        if !self.policy.ready(now) {
            return Ok(None);
        }
        let t = Instant::now();
        let (pw, ph) = self.panel.size();
        // Each box of glass on the panel, widened to the panel's packing,
        // and whether the fast mode may draw it: all black and white, and
        // only adding ink. The fast mode leaves a ghost of ink it takes
        // away, so a box where any black turns white is drawn in grey.
        let boxes: Vec<(Rect, bool)> = self
            .comp
            .boxes()
            .iter()
            .map(|g| {
                let r = self.orient.to_panel(g).align_x(ALIGN, pw);
                let px = self.orient.read(&self.comp.glass, &r);
                let was = self.orient.read(&self.comp.shown, &r);
                let adds = px.iter().zip(was.iter()).all(|(&now, &then)| !(then < 128 && now >= 128));
                (r, adds && px.iter().all(|&v| bw(v)))
            })
            .collect();
        let plan = self.policy.plan(now, &boxes);
        let mut out = Pushed::default();
        match plan {
            Plan::Clean => {
                let all = Rect::new(0, 0, pw, ph);
                let px = self.orient.read(&self.comp.glass, &all);
                crate::panel::clean(&mut self.panel, self.clean, &px)?;
                self.comp.shown_all();
                out.clean = true;
                out.boxes.push((all, Mode::Gc16));
            }
            Plan::Boxes(bs) => {
                for (r, mode) in bs {
                    let px = self.orient.read(&self.comp.glass, &r);
                    self.panel.push(&r, &px, mode)?;
                    self.comp.shown_now(&self.orient.to_glass(&r));
                    out.boxes.push((r, mode));
                }
            }
        }
        self.panel.sleep()?;
        out.secs = t.elapsed().as_secs_f64();
        Ok(Some(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::panel::FilePanel;
    use crate::scene::{Cell, Cells, DEFAULT};
    use std::time::Duration;

    fn wall(dir: &str, o: Options) -> (Eink<FilePanel>, std::path::PathBuf) {
        let d = std::env::temp_dir().join(format!("eink-wall-{dir}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        (Eink::new(FilePanel::new(&d, 400, 240).unwrap(), o), d)
    }

    fn log(d: &std::path::Path) -> Vec<String> {
        std::fs::read_to_string(d.join("log")).unwrap().lines().map(|l| l.split_once(' ').unwrap().1.to_string()).collect()
    }

    #[test]
    fn start_is_clean_then_changes_are_boxes() {
        let (mut w, d) = wall("start", Options::default());
        let t = Instant::now();
        let (cols, rows) = w.area(17);
        let mut b = Cells::new(17, cols, rows, Cell::BLANK);
        b.text(1, 1, "hello", DEFAULT, DEFAULT, 0);
        w.show(&Scene { base: Some(b.clone()), layers: vec![] }, t);
        assert!(w.tick(t + Duration::from_millis(100)).unwrap().is_none(), "waits for quiet");
        let p = w.tick(t + Duration::from_millis(500)).unwrap().unwrap();
        assert!(p.clean);
        // A block: all black and white, so fast.
        b.text(10, 3, "\u{2588}\u{2588}", DEFAULT, DEFAULT, 0);
        let t2 = t + Duration::from_secs(1);
        w.show(&Scene { base: Some(b.clone()), layers: vec![] }, t2);
        let p = w.tick(t2 + Duration::from_millis(500)).unwrap().unwrap();
        assert_eq!(p.boxes.len(), 1);
        assert_eq!(p.boxes[0].1, Mode::Du);
        assert_eq!(p.boxes[0].0.x % ALIGN, 0);
        assert_eq!(p.boxes[0].0.w % ALIGN, 0);
        // The block taken away: black turning white, which the fast mode
        // would leave a ghost of, so GC16 although it is black and white.
        b.text(10, 3, "  ", DEFAULT, DEFAULT, 0);
        let t25 = t + Duration::from_millis(1500);
        w.show(&Scene { base: Some(b.clone()), layers: vec![] }, t25);
        let p = w.tick(t25 + Duration::from_millis(500)).unwrap().unwrap();
        assert_eq!(p.boxes[0].1, Mode::Gc16, "ink taken away is not fast");
        // Text has grey edges, so GC16.
        b.text(1, 5, "grey", DEFAULT, DEFAULT, 0);
        let t3 = t + Duration::from_secs(2);
        w.show(&Scene { base: Some(b.clone()), layers: vec![] }, t3);
        let p = w.tick(t3 + Duration::from_millis(500)).unwrap().unwrap();
        assert_eq!(p.boxes[0].1, Mode::Gc16);
        // Nothing new: nothing due.
        w.show(&Scene { base: Some(b), layers: vec![] }, t3 + Duration::from_secs(1));
        assert!(w.next().is_none());
        let l = log(&d);
        // INIT, black, white, the picture; then the three boxes.
        assert_eq!(l[0], "1 box 0,0 400x240 mode INIT");
        assert_eq!(l[1], "2 box 0,0 400x240 mode GC16");
        assert_eq!(l[3], "4 box 0,0 400x240 mode GC16");
        assert!(l[4].ends_with("mode DU"));
        assert!(l[5].ends_with("mode GC16"));
        assert!(l[6].ends_with("mode GC16"));
        assert_eq!(l.len(), 7);
        // The panel shows exactly the glass.
        assert_eq!(w.panel.image(), &w.comp.glass[..]);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn turned_and_mirrored_the_panel_still_shows_the_glass() {
        for (rotate, mirror) in [(90, false), (180, true), (270, true)] {
            let (mut w, d) = wall(&format!("turn{rotate}{mirror}"), Options { rotate, mirror, ..Options::default() });
            assert_eq!(w.glass(), if rotate == 180 { (400, 240) } else { (240, 400) });
            let t = Instant::now();
            let (cols, rows) = w.area(17);
            let mut b = Cells::new(17, cols, rows, Cell::BLANK);
            w.show(&Scene { base: Some(b.clone()), layers: vec![] }, t);
            w.tick(t + Duration::from_secs(1)).unwrap().unwrap();
            b.text(2, 2, "\u{2588}x", DEFAULT, DEFAULT, 0);
            w.show(&Scene { base: Some(b), layers: vec![] }, t + Duration::from_secs(2));
            w.tick(t + Duration::from_secs(3)).unwrap().unwrap();
            let want = w.orient.read(&w.comp.glass, &Rect::new(0, 0, 400, 240));
            assert_eq!(w.panel.image(), &want[..], "{rotate} {mirror}");
            std::fs::remove_dir_all(d).unwrap();
        }
    }

    #[test]
    fn the_nerd_font_icons_are_drawn() {
        let (mut w, d) = wall("icons", Options::default());
        for cp in [0xE0B0, 0xE30D, 0xEA60, 0xF05A, 0xF075, 0xF129, 0xF400, 0xF0369, 0xF1AF0] {
            assert!(w.draws(cp), "U+{cp:04X} is not drawn");
            let m = w.comp.face.metrics(17);
            let g = w.comp.face.glyph(cp, &m, 1, 0);
            assert!(g.cov.iter().any(|&v| v > 128), "U+{cp:04X} has no ink");
        }
        std::fs::remove_dir_all(d).ok();
    }

    #[test]
    fn a_new_font_is_pushed_clean() {
        let (mut w, d) = wall("font", Options::default());
        let t = Instant::now();
        let (c, r) = w.area(17);
        w.show(&Scene { base: Some(Cells::new(17, c, r, Cell::BLANK)), layers: vec![] }, t);
        assert!(w.tick(t + Duration::from_secs(1)).unwrap().unwrap().clean);
        let (c, r) = w.area(24);
        let mut b = Cells::new(24, c, r, Cell::BLANK);
        b.text(0, 0, "big", DEFAULT, DEFAULT, 0);
        w.show(&Scene { base: Some(b), layers: vec![] }, t + Duration::from_secs(2));
        assert!(w.tick(t + Duration::from_secs(3)).unwrap().unwrap().clean);
        std::fs::remove_dir_all(d).unwrap();
    }
}
