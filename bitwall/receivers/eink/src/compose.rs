//! The glass as greys: the base's cells, then every layer above it in
//! order, cells layers blended by their alpha and pictures over them, and
//! what changed.
//!
//! Cells, text and geometry alike, are drawn anti-aliased and then go
//! through the contrast curve onto a level (`grey::curve`). Pictures --
//! picture layers, and kitty graphics placed in a grid -- are dithered by
//! Floyd-Steinberg onto the levels and go through no curve. On paper a
//! picture layer is never turned over; a kitty picture in a grid is, with
//! the cells it sits among, while `kitty_paper` says so, since wordmarks
//! and tile letters are drawn for a dark wall.
//!
//! A new scene is compared with the last, cell by cell and picture by
//! picture, and only the squares of glass (`TILE` a side) it touched are
//! composed again. What the panel shows is kept as well, so the boxes a
//! push needs are where the two differ: a change undone before a push
//! costs nothing, and a box is drawn tight round what differs.

use std::collections::HashMap;
use std::sync::Arc;

use crate::face::{Face, Mask, Metrics};
use crate::geom::{clip, merge, Rect};
use crate::grey::{curve, dither, lum, nearest, over, paper_levels, Dither, Tones};
use crate::scene::{Cell, Cells, Layer, Picture, Scene, Show, DEFAULT, FAINT, NONE, REVERSE, STRIKE, TAIL, UNDER};
use crate::shapes::{self, BRAILLE};
use crate::tune::{MAX_BOXES, NEAR, TILE};

/// A picture brought to the glass: its visible part, scaled to its box,
/// in greys at the panel's levels with an alpha.
struct Prepared {
    picture: Picture,
    area: Rect,
    grey: Vec<u8>,
    alpha: Vec<u8>,
}

pub struct Compositor {
    pub face: Face,
    shapes: HashMap<(u32, u32), Option<Arc<Mask>>>,
    pub w: u32,
    pub h: u32,
    pub tones: Tones,
    pub dither: Dither,
    /// The glass as composed, at the panel's levels.
    pub glass: Vec<u8>,
    /// The glass as the panel shows it.
    pub shown: Vec<u8>,
    scene: Scene,
    pictures: HashMap<u64, Prepared>,
    /// Kitty pictures in grids, by their frame, box and whether turned over.
    kitty: HashMap<(usize, i32, i32, u32, u32, bool), Prepared>,
    /// Kitty pictures in a grid are turned over on paper with the cells.
    pub kitty_paper: bool,
    tw: u32,
    th: u32,
    /// Squares composed since the last push that may differ from what is shown.
    pending: Vec<bool>,
}

/// A grid's cell and where it starts, centred on the glass by its own cell.
#[derive(Clone, Copy)]
struct Place {
    m: Metrics,
    ox: u32,
    oy: u32,
}

/// Whether the icon at (x, y) of `c` is drawn across two cells: a blank
/// after it that looks as its own cell does, ground, reversal and lines
/// alike, so the icon's second half lands on what its first has.
fn spreads(c: &Cells, x: usize, y: usize) -> bool {
    if x + 1 >= c.cols {
        return false;
    }
    let (a, b) = (c.at(x, y), c.at(x + 1, y));
    let looks = REVERSE | UNDER | STRIKE;
    crate::face::icon(a.ch)
        && b.ch == ' ' as u32
        && b.bg == a.bg
        && b.at & looks == a.at & looks
        && (a.at & (UNDER | STRIKE) == 0 || b.fg == a.fg)
        && (a.at & REVERSE == 0 || b.fg == a.fg)
}

impl Compositor {
    pub fn new(w: u32, h: u32, tones: Tones, dither: Dither) -> Compositor {
        let (tw, th) = (w.div_ceil(TILE), h.div_ceil(TILE));
        let g = tones.ground();
        Compositor {
            face: Face::new(),
            shapes: HashMap::new(),
            w,
            h,
            tones,
            dither,
            glass: vec![g; (w * h) as usize],
            shown: vec![g; (w * h) as usize],
            scene: Scene::default(),
            pictures: HashMap::new(),
            kitty: HashMap::new(),
            kitty_paper: true,
            tw,
            th,
            pending: vec![false; (tw * th) as usize],
        }
    }

    fn place(&self, c: &Cells) -> Place {
        let m = self.face.metrics(c.font);
        let gw = c.cols as u32 * m.cw;
        let gh = c.rows as u32 * m.ch;
        Place { m, ox: self.w.saturating_sub(gw) / 2, oy: self.h.saturating_sub(gh) / 2 }
    }

    /// Glass rectangle of cells x0..x1 on row y.
    fn cells_rect(&self, p: &Place, x0: usize, x1: usize, y: usize) -> Rect {
        clip(
            (p.ox + x0 as u32 * p.m.cw) as i64,
            (p.oy + y as u32 * p.m.ch) as i64,
            ((x1 - x0) as u32 * p.m.cw) as i64,
            p.m.ch as i64,
            self.w,
            self.h,
        )
    }

    fn mark(&self, redo: &mut [bool], r: &Rect) {
        if r.is_empty() {
            return;
        }
        let (x0, y0) = (r.x / TILE, r.y / TILE);
        let (x1, y1) = ((r.right() - 1) / TILE, (r.bottom() - 1) / TILE);
        for ty in y0..=y1.min(self.th - 1) {
            for tx in x0..=x1.min(self.tw - 1) {
                redo[(ty * self.tw + tx) as usize] = true;
            }
        }
    }

    /// Mark the cells of `c` that differ from `was` (both on one grid), or
    /// with no `was` every drawn cell.
    fn mark_cells(&self, redo: &mut [bool], c: &Cells, was: Option<&Cells>) {
        match was {
            Some(w) if w.pictures == c.pictures => {}
            _ => {
                for p in c.pictures.iter().chain(was.map(|w| &w.pictures[..]).unwrap_or(&[])) {
                    self.mark(redo, &self.picture_rect(p));
                }
            }
        }
        let p = self.place(c);
        for y in 0..c.rows {
            let mut x = 0;
            while x < c.cols {
                let i = y * c.cols + x;
                let differs = match was {
                    Some(w) => w.cells[i] != c.cells[i],
                    None => c.cells[i].ch != NONE,
                };
                if !differs {
                    x += 1;
                    continue;
                }
                let start = x;
                while x < c.cols
                    && match was {
                        Some(w) => w.cells[y * c.cols + x] != c.cells[y * c.cols + x],
                        None => c.cells[y * c.cols + x].ch != NONE,
                    }
                {
                    x += 1;
                }
                // A wide character's lead changes the cell after it too, and
                // a tail the one before it.
                let x0 = start.saturating_sub(1);
                let x1 = (x + 1).min(c.cols);
                self.mark(redo, &self.cells_rect(&p, x0, x1, y));
            }
        }
    }

    fn mark_show(&self, redo: &mut [bool], s: &Show) {
        match s {
            Show::Cells(c) => self.mark_cells(redo, c, None),
            Show::Picture(p) => self.mark(redo, &self.picture_rect(p)),
        }
    }

    fn picture_rect(&self, p: &Picture) -> Rect {
        clip(p.at.0 as i64, p.at.1 as i64, p.size.0 as i64, p.size.1 as i64, self.w, self.h)
    }

    /// Take a new scene: compose what it changed. Returns whether any of
    /// the glass changed.
    pub fn update(&mut self, scene: &Scene) -> bool {
        let mut redo = vec![false; self.pending.len()];
        let old = std::mem::take(&mut self.scene);
        match (&old.base, &scene.base) {
            (None, None) => {}
            (Some(a), Some(b)) if a.font == b.font && a.cols == b.cols && a.rows == b.rows => {
                self.mark_cells(&mut redo, b, Some(a))
            }
            _ => redo.iter_mut().for_each(|t| *t = true),
        }
        let ids_old: Vec<u64> = old.layers.iter().map(|l| l.id).collect();
        let ids_new: Vec<u64> = scene.layers.iter().map(|l| l.id).collect();
        for (i, l) in old.layers.iter().enumerate() {
            match scene.layers.iter().position(|n| n.id == l.id) {
                None => self.mark_show(&mut redo, &l.show),
                Some(j) => {
                    let n = &scene.layers[j];
                    // A layer that changed its place among the others shows
                    // differently wherever it is drawn.
                    let moved = ids_old[..i].iter().filter(|id| ids_new.contains(id)).ne(ids_new[..j].iter().filter(|id| ids_old.contains(id)));
                    match (&l.show, &n.show) {
                        _ if moved => {
                            self.mark_show(&mut redo, &l.show);
                            self.mark_show(&mut redo, &n.show);
                        }
                        (Show::Cells(a), Show::Cells(b)) if a.font == b.font && a.cols == b.cols && a.rows == b.rows => {
                            self.mark_cells(&mut redo, b, Some(a))
                        }
                        (a, b) if a == b => {}
                        (a, b) => {
                            self.mark_show(&mut redo, a);
                            self.mark_show(&mut redo, b);
                        }
                    }
                }
            }
        }
        for l in &scene.layers {
            if !ids_old.contains(&l.id) {
                self.mark_show(&mut redo, &l.show);
            }
        }
        self.scene = scene.clone();
        self.pictures.retain(|id, _| ids_new.contains(id));
        self.compose(&redo)
    }

    /// Compose everything again, as after a change of font or glass.
    pub fn recompose(&mut self) -> bool {
        let redo = vec![true; self.pending.len()];
        self.compose(&redo)
    }

    fn compose(&mut self, redo: &[bool]) -> bool {
        let mut used = Vec::new();
        let grids: Vec<Cells> = self.scene.base.iter().cloned().chain(self.scene.layers.iter().filter_map(|l| match &l.show {
            Show::Cells(c) => Some(c.clone()),
            _ => None,
        })).collect();
        let turn = self.tones.paper && self.kitty_paper;
        for c in &grids {
            for p in &c.pictures {
                let k = kitty_key(p, turn);
                if !self.kitty.contains_key(&k) {
                    let prepared = self.prepare_picture(p, turn);
                    self.kitty.insert(k, prepared);
                }
                used.push(k);
            }
        }
        self.kitty.retain(|k, _| used.contains(k));
        for l in &self.scene.layers.clone() {
            if let Show::Picture(p) = &l.show {
                if !self.pictures.get(&l.id).is_some_and(|q| q.picture == *p) {
                    let prepared = self.prepare_picture(p, false);
                    self.pictures.insert(l.id, prepared);
                }
            }
        }
        let mut changed = false;
        for ty in 0..self.th {
            let mut tx = 0;
            while tx < self.tw {
                if !redo[(ty * self.tw + tx) as usize] {
                    tx += 1;
                    continue;
                }
                let start = tx;
                while tx < self.tw && redo[(ty * self.tw + tx) as usize] {
                    tx += 1;
                }
                let r = clip((start * TILE) as i64, (ty * TILE) as i64, ((tx - start) * TILE) as i64, TILE as i64, self.w, self.h);
                let buf = self.region(&r);
                // Which squares of the run came out differently.
                for t in start..tx {
                    let sq = r.intersect(&Rect::new(t * TILE, ty * TILE, TILE, TILE));
                    let mut differs = false;
                    for y in sq.y..sq.bottom() {
                        let a = (y * self.w + sq.x) as usize;
                        let b = ((y - r.y) * r.w + sq.x - r.x) as usize;
                        let (row, new) = (&mut self.glass[a..a + sq.w as usize], &buf[b..b + sq.w as usize]);
                        if row != new {
                            row.copy_from_slice(new);
                            differs = true;
                        }
                    }
                    if differs {
                        self.pending[(ty * self.tw + t) as usize] = true;
                        changed = true;
                    }
                }
            }
        }
        changed
    }

    /// The glass in `r`, composed from nothing.
    fn region(&mut self, r: &Rect) -> Vec<u8> {
        let mut buf = vec![self.tones.ground(); r.area() as usize];
        // Which pixels a picture has the last word on: those are not curved.
        let mut pic = vec![false; buf.len()];
        let scene = std::mem::take(&mut self.scene);
        let turn = self.tones.paper && self.kitty_paper;
        let grid = |me: &mut Compositor, buf: &mut [u8], pic: &mut [bool], c: &Cells, layer: bool| {
            me.draw_cells(buf, pic, r, c, layer);
            for p in &c.pictures {
                if let Some(k) = me.kitty.get(&kitty_key(p, turn)) {
                    blit(buf, pic, r, k);
                }
            }
        };
        if let Some(b) = &scene.base {
            grid(self, &mut buf, &mut pic, b, false);
        }
        for l in &scene.layers {
            match &l.show {
                Show::Cells(c) => grid(self, &mut buf, &mut pic, c, true),
                Show::Picture(_) => {
                    if let Some(p) = self.pictures.get(&l.id) {
                        blit(&mut buf, &mut pic, r, p);
                    }
                }
            }
        }
        self.scene = scene;
        for (v, p) in buf.iter_mut().zip(&pic) {
            *v = if *p { nearest(*v) } else { curve(*v) };
        }
        buf
    }

    fn shape(&mut self, cp: u32, m: &Metrics) -> Option<Arc<Mask>> {
        self.shapes.entry((cp, m.size)).or_insert_with(|| shapes::mask(cp, m).map(Arc::new)).clone()
    }

    /// The cells of `c` that fall in `r`, drawn into `buf`. `layer` is a
    /// cells layer above the base: a cell it has not drawn and a ground in
    /// the default colour are none, and its colours may carry an alpha.
    fn draw_cells(&mut self, buf: &mut [u8], pic: &mut [bool], r: &Rect, c: &Cells, layer: bool) {
        let p = self.place(c);
        let m = p.m;
        let grid = clip(p.ox as i64, p.oy as i64, (c.cols as u32 * m.cw) as i64, (c.rows as u32 * m.ch) as i64, self.w, self.h);
        let a = grid.intersect(r);
        if a.is_empty() {
            return;
        }
        let cx0 = ((a.x - p.ox) / m.cw) as usize;
        let cx1 = ((a.right() - 1 - p.ox) / m.cw) as usize;
        let cy0 = ((a.y - p.oy) / m.ch) as usize;
        let cy1 = ((a.bottom() - 1 - p.oy) / m.ch) as usize;
        for cy in cy0..=cy1.min(c.rows - 1) {
            // A tail at the start belongs to the wide character before it,
            // and a blank to an icon spread over it.
            let first = if cx0 > 0 && (c.at(cx0, cy).ch == TAIL || spreads(c, cx0 - 1, cy)) { cx0 - 1 } else { cx0 };
            for cx in first..=cx1.min(c.cols - 1) {
                let cell = *c.at(cx, cy);
                if cell.ch == NONE {
                    continue;
                }
                let lead = cx > 0 && cell.ch == TAIL && c.at(cx - 1, cy).ch != TAIL && c.at(cx - 1, cy).ch != NONE;
                if lead || (cx > 0 && spreads(c, cx - 1, cy)) {
                    continue; // drawn with the character before it
                }
                let cells = if cell.ch != TAIL && cx + 1 < c.cols && c.at(cx + 1, cy).ch == TAIL || spreads(c, cx, cy) { 2 } else { 1 };
                let x = p.ox + cx as u32 * m.cw;
                let y = p.oy + cy as u32 * m.ch;
                self.draw_cell(buf, pic, r, &cell, x, y, cells, &m, layer);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn draw_cell(&mut self, buf: &mut [u8], pic: &mut [bool], r: &Rect, cell: &Cell, x: u32, y: u32, cells: u32, m: &Metrics, layer: bool) {
        let t = self.tones;
        // Ink and ground as greys with an alpha; a layer's unset ground is none.
        let (mut ink, mut ground) = (t.grey(cell.fg, t.ink()), if layer && cell.bg == DEFAULT { (0, 0) } else { t.grey(cell.bg, t.ground()) });
        if cell.at & REVERSE != 0 {
            let g = if cell.bg == DEFAULT { (t.ground(), 255) } else { ground };
            ground = ink;
            ink = g;
        }
        if !layer {
            ink.1 = 255;
            ground.1 = 255;
        }
        let ch = cell.ch;
        let faint = cell.at & FAINT != 0 && (BRAILLE..BRAILLE + 0x100).contains(&ch);
        let blank = ch == ' ' as u32 || ch == TAIL;
        if blank && ground.1 == 0 && cell.at & (UNDER | STRIKE) == 0 {
            return;
        }
        let w = m.cw * cells;
        let cellr = clip(x as i64, y as i64, w as i64, m.ch as i64, self.w, self.h).intersect(r);
        if cellr.is_empty() {
            return;
        }
        let mask = if blank || faint {
            None
        } else if shapes::drawn(ch) {
            self.shape(ch, m)
        } else {
            Some(self.face.glyph(ch, m, cells, cell.at & (crate::scene::BOLD | crate::scene::ITALIC)))
        };
        // Faint braille: the wall's ground between the dots, the unlit
        // dots in the cell's ground, the lit ones in ink.
        let mut faint_cov: Vec<u8> = Vec::new();
        if faint {
            faint_cov = vec![0; (m.cw * m.ch) as usize];
            for (bits, v) in [(0xff, 1u8), (ch - BRAILLE, 2u8)] {
                for d in shapes::dots(bits, m.cw, m.ch) {
                    for yy in d.y.max(0)..(d.y + d.h).min(m.ch as i32) {
                        for xx in d.x.max(0)..(d.x + d.w).min(m.cw as i32) {
                            faint_cov[(yy as u32 * m.cw + xx as u32) as usize] = v;
                        }
                    }
                }
            }
        }
        // Underline and strikethrough, as the web receiver draws them.
        let line = ((m.size as f32 / 14.0).round() as u32).max(1);
        let under = (cell.at & UNDER != 0).then(|| m.baseline() + line);
        let strike = (cell.at & STRIKE != 0).then(|| m.baseline().saturating_sub((m.size as f32 * 0.3).round() as u32));
        let on_line = |yy: u32| under.is_some_and(|u| yy >= u && yy < u + line) || strike.is_some_and(|s| yy >= s && yy < s + line);
        let wall = t.ground();
        for gy in cellr.y..cellr.bottom() {
            let yy = gy - y;
            let lined = on_line(yy);
            for gx in cellr.x..cellr.right() {
                let xx = gx - x;
                let d = ((gy - r.y) * r.w + gx - r.x) as usize;
                let beneath = buf[d];
                let mut drew = ground.1 > 0;
                let v = if faint {
                    match faint_cov[(yy * m.cw + xx % m.cw) as usize] {
                        2 => {
                            drew = true;
                            over(ink.0, ink.1, over(wall, ground.1, beneath))
                        }
                        1 => over(ground.0, ground.1, beneath),
                        _ => over(wall, ground.1, beneath),
                    }
                } else {
                    let g = over(ground.0, ground.1, beneath);
                    let cov = if lined {
                        255
                    } else {
                        match &mask {
                            Some(k) => k.cov[(yy * k.w + xx.min(k.w - 1)) as usize] as u32,
                            None => 0,
                        }
                    };
                    if cov == 0 {
                        g
                    } else {
                        drew = true;
                        over(ink.0, ((ink.1 as u32 * cov + 127) / 255) as u8, g)
                    }
                };
                buf[d] = v;
                if drew {
                    pic[d] = false;
                }
            }
        }
    }

    /// Bring a picture to the glass: scaled to its box, to greys (turned
    /// over where `turn` says), dithered onto the levels.
    fn prepare_picture(&self, p: &Picture, turn: bool) -> Prepared {
        let area = self.picture_rect(p);
        let n = area.area() as usize;
        let (mut grey, mut alpha) = (vec![0u8; n], vec![0u8; n]);
        let (sw, sh) = (p.w as usize, p.h as usize);
        let (bw, bh) = (p.size.0.max(1) as f64, p.size.1.max(1) as f64);
        if sw > 0 && sh > 0 && p.rgba.len() >= sw * sh * 4 {
            let px = |x: usize, y: usize| -> (f64, f64) {
                let i = (y * sw + x) * 4;
                let c = &p.rgba[i..i + 4];
                let a = c[3] as f64;
                (lum((c[0] as i64) << 16 | (c[1] as i64) << 8 | c[2] as i64) as f64 * a, a)
            };
            let (kx, ky) = (sw as f64 / bw, sh as f64 / bh);
            for y in 0..area.h {
                // The box's own pixel, from its top left.
                let by = (area.y + y) as i64 - p.at.1 as i64;
                let (sy0, sy1) = (by as f64 * ky, (by + 1) as f64 * ky);
                for x in 0..area.w {
                    let bx = (area.x + x) as i64 - p.at.0 as i64;
                    let (sx0, sx1) = (bx as f64 * kx, (bx + 1) as f64 * kx);
                    let (mut g, mut a, mut n) = (0.0, 0.0, 0.0);
                    if kx >= 1.0 && ky >= 1.0 {
                        // Smaller: the mean of the source pixels it covers.
                        let (x0, x1) = (sx0 as usize, (sx1.ceil() as usize).min(sw).max(sx0 as usize + 1));
                        let (y0, y1) = (sy0 as usize, (sy1.ceil() as usize).min(sh).max(sy0 as usize + 1));
                        for yy in y0..y1.min(sh) {
                            for xx in x0..x1.min(sw) {
                                let (pg, pa) = px(xx, yy);
                                g += pg;
                                a += pa;
                                n += 1.0;
                            }
                        }
                    } else {
                        // Larger: between the four nearest.
                        let fx = ((sx0 + sx1) / 2.0 - 0.5).clamp(0.0, (sw - 1) as f64);
                        let fy = ((sy0 + sy1) / 2.0 - 0.5).clamp(0.0, (sh - 1) as f64);
                        let (x0, y0) = (fx as usize, fy as usize);
                        let (x1, y1) = ((x0 + 1).min(sw - 1), (y0 + 1).min(sh - 1));
                        let (u, v) = (fx - x0 as f64, fy - y0 as f64);
                        for (xx, yy, k) in [(x0, y0, (1.0 - u) * (1.0 - v)), (x1, y0, u * (1.0 - v)), (x0, y1, (1.0 - u) * v), (x1, y1, u * v)] {
                            let (pg, pa) = px(xx, yy);
                            g += pg * k;
                            a += pa * k;
                        }
                        n = 1.0;
                    }
                    let i = (y * area.w + x) as usize;
                    if a > 0.0 {
                        let v = (g / a).round().clamp(0.0, 255.0) as u8;
                        grey[i] = if turn { paper_levels(255 - v) } else { v };
                        alpha[i] = (a / n).round().clamp(0.0, 255.0) as u8;
                    }
                }
            }
            dither(&mut grey, &alpha, area.w as usize, area.h as usize, self.dither);
        }
        Prepared { picture: p.clone(), area, grey, alpha }
    }

    /// Where the glass differs from what the panel shows, as at most
    /// MAX_BOXES rectangles each drawn tight round what differs. What is
    /// shown is not changed: `shown_now` says what a push put up.
    pub fn boxes(&mut self) -> Vec<Rect> {
        let mut found = Vec::new();
        for ty in 0..self.th {
            let mut tx = 0;
            while tx < self.tw {
                let i = (ty * self.tw + tx) as usize;
                if !self.pending[i] || !self.square_differs(tx, ty) {
                    self.pending[i] = false;
                    tx += 1;
                    continue;
                }
                let start = tx;
                while tx < self.tw && self.pending[(ty * self.tw + tx) as usize] && self.square_differs(tx, ty) {
                    tx += 1;
                }
                found.push(clip((start * TILE) as i64, (ty * TILE) as i64, ((tx - start) * TILE) as i64, TILE as i64, self.w, self.h));
            }
        }
        merge(found, NEAR, MAX_BOXES).into_iter().map(|r| self.tight(&r)).filter(|r| !r.is_empty()).collect()
    }

    fn square_differs(&self, tx: u32, ty: u32) -> bool {
        let sq = clip((tx * TILE) as i64, (ty * TILE) as i64, TILE as i64, TILE as i64, self.w, self.h);
        (sq.y..sq.bottom()).any(|y| {
            let a = (y * self.w + sq.x) as usize;
            self.glass[a..a + sq.w as usize] != self.shown[a..a + sq.w as usize]
        })
    }

    /// The part of `r` where the glass differs from what is shown.
    fn tight(&self, r: &Rect) -> Rect {
        let (mut x0, mut y0, mut x1, mut y1) = (u32::MAX, u32::MAX, 0, 0);
        for y in r.y..r.bottom() {
            let a = (y * self.w) as usize;
            for x in r.x..r.right() {
                if self.glass[a + x as usize] != self.shown[a + x as usize] {
                    x0 = x0.min(x);
                    x1 = x1.max(x + 1);
                    y0 = y0.min(y);
                    y1 = y1.max(y + 1);
                }
            }
        }
        if x1 == 0 {
            return Rect::default();
        }
        Rect::new(x0, y0, x1 - x0, y1 - y0)
    }

    /// The panel now shows the glass in `r`.
    pub fn shown_now(&mut self, r: &Rect) {
        for y in r.y..r.bottom() {
            let a = (y * self.w + r.x) as usize;
            let b = a + r.w as usize;
            let (glass, shown) = (&self.glass[a..b], &mut self.shown[a..b]);
            shown.copy_from_slice(glass);
        }
    }

    /// The panel now shows the whole glass.
    pub fn shown_all(&mut self) {
        self.shown.copy_from_slice(&self.glass);
        self.pending.iter_mut().for_each(|p| *p = false);
    }

    /// Whether anything waits to be shown.
    pub fn waiting(&self) -> bool {
        self.pending.iter().any(|&p| p)
    }

    /// The scene last taken.
    pub fn scene(&self) -> &Scene {
        &self.scene
    }

    pub fn layer(&self, id: u64) -> Option<&Layer> {
        self.scene.layers.iter().find(|l| l.id == id)
    }
}

fn kitty_key(p: &Picture, turn: bool) -> (usize, i32, i32, u32, u32, bool) {
    (Arc::as_ptr(&p.rgba) as usize, p.at.0, p.at.1, p.size.0, p.size.1, turn)
}

/// A prepared picture over the region `r`; where it is opaque, the pixel
/// is the picture's.
fn blit(buf: &mut [u8], pic: &mut [bool], r: &Rect, p: &Prepared) {
    let a = p.area.intersect(r);
    for y in a.y..a.bottom() {
        for x in a.x..a.right() {
            let s = ((y - p.area.y) * p.area.w + x - p.area.x) as usize;
            let d = ((y - r.y) * r.w + x - r.x) as usize;
            buf[d] = over(p.grey[s], p.alpha[s], buf[d]);
            if p.alpha[s] == 255 {
                pic[d] = true;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scene::{BOLD, ITALIC};

    fn comp() -> Compositor {
        Compositor::new(400, 300, Tones { paper: true }, Dither::Diffuse)
    }

    fn base(c: &Compositor, font: u32) -> Cells {
        let (cols, rows) = c.face.area(font, c.w, c.h);
        Cells::new(font, cols, rows, Cell::BLANK)
    }

    fn levels(c: &Compositor) -> bool {
        c.glass.iter().all(|&v| v % 17 == 0)
    }

    #[test]
    fn a_blank_wall_is_paper() {
        let mut c = comp();
        let b = base(&c, 17);
        c.update(&Scene { base: Some(b), layers: vec![] });
        assert!(c.glass.iter().all(|&v| v == 255));
        assert!(c.boxes().is_empty(), "the panel starts white, so nothing to push");
    }

    #[test]
    fn text_is_dark_ink_on_paper_and_only_its_cells_change() {
        let mut c = comp();
        let mut b = base(&c, 17);
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        b.text(3, 2, "Hi", DEFAULT, DEFAULT, 0);
        assert!(c.update(&Scene { base: Some(b.clone()), layers: vec![] }));
        assert!(levels(&c));
        let boxes = c.boxes();
        assert_eq!(boxes.len(), 1);
        let p = c.place(&b);
        let cell = c.cells_rect(&p, 3, 5, 2);
        let r = boxes[0];
        assert!(cell.intersect(&r) == r, "the box {r:?} is inside the cells {cell:?}");
        assert!(c.glass.iter().any(|&v| v == 0), "default ink on paper is black");
        // Shown, then the same scene again: nothing.
        c.shown_now(&r);
        assert!(!c.update(&Scene { base: Some(b.clone()), layers: vec![] }));
        assert!(c.boxes().is_empty());
    }

    #[test]
    fn an_icon_before_a_blank_spreads_over_it() {
        let wifi = '\u{f05a9}';
        let ink = |c: &Compositor, b: &Cells, x: usize| {
            let r = c.cells_rect(&c.place(b), x, x + 1, 1);
            (r.y..r.bottom()).flat_map(|y| (r.x..r.right()).map(move |x| (x, y))).filter(|&(x, y)| c.glass[(y * c.w + x) as usize] < 255).count()
        };
        let mut c = comp();
        let mut b = base(&c, 22);
        b.set(2, 1, Cell::new(wifi, DEFAULT, DEFAULT, 0));
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        assert!(ink(&c, &b, 2) > 0 && ink(&c, &b, 3) > 0, "the icon is across both cells");
        // A letter after it keeps it to its one cell, and the change
        // redraws the icon's own cell too.
        b.set(3, 1, Cell::new('x', DEFAULT, DEFAULT, 0));
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        let mut alone = comp();
        let mut a = base(&alone, 22);
        a.set(2, 1, Cell::new(wifi, DEFAULT, DEFAULT, 0));
        a.set(3, 1, Cell::new('x', DEFAULT, DEFAULT, 0));
        alone.update(&Scene { base: Some(a.clone()), layers: vec![] });
        let squeezed = ink(&alone, &a, 2);
        assert!(squeezed > 0);
        assert_eq!(ink(&c, &b, 2), squeezed, "squeezed into one cell");
        // A blank on another ground is not the icon's to spread over.
        b.set(3, 1, Cell::new(' ', DEFAULT, 0x808080, 0));
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        assert_eq!(ink(&c, &b, 2), squeezed);
    }

    #[test]
    fn an_undone_change_pushes_nothing() {
        let mut c = comp();
        let b = base(&c, 17);
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        let mut b2 = b.clone();
        b2.text(0, 0, "x", DEFAULT, DEFAULT, 0);
        c.update(&Scene { base: Some(b2), layers: vec![] });
        c.update(&Scene { base: Some(b), layers: vec![] });
        assert!(c.boxes().is_empty());
    }

    #[test]
    fn paper_off_is_the_screen() {
        let mut c = Compositor::new(100, 100, Tones { paper: false }, Dither::Diffuse);
        let b = base(&c, 17);
        c.update(&Scene { base: Some(b), layers: vec![] });
        assert!(c.glass.iter().all(|&v| v == 0));
    }

    #[test]
    fn a_layer_shows_through_where_it_has_not_drawn() {
        let mut c = comp();
        let mut b = base(&c, 17);
        b.text(0, 0, "base", DEFAULT, DEFAULT, 0);
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        let before = c.glass.clone();
        let (cols, rows) = (b.cols, b.rows);
        let mut l = Cells::new(17, cols, rows, Cell::UNDRAWN);
        // A space in the default colours takes nothing away.
        l.set(1, 0, Cell::BLANK);
        let layer = Layer { id: 7, show: Show::Cells(l.clone()) };
        c.update(&Scene { base: Some(b.clone()), layers: vec![layer] });
        assert_eq!(c.glass, before);
        // A solid ground covers; half an alpha mixes.
        l.set(0, 0, Cell { ch: ' ' as u32, fg: DEFAULT, bg: 0x000000, at: 0 });
        l.set(2, 0, Cell { ch: ' ' as u32, fg: DEFAULT, bg: 128 << 24 | 0xffffff, at: 0 });
        c.update(&Scene { base: Some(b), layers: vec![Layer { id: 7, show: Show::Cells(l.clone()) }] });
        let m = c.face.metrics(17);
        let p = c.place(&l);
        let at = |x: u32, y: u32| c.glass[((p.oy + y) * c.w + p.ox + x) as usize];
        assert_eq!(at(1, m.ch / 2), 255, "black on paper is white");
        assert_eq!(at(2 * m.cw + 1, 1), curve(255 - 127), "half white, which is black on paper, over white paper");
    }

    #[test]
    fn a_picture_is_not_turned_over_and_is_dithered() {
        let mut c = comp();
        let b = base(&c, 17);
        // A dark grey picture, 10x10, scaled to 40x40.
        let rgba: Vec<u8> = (0..100).flat_map(|_| [60u8, 60, 60, 255]).collect();
        let pic = Picture { rgba: Arc::new(rgba), w: 10, h: 10, at: (20, 30), size: (40, 40) };
        c.update(&Scene { base: Some(b), layers: vec![Layer { id: 1, show: Show::Picture(pic) }] });
        let inside: Vec<u8> = (30..70).flat_map(|y| (20..60).map(move |x| (x, y))).map(|(x, y)| c.glass[y * 400 + x]).collect();
        let mean = inside.iter().map(|&v| v as f64).sum::<f64>() / inside.len() as f64;
        assert!((mean - 60.0).abs() < 2.0, "mean {mean}");
        assert!(inside.contains(&51) && inside.contains(&68));
        assert_eq!(c.glass[29 * 400 + 20], 255);
        let boxes = c.boxes();
        assert_eq!(boxes, vec![Rect::new(20, 30, 40, 40)]);
    }

    #[test]
    fn a_picture_moved_marks_where_it_was() {
        let mut c = comp();
        let b = base(&c, 17);
        let rgba = Arc::new(vec![0u8; 4 * 4 * 4].chunks(4).flat_map(|_| [0, 0, 0, 255]).collect::<Vec<u8>>());
        let p1 = Picture { rgba: rgba.clone(), w: 4, h: 4, at: (0, 0), size: (16, 16) };
        c.update(&Scene { base: Some(b.clone()), layers: vec![Layer { id: 1, show: Show::Picture(p1) }] });
        c.shown_all();
        let p2 = Picture { rgba, w: 4, h: 4, at: (200, 200), size: (16, 16) };
        c.update(&Scene { base: Some(b), layers: vec![Layer { id: 1, show: Show::Picture(p2) }] });
        let boxes = c.boxes();
        assert_eq!(boxes.len(), 2);
        assert!(boxes.contains(&Rect::new(0, 0, 16, 16)) && boxes.contains(&Rect::new(200, 200, 16, 16)));
    }

    #[test]
    fn styles_draw() {
        let mut c = comp();
        let mut b = base(&c, 20);
        b.text(0, 0, "a", DEFAULT, DEFAULT, 0);
        b.text(1, 0, "a", DEFAULT, DEFAULT, BOLD);
        b.text(2, 0, "a", DEFAULT, DEFAULT, ITALIC);
        b.text(3, 0, " ", DEFAULT, DEFAULT, UNDER);
        b.text(4, 0, "a", 0xffffff, 0x000000, REVERSE);
        c.update(&Scene { base: Some(b.clone()), layers: vec![] });
        let p = c.place(&b);
        let ink = |cx: u32| -> usize {
            let m = p.m;
            (0..m.ch).flat_map(|y| (0..m.cw).map(move |x| (x, y))).filter(|&(x, y)| c.glass[((p.oy + y) * c.w + p.ox + cx * m.cw + x) as usize] < 128).count()
        };
        assert!(ink(1) > ink(0), "bold is heavier");
        assert!(ink(2) > 0 && ink(3) > 0);
        assert!(ink(4) > (p.m.cw * p.m.ch / 2) as usize, "reverse on paper is a dark cell");
    }

    #[test]
    fn full_grid_renders_at_the_panels_size() {
        let mut c = Compositor::new(1872, 1404, Tones { paper: true }, Dither::Diffuse);
        let (cols, rows) = (187, 70);
        let mut b = Cells::new(15, cols, rows, Cell::BLANK);
        for y in 0..rows {
            let s: String = (0..cols).map(|x| char::from_u32(0x21 + ((x + y) % 90) as u32).unwrap()).collect();
            b.text(0, y, &s, DEFAULT, DEFAULT, 0);
        }
        let t = std::time::Instant::now();
        c.update(&Scene { base: Some(b), layers: vec![] });
        let ms = t.elapsed().as_secs_f64() * 1e3;
        eprintln!("full {cols}x{rows} grid composed in {ms:.1} ms");
        assert!(levels(&c));
        let t = std::time::Instant::now();
        let boxes = c.boxes();
        eprintln!("boxes {:?} in {:.1} ms", boxes, t.elapsed().as_secs_f64() * 1e3);
        assert_eq!(boxes.len(), 1);
    }
}
