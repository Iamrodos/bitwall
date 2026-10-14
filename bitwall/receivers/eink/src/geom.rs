//! Rectangles, and the way the glass senders compose for is turned onto
//! the panel. The glass is the panel turned by `rotate` degrees clockwise
//! and then mirrored left to right where asked: a panel hung on its side
//! is a glass taller than wide, and senders compose for that shape.

/// A rectangle of pixels, `x` and `y` its top left.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default)]
pub struct Rect {
    pub x: u32,
    pub y: u32,
    pub w: u32,
    pub h: u32,
}

impl Rect {
    pub const fn new(x: u32, y: u32, w: u32, h: u32) -> Rect {
        Rect { x, y, w, h }
    }

    pub fn right(&self) -> u32 {
        self.x + self.w
    }

    pub fn bottom(&self) -> u32 {
        self.y + self.h
    }

    pub fn area(&self) -> u64 {
        self.w as u64 * self.h as u64
    }

    pub fn is_empty(&self) -> bool {
        self.w == 0 || self.h == 0
    }

    /// The smallest rectangle holding both.
    pub fn union(&self, o: &Rect) -> Rect {
        if self.is_empty() {
            return *o;
        }
        if o.is_empty() {
            return *self;
        }
        let x = self.x.min(o.x);
        let y = self.y.min(o.y);
        Rect { x, y, w: self.right().max(o.right()) - x, h: self.bottom().max(o.bottom()) - y }
    }

    pub fn intersect(&self, o: &Rect) -> Rect {
        let x = self.x.max(o.x);
        let y = self.y.max(o.y);
        let r = self.right().min(o.right());
        let b = self.bottom().min(o.bottom());
        if r <= x || b <= y {
            return Rect::default();
        }
        Rect { x, y, w: r - x, h: b - y }
    }

    /// The gap between the two, the larger of across and down; 0 where
    /// they touch or overlap.
    pub fn gap(&self, o: &Rect) -> u32 {
        let gx = if o.x >= self.right() { o.x - self.right() } else { self.x.saturating_sub(o.right()) };
        let gy = if o.y >= self.bottom() { o.y - self.bottom() } else { self.y.saturating_sub(o.bottom()) };
        gx.max(gy)
    }

    /// Widened outward so its left edge and width are multiples of `n`,
    /// within `width`.
    pub fn align_x(&self, n: u32, width: u32) -> Rect {
        let x0 = self.x / n * n;
        let x1 = (self.right().div_ceil(n) * n).min(width);
        Rect { x: x0, y: self.y, w: x1 - x0, h: self.h }
    }
}

/// A rectangle given by signed corners, cut to a glass `w` by `h`.
pub fn clip(x: i64, y: i64, w: i64, h: i64, gw: u32, gh: u32) -> Rect {
    let x0 = x.clamp(0, gw as i64);
    let y0 = y.clamp(0, gh as i64);
    let x1 = (x + w).clamp(0, gw as i64);
    let y1 = (y + h).clamp(0, gh as i64);
    Rect { x: x0 as u32, y: y0 as u32, w: (x1 - x0).max(0) as u32, h: (y1 - y0).max(0) as u32 }
}

/// Rectangles merged until no two are within `near` of each other and
/// there are at most `most`, each merge the one adding the least area.
pub fn merge(mut rs: Vec<Rect>, near: u32, most: usize) -> Vec<Rect> {
    rs.retain(|r| !r.is_empty());
    loop {
        let mut best: Option<(usize, usize, u64)> = None;
        let mut close: Option<(usize, usize)> = None;
        'scan: for i in 0..rs.len() {
            for j in i + 1..rs.len() {
                if rs[i].gap(&rs[j]) <= near {
                    close = Some((i, j));
                    break 'scan;
                }
                if rs.len() > most {
                    let u = rs[i].union(&rs[j]);
                    let cost = u.area() - rs[i].area() - rs[j].area() + rs[i].intersect(&rs[j]).area();
                    if best.is_none_or(|b| cost < b.2) {
                        best = Some((i, j, cost));
                    }
                }
            }
        }
        let (i, j) = match (close, best) {
            (Some(p), _) => p,
            (None, Some((i, j, _))) => (i, j),
            _ => return rs,
        };
        let u = rs[i].union(&rs[j]);
        rs[i] = u;
        rs.swap_remove(j);
    }
}

/// How the glass lies on the panel.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Orient {
    /// Degrees clockwise: 0, 90, 180 or 270.
    pub rotate: u32,
    /// Mirrored left to right after turning.
    pub mirror: bool,
    /// The panel's own size, as it scans.
    pub pw: u32,
    pub ph: u32,
}

impl Orient {
    pub fn new(rotate: u32, mirror: bool, pw: u32, ph: u32) -> Orient {
        assert!(matches!(rotate, 0 | 90 | 180 | 270), "rotate is 0, 90, 180 or 270");
        Orient { rotate, mirror, pw, ph }
    }

    /// The glass senders compose for.
    pub fn glass(&self) -> (u32, u32) {
        if self.rotate % 180 == 0 { (self.pw, self.ph) } else { (self.ph, self.pw) }
    }

    /// The glass pixel shown at panel pixel (x, y).
    #[inline]
    pub fn glass_of(&self, x: u32, y: u32) -> (u32, u32) {
        let (gw, gh) = self.glass();
        let x = if self.mirror { self.pw - 1 - x } else { x };
        match self.rotate {
            0 => (x, y),
            90 => (y, gh - 1 - x),
            180 => (gw - 1 - x, gh - 1 - y),
            _ => (gw - 1 - y, x),
        }
    }

    /// The panel pixel showing glass pixel (x, y).
    #[inline]
    pub fn panel_of(&self, x: u32, y: u32) -> (u32, u32) {
        let (gw, gh) = self.glass();
        let (px, py) = match self.rotate {
            0 => (x, y),
            90 => (gh - 1 - y, x),
            180 => (gw - 1 - x, gh - 1 - y),
            _ => (y, gw - 1 - x),
        };
        (if self.mirror { self.pw - 1 - px } else { px }, py)
    }

    fn corners(r: &Rect, f: impl Fn(u32, u32) -> (u32, u32)) -> Rect {
        let (a, b) = f(r.x, r.y);
        let (c, d) = f(r.right() - 1, r.bottom() - 1);
        Rect { x: a.min(c), y: b.min(d), w: a.abs_diff(c) + 1, h: b.abs_diff(d) + 1 }
    }

    /// A glass rectangle on the panel.
    pub fn to_panel(&self, r: &Rect) -> Rect {
        if r.is_empty() {
            return *r;
        }
        Self::corners(r, |x, y| self.panel_of(x, y))
    }

    /// A panel rectangle on the glass.
    pub fn to_glass(&self, r: &Rect) -> Rect {
        if r.is_empty() {
            return *r;
        }
        Self::corners(r, |x, y| self.glass_of(x, y))
    }

    /// The panel's pixels in `r` (a panel rectangle), from a glass of greys.
    pub fn read(&self, glass: &[u8], r: &Rect) -> Vec<u8> {
        let (gw, _) = self.glass();
        let mut out = Vec::with_capacity(r.area() as usize);
        if self.rotate == 0 && !self.mirror {
            for y in r.y..r.bottom() {
                let at = (y * gw + r.x) as usize;
                out.extend_from_slice(&glass[at..at + r.w as usize]);
            }
            return out;
        }
        for y in r.y..r.bottom() {
            for x in r.x..r.right() {
                let (gx, gy) = self.glass_of(x, y);
                out.push(glass[(gy * gw + gx) as usize]);
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn turning_round_and_back() {
        for rotate in [0, 90, 180, 270] {
            for mirror in [false, true] {
                let o = Orient::new(rotate, mirror, 40, 30);
                let (gw, gh) = o.glass();
                for y in 0..gh {
                    for x in 0..gw {
                        let (px, py) = o.panel_of(x, y);
                        assert!(px < 40 && py < 30);
                        assert_eq!(o.glass_of(px, py), (x, y), "{rotate} {mirror}");
                    }
                }
                let r = Rect::new(3, 4, 5, 6);
                assert_eq!(o.to_glass(&o.to_panel(&r)), r);
            }
        }
    }

    #[test]
    fn ninety_is_clockwise() {
        // The glass's top left lands at the panel's top right.
        let o = Orient::new(90, false, 40, 30);
        assert_eq!(o.glass(), (30, 40));
        assert_eq!(o.panel_of(0, 0), (39, 0));
        assert_eq!(o.panel_of(29, 0), (39, 29));
    }

    #[test]
    fn align_widens_outward() {
        assert_eq!(Rect::new(13, 2, 5, 1).align_x(8, 1872), Rect::new(8, 2, 16, 1));
        assert_eq!(Rect::new(1866, 0, 6, 1).align_x(8, 1872), Rect::new(1864, 0, 8, 1));
    }

    #[test]
    fn merging_joins_near_and_caps_the_count() {
        let rs = vec![Rect::new(0, 0, 10, 10), Rect::new(12, 0, 10, 10), Rect::new(500, 500, 10, 10)];
        let m = merge(rs.clone(), 4, 6);
        assert_eq!(m.len(), 2);
        assert!(m.contains(&Rect::new(0, 0, 22, 10)));
        let m = merge(rs, 0, 1);
        assert_eq!(m, vec![Rect::new(0, 0, 510, 510)]);
    }
}
