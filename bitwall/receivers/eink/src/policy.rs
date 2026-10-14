//! When to push, and how each box is refreshed. Pure logic: the time is
//! handed in, so every rule can be tested without a panel or a clock.
//!
//! - Changes are gathered and pushed once the senders have been quiet for
//!   QUIET, or MOST after the first, whichever is sooner.
//! - A box all black and white is refreshed fast (DU); one with any grey
//!   in it is refreshed in sixteen greys (GC16).
//! - Each AREA of the panel counts its fast refreshes, and after FAST_LIMIT
//!   the next refresh there is GC16, which clears the ghosts.
//! - A full repaint (a sender attaching, a new font or glass, the wall
//!   starting) is pushed clean: the steps of `tune::CLEAN` over the whole
//!   panel, then the picture in GC16. So is
//!   a push CLEAN_EVERY after the last clean one; nothing is pushed when
//!   nothing has changed.
//! - While nobody is watching nothing is pushed and the picture stays; on
//!   their return what is waiting goes at once, all of it in GC16.

use std::time::Instant;

use crate::geom::Rect;
use crate::tune::{AREA, CLEAN_EVERY, FAST_LIMIT, MOST, QUIET};

/// How a box is refreshed. The numbers are the IT8951's waveform modes,
/// which the panel's firmware decides; the driver maps them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Clear to white through every level: slow, and leaves no ghost.
    Init,
    /// Black and white, fast.
    Du,
    /// Sixteen greys.
    Gc16,
}

/// A step of a clean refresh, over the whole panel.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Clean {
    /// INIT: to white through every level.
    Init,
    /// All black, in GC16.
    Black,
    /// All white, in GC16.
    White,
}

/// What one push does: a clean refresh of the whole panel, or each box
/// in its mode.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Plan {
    Clean,
    Boxes(Vec<(Rect, Mode)>),
}

pub struct Policy {
    first: Option<Instant>,
    last: Option<Instant>,
    clean_asked: bool,
    last_clean: Option<Instant>,
    watching: bool,
    returned: bool,
    aw: u32,
    ah: u32,
    fast: Vec<u32>,
}

impl Policy {
    /// For a panel `pw` by `ph`, which starts asking for a clean push.
    pub fn new(pw: u32, ph: u32) -> Policy {
        let (aw, ah) = (pw.div_ceil(AREA), ph.div_ceil(AREA));
        Policy {
            first: None,
            last: None,
            clean_asked: true,
            last_clean: None,
            watching: true,
            returned: false,
            aw,
            ah,
            fast: vec![0; (aw * ah) as usize],
        }
    }

    /// Something changed on the glass.
    pub fn changed(&mut self, now: Instant) {
        self.first.get_or_insert(now);
        self.last = Some(now);
    }

    /// The next push is a clean one: a full repaint.
    pub fn clean(&mut self, now: Instant) {
        self.clean_asked = true;
        self.changed(now);
    }

    /// Whether anybody is watching. Coming back sends what waits at once.
    pub fn watching(&mut self, on: bool) {
        if on && !self.watching {
            self.returned = true;
        }
        self.watching = on;
    }

    pub fn is_watching(&self) -> bool {
        self.watching
    }

    /// When a push is due, or None while there is nothing to push or
    /// nobody to see it.
    pub fn due(&self) -> Option<Instant> {
        let (first, last) = (self.first?, self.last?);
        if !self.watching {
            return None;
        }
        if self.returned {
            return Some(last.min(first));
        }
        Some((last + QUIET).min(first + MOST))
    }

    /// Whether a push is due at `now`.
    pub fn ready(&self, now: Instant) -> bool {
        self.due().is_some_and(|d| now >= d)
    }

    fn areas(&self, r: &Rect) -> impl Iterator<Item = usize> + '_ {
        let (x0, y0) = (r.x / AREA, r.y / AREA);
        let (x1, y1) = ((r.right().max(1) - 1) / AREA, (r.bottom().max(1) - 1) / AREA);
        (y0..=y1.min(self.ah - 1)).flat_map(move |y| (x0..=x1.min(self.aw - 1)).map(move |x| (y * self.aw + x) as usize))
    }

    /// The plan for a push at `now` of `boxes` (panel rectangles), each
    /// said to be all black and white or not; and the push is taken as
    /// done.
    pub fn plan(&mut self, now: Instant, boxes: &[(Rect, bool)]) -> Plan {
        let clean = self.clean_asked || self.last_clean.is_none_or(|t| now.duration_since(t) >= CLEAN_EVERY);
        let all_grey = self.returned;
        self.first = None;
        self.last = None;
        self.returned = false;
        if clean {
            self.clean_asked = false;
            self.last_clean = Some(now);
            self.fast.iter_mut().for_each(|f| *f = 0);
            return Plan::Clean;
        }
        let mut out = Vec::with_capacity(boxes.len());
        for (r, bw) in boxes {
            let worn = self.areas(r).any(|a| self.fast[a] >= FAST_LIMIT);
            let mode = if *bw && !worn && !all_grey { Mode::Du } else { Mode::Gc16 };
            let areas: Vec<usize> = self.areas(r).collect();
            for a in areas {
                if mode == Mode::Du {
                    self.fast[a] += 1;
                } else {
                    self.fast[a] = 0;
                }
            }
            out.push((*r, mode));
        }
        Plan::Boxes(out)
    }

    /// Fast refreshes counted in the area holding panel pixel (x, y).
    pub fn fast_at(&self, x: u32, y: u32) -> u32 {
        self.fast[((y / AREA) * self.aw + x / AREA) as usize]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn ms(t: Instant, n: u64) -> Instant {
        t + Duration::from_millis(n)
    }

    fn started(t: Instant) -> Policy {
        let mut p = Policy::new(1872, 1404);
        p.changed(t);
        assert_eq!(p.plan(t, &[]), Plan::Clean, "the first push is clean");
        p
    }

    #[test]
    fn waits_for_quiet() {
        let t = Instant::now();
        let mut p = started(t);
        assert_eq!(p.due(), None, "nothing waits");
        p.changed(ms(t, 1000));
        p.changed(ms(t, 1200));
        assert_eq!(p.due(), Some(ms(t, 1700)));
        assert!(!p.ready(ms(t, 1600)));
        assert!(p.ready(ms(t, 1700)));
    }

    #[test]
    fn a_sender_that_never_stops_still_shows_every_two_seconds() {
        let t = Instant::now();
        let mut p = started(t);
        for i in 0..30 {
            p.changed(ms(t, 1000 + i * 100));
        }
        assert_eq!(p.due(), Some(ms(t, 3000)));
    }

    #[test]
    fn black_and_white_is_fast_and_grey_is_not() {
        let t = Instant::now();
        let mut p = started(t);
        p.changed(ms(t, 10));
        let a = Rect::new(0, 0, 64, 20);
        let b = Rect::new(800, 600, 64, 20);
        assert_eq!(p.plan(ms(t, 600), &[(a, true), (b, false)]), Plan::Boxes(vec![(a, Mode::Du), (b, Mode::Gc16)]));
        assert_eq!(p.fast_at(0, 0), 1);
    }

    #[test]
    fn worn_areas_get_a_grey_refresh() {
        let t = Instant::now();
        let mut p = started(t);
        let a = Rect::new(10, 10, 40, 20);
        for i in 0..FAST_LIMIT as u64 {
            p.changed(ms(t, 100 + i));
            assert_eq!(p.plan(ms(t, 1000 + i), &[(a, true)]), Plan::Boxes(vec![(a, Mode::Du)]));
        }
        assert_eq!(p.plan(ms(t, 2000), &[(a, true)]), Plan::Boxes(vec![(a, Mode::Gc16)]));
        assert_eq!(p.fast_at(10, 10), 0, "and counts again from nothing");
        // A box elsewhere was never worn.
        let b = Rect::new(1000, 1000, 8, 8);
        assert_eq!(p.plan(ms(t, 2100), &[(b, true)]), Plan::Boxes(vec![(b, Mode::Du)]));
    }

    #[test]
    fn clean_when_asked_and_at_most_hourly() {
        let t = Instant::now();
        let mut p = started(t);
        p.clean(ms(t, 100));
        assert_eq!(p.plan(ms(t, 700), &[]), Plan::Clean);
        let a = Rect::new(0, 0, 8, 8);
        let later = t + CLEAN_EVERY;
        // An hour on with nothing changed: nothing due, so nothing pushed.
        assert_eq!(p.due(), None);
        p.changed(later + Duration::from_secs(5));
        assert_eq!(p.plan(later + Duration::from_secs(6), &[(a, true)]), Plan::Clean);
        p.changed(later + Duration::from_secs(10));
        assert_eq!(p.plan(later + Duration::from_secs(11), &[(a, true)]), Plan::Boxes(vec![(a, Mode::Du)]));
    }

    #[test]
    fn nobody_watching_holds_and_return_pushes_grey_at_once() {
        let t = Instant::now();
        let mut p = started(t);
        p.watching(false);
        p.changed(ms(t, 100));
        p.changed(ms(t, 5000));
        assert_eq!(p.due(), None);
        assert!(!p.ready(ms(t, 60_000)));
        p.watching(true);
        assert!(p.ready(ms(t, 60_001)));
        let a = Rect::new(0, 0, 8, 8);
        assert_eq!(p.plan(ms(t, 60_001), &[(a, true)]), Plan::Boxes(vec![(a, Mode::Gc16)]));
        // And after that, fast again.
        p.changed(ms(t, 61_000));
        assert!(!p.ready(ms(t, 61_100)));
        assert_eq!(p.plan(ms(t, 61_600), &[(a, true)]), Plan::Boxes(vec![(a, Mode::Du)]));
    }
}
