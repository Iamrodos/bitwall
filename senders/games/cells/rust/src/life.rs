//! The population (`../life.py`), ported: soft organisms that live, split,
//! merge and die, drawn as a frame of half blocks. Knows nothing of a wall
//! beyond `bitwall::frame::Frame`.
//!
//! Every organism is a handful of nodes, each radiating a field that falls
//! off with distance, and a pixel is inside a body where the summed field
//! crosses a threshold; `life.py` says why that one rule gives necks,
//! waists and dissolving deaths for nothing. This is the same population
//! with the same rules and constants. Its random numbers are its own, so a
//! seed does not give Python's run; what is held to Python is the picture:
//! given the same organisms, `paint` gives exactly the cells
//! `World.arrays` gives, which `tests/golden.py` and the test below check
//! cell for cell.
//!
//! The field is the cost, and the reason for this port. It is summed in
//! f64 with the arithmetic in numpy's order, so every pixel's colour is the
//! same double Python computes before it is quantised; the pixel rows are
//! shared out between threads with rayon, two to a cell row, and a cell row
//! goes from field to glyphs without leaving its thread.

use std::collections::VecDeque;
use std::f64::consts::PI;
use std::time::Instant;

use bitwall::frame::Frame;
use rayon::prelude::*;

/// How far a node's field reaches, in radii, and how soft its centre is.
/// Both fix the threshold: a lone node's surface sits at exactly one radius.
pub const SUPPORT: f64 = 2.5;
pub const CORE: f64 = 0.35;
const CORE2: f64 = CORE * CORE;
const INV_FAR: f64 = 1.0 / (SUPPORT * SUPPORT + CORE2);
pub const THRESHOLD: f64 = 1.0 / (1.0 + CORE2) - INV_FAR;

/// Colours are rounded to this step, so that a slow shimmer inside a body
/// does not re-send every cell of it each frame.
pub const STEP: i64 = 12;

/// The grid this is composed for; past it a body grows more slowly than the
/// grid and the count makes up the difference (`life.py`, `NOMINAL`).
pub const NOMINAL: (usize, usize) = (150, 45);
const SIZE_EXP: f64 = 0.6;
const COUNT_EXP: f64 = 2.0 - 2.0 * SIZE_EXP;

fn nominal_scale() -> f64 {
    ((NOMINAL.0 * NOMINAL.1 * 2) as f64).sqrt()
}

const BIRTH_SECS: f64 = 3.5;
const DEATH_SECS: f64 = 5.0;
/// How long after a split the halves refuse to rejoin, and after a merge a
/// body holds together before it may split again.
const SPLIT_REFRACTORY: f64 = 25.0;
const MERGE_REFRACTORY: f64 = 25.0;
/// How long a body goes without appetite after it is born, joins, or
/// touches something it does not join, drawn afresh each time so the
/// population does not fall into step.
const APPETITE: (f64, f64) = (12.0, 40.0);
/// What a split costs each child and a merge gives back, as a share of
/// lifespan.
const SPLIT_COST: f64 = 0.10;
const MERGE_GAIN: f64 = 0.12;
/// Trail points are this far apart, and this many are kept, so a chain has
/// a path to follow even when a merge has made it long.
const TRAIL_STEP: f64 = 0.3;
const TRAIL_LEN: usize = 600;

// ------------------------------------------------------------------ rng

/// xoshiro256**, seeded through splitmix64: small, fast, and enough for
/// an animation that only has to look random.
#[derive(Clone, Debug)]
pub struct Rng([u64; 4]);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        let mut z = seed;
        let mut s = [0u64; 4];
        for v in &mut s {
            z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut x = z;
            x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            *v = x ^ (x >> 31);
        }
        Rng(s)
    }

    /// A seed from the clock, for a run that need not repeat.
    pub fn from_clock() -> Rng {
        let ns = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x1234_5678);
        Rng::new(ns ^ ((std::process::id() as u64) << 32))
    }

    pub fn next_u64(&mut self) -> u64 {
        let s = &mut self.0;
        let out = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = s[1] << 17;
        s[2] ^= s[0];
        s[3] ^= s[1];
        s[1] ^= s[2];
        s[0] ^= s[3];
        s[2] ^= t;
        s[3] = s[3].rotate_left(45);
        out
    }

    /// Uniform in [0, 1).
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// Uniform between a and b, as Python's `random.uniform` has it.
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.random()
    }

    pub fn gauss(&mut self) -> f64 {
        let u = 1.0 - self.random();
        let v = self.random();
        (-2.0 * u.ln()).sqrt() * (2.0 * PI * v).cos()
    }

    pub fn expovariate(&mut self, rate: f64) -> f64 {
        -(1.0 - self.random()).ln() / rate
    }

    pub fn sign(&mut self) -> f64 {
        if self.next_u64() & 1 == 0 { -1.0 } else { 1.0 }
    }
}

// ------------------------------------------------------------- numerics

/// A sum in the order numpy's `.sum()` takes it -- the first element, then
/// the rest summed pairwise eight lanes wide -- so that a mass summed here
/// is the same double Python has, and radii settled from it match too.
pub fn np_sum(a: &[f64]) -> f64 {
    fn pairwise(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut r = -0.0;
            for &x in a {
                r += x;
            }
            r
        } else if n <= 128 {
            let mut r = [0.0; 8];
            r.copy_from_slice(&a[..8]);
            let mut i = 8;
            while i < n - n % 8 {
                for j in 0..8 {
                    r[j] += a[i + j];
                }
                i += 8;
            }
            let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            while i < n {
                res += a[i];
                i += 1;
            }
            res
        } else {
            let mut n2 = n / 2;
            n2 -= n2 % 8;
            pairwise(&a[..n2]) + pairwise(&a[n2..])
        }
    }
    match a.len() {
        0 => 0.0,
        _ => a[0] + pairwise(&a[1..]),
    }
}

fn sum_sq(a: &[f64]) -> f64 {
    let sq: Vec<f64> = a.iter().map(|v| v * v).collect();
    np_sum(&sq)
}

/// Python's float `%`: the result takes the divisor's sign.
fn pymod(x: f64, m: f64) -> f64 {
    let r = x % m;
    if r != 0.0 && (r < 0.0) != (m < 0.0) { r + m } else { r }
}

/// `colorsys.hsv_to_rgb`, step for step.
pub fn hsv_to_rgb(h: f64, s: f64, v: f64) -> [f64; 3] {
    if s == 0.0 {
        return [v, v, v];
    }
    let i = (h * 6.0) as i64;
    let f = (h * 6.0) - i as f64;
    let p = v * (1.0 - s);
    let q = v * (1.0 - s * f);
    let t = v * (1.0 - s * (1.0 - f));
    match i.rem_euclid(6) {
        0 => [v, t, p],
        1 => [q, v, p],
        2 => [p, v, t],
        3 => [p, q, v],
        4 => [t, p, v],
        _ => [v, p, q],
    }
}

/// numpy's `linspace`, endpoint included or not.
fn linspace(start: f64, stop: f64, n: usize, endpoint: bool) -> Vec<f64> {
    let div = if endpoint { n.saturating_sub(1) } else { n };
    let step = if div > 0 { (stop - start) / div as f64 } else { 0.0 };
    let mut v: Vec<f64> = (0..n).map(|i| i as f64 * step + start).collect();
    if endpoint && n > 1 {
        v[n - 1] = stop;
    }
    v
}

// ---------------------------------------------------------------- kinds

/// One kind of organism and the ranges it is drawn from. `size` is the
/// radius of a whole-body circle as a fraction of the grid's geometric mean
/// side, `speed` is in those units a second, `hues` the palette slice in
/// degrees, `wander` how restlessly the heading drifts.
#[derive(Debug)]
pub struct Kind {
    pub name: &'static str,
    pub size: (f64, f64),
    pub speed: (f64, f64),
    pub lifespan: (f64, f64),
    pub hues: (f64, f64),
    pub nodes: usize,
    pub weight: f64,
    pub wander: f64,
    pub splits: bool,
    pub wiggle: f64,
    pub dash: f64,
    pub cohesion: f64,
    pub momentum: f64,
}

pub static KINDS: [Kind; 3] = [
    Kind {
        name: "drifter", size: (0.060, 0.085), speed: (0.014, 0.028),
        lifespan: (200.0, 360.0), hues: (150.0, 300.0), nodes: 6, weight: 0.34,
        wander: 0.5, splits: true, wiggle: 0.0, dash: 0.0, cohesion: 2.5,
        momentum: 1.8,
    },
    Kind {
        name: "swimmer", size: (0.055, 0.075), speed: (0.075, 0.125),
        lifespan: (140.0, 240.0), hues: (320.0, 420.0), nodes: 18, weight: 0.38,
        wander: 0.6, splits: true, wiggle: 0.55, dash: 0.0, cohesion: 7.0,
        momentum: 3.5,
    },
    Kind {
        name: "mote", size: (0.018, 0.026), speed: (0.10, 0.18),
        lifespan: (60.0, 120.0), hues: (60.0, 150.0), nodes: 3, weight: 0.28,
        wander: 2.2, splits: false, wiggle: 0.0, dash: 2.0, cohesion: 8.0,
        momentum: 3.5,
    },
];

const DRIFTER: usize = 0;
const SWIMMER: usize = 1;
const MOTE: usize = 2;

pub fn kind_index(name: &str) -> Option<usize> {
    KINDS.iter().position(|k| k.name == name)
}

// --------------------------------------------------------------- config

/// Everything an operator can turn, with the defaults. Values are clamped
/// in `clamp()` rather than refused, because a wall has nobody at the
/// keyboard to read an error, and a slightly wrong animation beats none.
#[derive(Clone, Debug)]
pub struct Config {
    /// Organisms aimed for at the nominal grid.
    pub population: f64,
    pub split: bool,
    pub merge: bool,
    pub birth: bool,
    pub death: bool,
    pub growth: bool,
    pub aura: bool,
    /// Multipliers on every kind's speed, size and lifespan.
    pub speed: f64,
    pub size: f64,
    pub lifespan: f64,
    /// A base hue in degrees, or each kind's own palette.
    pub hue: Option<f64>,
    /// Hue spread either side of `hue`.
    pub spread: f64,
    pub kinds: Vec<String>,
    pub seed: Option<u64>,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            population: 8.0,
            split: true,
            merge: true,
            birth: true,
            death: true,
            growth: true,
            aura: true,
            speed: 1.0,
            size: 1.0,
            lifespan: 1.0,
            hue: None,
            spread: 40.0,
            kinds: KINDS.iter().map(|k| k.name.to_string()).collect(),
            seed: None,
        }
    }
}

impl Config {
    /// Pull every value into its working range; says what it changed.
    pub fn clamp(&mut self) -> Vec<String> {
        let mut notes = Vec::new();
        let fresh = Config::default();
        let mut rng = |name: &str, v: &mut f64, lo: f64, hi: f64, dflt: f64| {
            if v.is_nan() {
                notes.push(format!("{name}: not a number, using default"));
                *v = dflt;
                return;
            }
            let c = v.clamp(lo, hi);
            if c != *v {
                notes.push(format!("{name}: {v} is outside {lo}..{hi}, using {c}"));
            }
            *v = c;
        };
        rng("population", &mut self.population, 1.0, 60.0, fresh.population);
        self.population = self.population.round_ties_even();
        rng("speed", &mut self.speed, 0.1, 5.0, fresh.speed);
        rng("size", &mut self.size, 0.3, 3.0, fresh.size);
        rng("lifespan", &mut self.lifespan, 0.1, 10.0, fresh.lifespan);
        rng("spread", &mut self.spread, 0.0, 180.0, fresh.spread);
        if let Some(h) = self.hue {
            if h.is_finite() {
                self.hue = Some(pymod(h, 360.0));
            } else {
                notes.push("hue: not a number, using the palettes".into());
                self.hue = None;
            }
        }
        let kinds: Vec<String> =
            self.kinds.iter().filter(|k| kind_index(k).is_some()).cloned().collect();
        if kinds.is_empty() {
            notes.push("kinds: none of those are kinds, using all".into());
            self.kinds = fresh.kinds;
        } else {
            if kinds.len() != self.kinds.len() {
                notes.push("kinds: ignoring the ones that are not kinds".into());
            }
            self.kinds = kinds;
        }
        notes
    }
}

// ------------------------------------------------------------- organism

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Born,
    Alive,
    Dying,
}

impl State {
    pub fn parse(s: &str) -> Option<State> {
        match s {
            "born" => Some(State::Born),
            "alive" => Some(State::Alive),
            "dying" => Some(State::Dying),
            _ => None,
        }
    }
}

/// What an organism needs of its world while it steps: the grid, its
/// scale, and how full the wall is at that moment.
#[derive(Clone, Copy, Debug)]
struct Env {
    w: f64,
    h: f64,
    scale: f64,
    count: usize,
    target: f64,
}

impl Env {
    fn room(&self) -> f64 {
        (1.0 - self.count as f64 / (1.5 * self.target)).max(0.0)
    }
}

/// One body: a head that steers, and nodes that carry its field.
///
/// The head is the point that moves; nodes are absolute positions with
/// radii, and each step they ease toward where the body plan puts them.
/// `extra` marks nodes the plan has no slot for -- inherited from a merge
/// or a split -- which shrink to nothing while the planned ones grow.
#[derive(Clone, Debug)]
pub struct Organism {
    pub id: u64,
    pub kind: usize,
    pub x: f64,
    pub y: f64,
    pub heading: f64,
    pub turn: f64,
    pub vx: f64,
    pub vy: f64,
    /// What the neighbours are doing to it.
    pub fx: f64,
    pub fy: f64,
    /// A body it is drawn toward: its id, and where it was when chosen, for
    /// when it has gone since.
    pub partner: Option<(u64, f64, f64)>,
    /// Seconds left of ignoring the crowd.
    pub soft: f64,
    pub hue: f64,
    pub age: f64,
    pub lifespan: f64,
    pub speed: f64,
    pub size: f64,
    pub phase: f64,
    /// Motes: seconds of burst left, and of gliding before the next.
    pub dash: f64,
    pub glide: f64,
    pub state: State,
    /// Seconds in this state.
    pub since: f64,
    pub no_merge: f64,
    pub no_split: f64,
    pub trail: VecDeque<(f64, f64)>,
    pub ph: Vec<f64>,
    pub fq: Vec<f64>,
    pub ang: Vec<f64>,
    /// Hue offset per node, and the offsets the plan wants: a body is a
    /// gradient, and a node from another body keeps its hue a while.
    pub plan_tint: Vec<f64>,
    pub tint: Vec<f64>,
    pub nx: Vec<f64>,
    pub ny: Vec<f64>,
    pub nr: Vec<f64>,
    pub extra: Vec<bool>,
    /// What a newborn is gathering toward, and what a dying body had when
    /// it began to go.
    pub target_mass: f64,
    pub mass_at_death: f64,
    pub mass: f64,
    dead: bool,
}

impl Organism {
    #[allow(clippy::too_many_arguments)]
    fn new(
        rng: &mut Rng, cfg: &Config, env: &Env, id: u64, kind: usize, x: f64, y: f64,
        mass: f64, hue: f64, age: f64, lifespan: Option<f64>, born: bool,
    ) -> Organism {
        let k = &KINDS[kind];
        let n = k.nodes;
        let heading = rng.uniform(0.0, 2.0 * PI);
        let lifespan =
            lifespan.unwrap_or_else(|| rng.uniform(k.lifespan.0, k.lifespan.1) * cfg.lifespan);
        let speed = rng.uniform(k.speed.0, k.speed.1);
        let size = rng.uniform(k.size.0, k.size.1);
        let phase = rng.uniform(0.0, 2.0 * PI);
        let glide = rng.uniform(1.0, 3.0);
        let no_merge = if born { rng.uniform(APPETITE.0, APPETITE.1) } else { 0.0 };
        let ph: Vec<f64> = (0..n).map(|_| rng.uniform(0.0, 2.0 * PI)).collect();
        let fq: Vec<f64> = (0..n).map(|_| rng.uniform(0.25, 0.6)).collect();
        let ang: Vec<f64> = linspace(0.0, 2.0 * PI, n, false)
            .into_iter()
            .map(|a| a + rng.uniform(-0.4, 0.4))
            .collect();
        let plan_tint = if kind == SWIMMER {
            linspace(12.0, -28.0, n, true)
        } else {
            let mut t: Vec<f64> = (0..n).map(|_| rng.uniform(-10.0, 10.0)).collect();
            t[0] = 0.0;
            t
        };
        let mut o = Organism {
            id,
            kind,
            x,
            y,
            heading,
            turn: 0.0,
            vx: 0.0,
            vy: 0.0,
            fx: 0.0,
            fy: 0.0,
            partner: None,
            soft: 0.0,
            hue,
            age,
            lifespan,
            speed,
            size,
            phase,
            dash: 0.0,
            glide,
            state: if born { State::Born } else { State::Alive },
            since: 0.0,
            no_merge,
            no_split: if born { BIRTH_SECS } else { 0.0 },
            trail: VecDeque::from([(x, y)]),
            ph,
            fq,
            ang,
            tint: plan_tint.clone(),
            plan_tint,
            nx: vec![x; n],
            ny: vec![y; n],
            nr: vec![0.0; n],
            extra: vec![false; n],
            target_mass: mass,
            mass_at_death: mass,
            mass,
            dead: false,
        };
        if born {
            // Scattered and faint, so the body draws itself together.
            o.mass = mass * 0.03;
            let s = mass.sqrt() * 2.5;
            for i in 0..n {
                o.nx[i] = (x + rng.uniform(-s, s)).clamp(1.0, env.w - 2.0);
                o.ny[i] = (y + rng.uniform(-s, s)).clamp(1.0, env.h - 2.0);
            }
        }
        o.settle_radii();
        o
    }

    fn kind(&self) -> &'static Kind {
        &KINDS[self.kind]
    }

    /// Radius of the one circle that would carry this body's mass.
    pub fn radius(&self) -> f64 {
        self.mass.max(1e-6).sqrt()
    }

    fn nominal_mass(&self, scale: f64, cfg: &Config) -> f64 {
        let r = self.size * scale * cfg.size;
        r * r
    }

    pub fn life(&self) -> f64 {
        (self.age / self.lifespan).min(1.0)
    }

    pub fn alive(&self) -> bool {
        self.state == State::Alive
    }

    /// Each node's colour now, in 0..1. Youth is saturated and bright, age
    /// drains both; a newborn arrives pale and a dying body goes dark.
    pub fn colours(&self) -> Vec<[f64; 3]> {
        let a = self.life();
        let mut s = 0.92 - 0.4 * a;
        let mut v = 1.0 - 0.25 * a;
        match self.state {
            State::Born => {
                let u = (self.since / BIRTH_SECS).min(1.0);
                s *= 0.25 + 0.75 * u;
                v *= 0.7 + 0.3 * u;
            }
            State::Dying => {
                let u = (self.since / DEATH_SECS).min(1.0);
                s *= 1.0 - 0.6 * u;
                v *= (1.0 - u).powf(1.3);
            }
            State::Alive => {}
        }
        self.tint
            .iter()
            .map(|t| hsv_to_rgb(pymod(self.hue + t, 360.0) / 360.0, s, v))
            .collect()
    }

    /// Where the plan wants each node, and how big, unnormalised.
    fn plan(&self, t: f64, spread: f64) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let r = self.radius();
        let n = self.kind().nodes;
        if self.kind == SWIMMER {
            return self.plan_chain(n, r, spread);
        }
        if self.kind == MOTE {
            let a = if self.vx.abs() + self.vy.abs() > 1e-6 {
                self.vy.atan2(self.vx)
            } else {
                self.heading
            };
            let d = r * 0.8 * spread;
            let (ca, sa) = (a.cos(), a.sin());
            let tx = (0..n).map(|i| self.x - ca * (i as f64 * d)).collect();
            let ty = (0..n).map(|i| self.y - sa * (i as f64 * d)).collect();
            let den = (n.max(2) - 1) as f64;
            let tr = (0..n).map(|i| 1.0 - 0.5 * i as f64 / den).collect();
            return (tx, ty, tr);
        }
        // A drifter: a core, and satellites breathing in and out around it.
        let tau = 2.0 * PI;
        let mut tx = Vec::with_capacity(n);
        let mut ty = Vec::with_capacity(n);
        let mut tr = Vec::with_capacity(n);
        for i in 0..n {
            let (fq, ph) = (self.fq[i], self.ph[i]);
            let rho = r * (0.55 + 0.35 * (fq * t * tau + ph).sin()) * spread;
            let ang = self.ang[i] + 0.35 * (0.7 * fq * t * tau + 2.1 * ph).sin();
            tx.push(self.x + rho * ang.cos());
            ty.push(self.y + rho * ang.sin());
            tr.push(0.5 + 0.12 * (1.3 * fq * t * tau + 3.0 * ph).sin());
        }
        tx[0] = self.x;
        ty[0] = self.y;
        tr[0] = 1.0;
        (tx, ty, tr)
    }

    /// Nodes strung back along the trail at a steady spacing, half a head
    /// radius apart: close enough that the field is a tube, not beads.
    fn plan_chain(&self, n: usize, r: f64, spread: f64) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let den = (n.max(2) - 1) as f64;
        let taper: Vec<f64> =
            (0..n).map(|i| 1.0 - 0.5 * (i as f64 / den).powf(1.4)).collect();
        let head = r / sum_sq(&taper).sqrt();
        let gap = head * 0.55 * spread;
        let pts = &self.trail;
        let mut tx = vec![0.0; n];
        let mut ty = vec![0.0; n];
        let (mut px, mut py) = *pts.back().unwrap();
        tx[0] = px;
        ty[0] = py;
        let mut k = 1;
        let mut acc = 0.0;
        for i in (0..pts.len().saturating_sub(1)).rev() {
            if k >= n {
                break;
            }
            let (qx, qy) = pts[i];
            let seg = (qx - px).hypot(qy - py);
            while k < n && acc + seg >= gap * k as f64 {
                let f = if seg > 0.0 { (gap * k as f64 - acc) / seg } else { 0.0 };
                tx[k] = px + (qx - px) * f;
                ty[k] = py + (qy - py) * f;
                k += 1;
            }
            acc += seg;
            px = qx;
            py = qy;
        }
        if k < n {
            // A trail shorter than the body: lay the rest out straight
            // behind the last point, so a newborn is a body and not a heap.
            let (hx, hy) = (self.heading.cos(), self.heading.sin());
            for j in k..n {
                let s = gap * (j - k + 1) as f64;
                tx[j] = px - hx * s;
                ty[j] = py - hy * s;
            }
        }
        (tx, ty, taper)
    }

    /// Scale every node so the body weighs exactly its mass.
    fn settle_radii(&mut self) {
        let tot = sum_sq(&self.nr);
        if tot <= 1e-12 {
            let n = self.nr.len();
            let r = (self.mass.max(1e-9) / n as f64).sqrt();
            self.nr = vec![r; n];
            return;
        }
        let f = (self.mass / tot).sqrt();
        for r in &mut self.nr {
            *r *= f;
        }
    }

    /// Take these nodes as the body, whatever the plan wanted. The largest
    /// fill the plan's slots (or the first, where `order` matters, as for a
    /// chain); the rest are extras to be absorbed. `hue` is each node's
    /// absolute hue, which it keeps for now.
    fn adopt(&mut self, mut nx: Vec<f64>, mut ny: Vec<f64>, mut nr: Vec<f64>,
             mut hue: Vec<f64>, order: bool) {
        let n = self.kind().nodes;
        if !order {
            let mut idx: Vec<usize> = (0..nr.len()).collect();
            idx.sort_by(|&a, &b| nr[b].total_cmp(&nr[a]));
            nx = idx.iter().map(|&i| nx[i]).collect();
            ny = idx.iter().map(|&i| ny[i]).collect();
            hue = idx.iter().map(|&i| hue[i]).collect();
            nr = idx.iter().map(|&i| nr[i]).collect();
        }
        while nr.len() < n {
            nx.push(self.x);
            ny.push(self.y);
            nr.push(0.0);
            hue.push(self.hue);
        }
        let m = nr.len();
        self.tint = hue.iter().map(|h| pymod(h - self.hue + 180.0, 360.0) - 180.0).collect();
        self.extra = (0..m).map(|i| i >= n).collect();
        self.mass = sum_sq(&nr);
        self.target_mass = self.mass;
        self.nx = nx;
        self.ny = ny;
        self.nr = nr;
    }

    fn step(&mut self, dt: f64, t: f64, cfg: &Config, env: &Env,
            partner: Option<(f64, f64)>, rng: &mut Rng) {
        // A crowd ages a body: far more than the wall wants, as after a
        // resize down, and the count settles in a minute, not an hour.
        let crowd = 1.0 + 2.0 * (env.count as f64 / env.target - 1.2).max(0.0);
        self.age += dt * crowd;
        self.since += dt;
        self.no_merge = (self.no_merge - dt).max(0.0);
        self.no_split = (self.no_split - dt).max(0.0);
        self.soft = (self.soft - dt).max(0.0);
        if self.state == State::Born && self.since >= BIRTH_SECS {
            self.state = State::Alive;
            self.since = 0.0;
        }
        if self.state == State::Alive && cfg.death && self.age >= self.lifespan {
            self.state = State::Dying;
            self.since = 0.0;
            self.mass_at_death = self.mass;
        }

        // Mass: a newborn gathers it, the living grow toward the kind's
        // ceiling while there is room on the wall, the dying let it go.
        match self.state {
            State::Born => {
                let u = (self.since / BIRTH_SECS).min(1.0);
                self.mass = self.target_mass * (0.03 + 0.97 * u * u);
            }
            State::Dying => {
                let u = (self.since / DEATH_SECS).min(1.0);
                self.mass = self.mass_at_death * (1.0 - u).powf(2.2);
            }
            State::Alive if cfg.growth => {
                let cap = self.nominal_mass(env.scale, cfg) * 1.6;
                self.mass += 0.035 * self.mass * (1.0 - self.mass / cap) * env.room() * dt;
            }
            State::Alive => {}
        }

        self.motion(dt, cfg, env, partner, rng);

        let mut spread = 1.0;
        let mut cohesion = self.kind().cohesion;
        match self.state {
            State::Born => {
                let u = (self.since / BIRTH_SECS).min(1.0);
                spread = 1.0 + 1.6 * (1.0 - u).powi(2);
                cohesion *= 0.6 + 0.4 * u;
            }
            State::Dying => {
                let u = (self.since / DEATH_SECS).min(1.0);
                spread = 1.0 + 2.2 * u;
                cohesion *= (1.0 - u).max(0.08) * 0.5;
            }
            State::Alive => {}
        }
        let (tx, ty, tr) = self.plan(t, spread);
        // Extras have no slot: they sink into the head and give up their
        // radius to the planned nodes through the mass settlement.
        let n = self.kind().nodes;
        let m = self.nr.len();
        let norm = (self.mass / sum_sq(&tr).max(1e-9)).sqrt();
        let k = 1.0 - (-cohesion * dt).exp();
        let kt = 1.0 - (-0.6 * dt).exp();
        for i in 0..m {
            let (gx, gy, gr, gt) = if i < n {
                (tx[i], ty[i], tr[i] * norm, self.plan_tint[i])
            } else {
                (self.x, self.y, 0.0, 0.0)
            };
            self.nx[i] += (gx - self.nx[i]) * k;
            self.ny[i] += (gy - self.ny[i]) * k;
            self.nr[i] += (gr - self.nr[i]) * k;
            self.tint[i] += (gt - self.tint[i]) * kt;
        }
        self.settle_radii();
        if m > n {
            let lim = 0.12 * self.radius();
            let keep: Vec<bool> =
                (0..m).map(|i| !(self.extra[i] && self.nr[i] < lim)).collect();
            if keep.iter().any(|k| !k) {
                let pick = |v: &Vec<f64>| -> Vec<f64> {
                    v.iter().zip(&keep).filter(|(_, k)| **k).map(|(x, _)| *x).collect()
                };
                self.nx = pick(&self.nx);
                self.ny = pick(&self.ny);
                self.nr = pick(&self.nr);
                self.tint = pick(&self.tint);
                self.extra =
                    self.extra.iter().zip(&keep).filter(|(_, k)| **k).map(|(x, _)| *x).collect();
                self.settle_radii();
            }
        }
    }

    fn motion(&mut self, dt: f64, cfg: &Config, env: &Env, partner: Option<(f64, f64)>,
              rng: &mut Rng) {
        let kind = self.kind();
        // Steering wanders as a noisy turn rate that keeps relaxing to
        // straight, which gives a path curves rather than kinks.
        self.turn += (-self.turn / 1.5) * dt + kind.wander * dt.sqrt() * rng.gauss();
        let lim = if self.kind == SWIMMER { 1.4 } else { 2.5 };
        self.turn = self.turn.clamp(-lim, lim);
        self.heading += self.turn * dt;

        // Motes glide, then dash.
        let mut speed = self.speed * env.scale * cfg.speed;
        if kind.dash != 0.0 {
            if self.dash > 0.0 {
                self.dash -= dt;
                speed *= kind.dash;
            } else {
                self.glide -= dt;
                if self.glide <= 0.0 {
                    self.dash = rng.uniform(0.4, 0.9);
                    self.glide = rng.uniform(1.5, 4.0);
                    self.turn += rng.uniform(-2.0, 2.0);
                }
            }
        }
        match self.state {
            State::Dying => {
                let u = (self.since / DEATH_SECS).min(1.0);
                speed *= (1.0 - u).powi(2);
            }
            State::Born => speed *= 0.4,
            State::Alive => {}
        }
        // Age slows a body a little, and a heavy one is slower than a light
        // one of its kind: the merged giant lumbers.
        speed *= 1.0 - 0.3 * self.life();
        let nominal = self.nominal_mass(env.scale, cfg);
        speed *= (nominal / self.mass.max(nominal * 0.25)).powf(0.25);

        // Steer back from the edges long before reaching them.
        let r = self.radius();
        let margin = 0.06 * env.w.min(env.h) + r;
        let (cx, cy) = (env.w / 2.0, env.h / 2.0);
        let mut push: f64 = 0.0;
        for d in [self.x, env.w - self.x, self.y, env.h - self.y] {
            if d < margin {
                push = push.max((margin - d) / margin);
            }
        }
        if push > 0.0 {
            let want = (cy - self.y).atan2(cx - self.x);
            let diff = pymod(want - self.heading + PI, 2.0 * PI) - PI;
            self.heading += diff * (3.0 * push * dt).min(1.0);
        }
        if let Some((px, py)) = partner {
            // Something it may join is close: turn toward it.
            let want = (py - self.y).atan2(px - self.x);
            let diff = pymod(want - self.heading + PI, 2.0 * PI) - PI;
            self.heading += diff * (1.5 * dt).min(1.0);
        }
        let mut aim = self.heading;
        if kind.wiggle != 0.0 {
            aim += kind.wiggle * (self.since * 2.0 * PI * 0.7 + self.phase).sin();
        }
        let ax = aim.cos() * speed;
        let ay = aim.sin() * speed;
        // Momentum: velocity chases the aim rather than being it.
        let k = 1.0 - (-dt * kind.momentum).exp();
        self.vx += (ax + self.fx - self.vx) * k;
        self.vy += (ay + self.fy - self.vy) * k;
        self.x += self.vx * dt;
        self.y += self.vy * dt;
        self.x = (1.0 + r * 0.5).max(self.x).min(env.w - 1.0 - r * 0.5);
        self.y = (1.0 + r * 0.5).max(self.y).min(env.h - 1.0 - r * 0.5);
        let (lx, ly) = *self.trail.back().unwrap();
        if (self.x - lx).hypot(self.y - ly) >= TRAIL_STEP {
            if self.trail.len() == TRAIL_LEN {
                self.trail.pop_front();
            }
            self.trail.push_back((self.x, self.y));
        }
    }

    /// Follow the grid to a new size.
    pub fn rescale(&mut self, fx: f64, fy: f64, fm: f64) {
        self.x *= fx;
        self.y *= fy;
        self.vx *= fx;
        self.vy *= fy;
        for v in &mut self.nx {
            *v *= fx;
        }
        for v in &mut self.ny {
            *v *= fy;
        }
        for v in &mut self.nr {
            *v *= fm;
        }
        self.mass *= fm * fm;
        self.target_mass *= fm * fm;
        self.mass_at_death *= fm * fm;
        for p in &mut self.trail {
            p.0 *= fx;
            p.1 *= fy;
        }
        self.settle_radii();
    }
}

// ---------------------------------------------------------------- world

/// One node's share of the field, worked out once a frame: its centre,
/// radius, colour and the box of pixels it reaches.
struct Node {
    cx: f64,
    cy: f64,
    r: f64,
    c: [f64; 3],
    x0: usize,
    x1: usize,
    y0: usize,
    y1: usize,
}

/// The population, and the grid it is painted onto.
///
/// `frame(cols, rows)` is the whole public surface: an area in, a frame of
/// half blocks out. The clock is read on each call, so the frame rate is a
/// cap and never a speed. `advance(dt)` steps without painting, and
/// `paint()` paints without stepping, which is what the golden test holds
/// to Python.
pub struct World {
    pub cfg: Config,
    pub rng: Rng,
    pub w: usize,
    pub h: usize,
    pub scale: f64,
    pub orgs: Vec<Organism>,
    pub t: f64,
    pub last: Option<Instant>,
    pub births: u64,
    pub deaths: u64,
    pub splits: u64,
    pub merges: u64,
    spawn_due: f64,
    next_id: u64,
}

impl World {
    pub fn new(cfg: Config) -> World {
        let rng = match cfg.seed {
            Some(s) => Rng::new(s),
            None => Rng::from_clock(),
        };
        World {
            cfg,
            rng,
            w: 0,
            h: 0,
            scale: 1.0,
            orgs: Vec::new(),
            t: 0.0,
            last: None,
            births: 0,
            deaths: 0,
            splits: 0,
            merges: 0,
            spawn_due: 0.0,
            next_id: 1,
        }
    }

    /// Organisms wanted on this grid: the asked-for count at the nominal
    /// grid, scaled for this one, and never fewer than two.
    pub fn target(&self) -> f64 {
        let ratio = ((self.w * self.h) as f64).sqrt() / nominal_scale();
        (self.cfg.population * ratio.powf(COUNT_EXP)).max(2.0)
    }

    fn env(&self, count: usize) -> Env {
        Env {
            w: self.w as f64,
            h: self.h as f64,
            scale: self.scale,
            count,
            target: self.target(),
        }
    }

    fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    fn new_hue(&mut self, kind: usize) -> f64 {
        if let Some(h) = self.cfg.hue {
            let s = self.cfg.spread;
            return h + self.rng.uniform(-s, s);
        }
        let (a, b) = KINDS[kind].hues;
        pymod(self.rng.uniform(a, b), 360.0)
    }

    fn pick_kind(&mut self) -> usize {
        let kinds: Vec<usize> = self.cfg.kinds.iter().filter_map(|k| kind_index(k)).collect();
        let total: f64 = kinds.iter().map(|&k| KINDS[k].weight).sum();
        let mut r = self.rng.random() * total;
        for &k in &kinds {
            r -= KINDS[k].weight;
            if r <= 0.0 {
                return k;
            }
        }
        *kinds.last().unwrap_or(&DRIFTER)
    }

    /// A new organism, of `kind` or one drawn by weight, at `at` or the
    /// emptiest of a few places; `born` is a body gathering itself, else
    /// one already grown and part way through its life.
    pub fn spawn(&mut self, kind: Option<usize>, at: Option<(f64, f64)>, born: bool) -> usize {
        let kind = kind.unwrap_or_else(|| self.pick_kind());
        let k = &KINDS[kind];
        let size = self.rng.uniform(k.size.0, k.size.1);
        let r = size * self.scale * self.cfg.size;
        let (w, h) = (self.w as f64, self.h as f64);
        let at = at.unwrap_or_else(|| {
            // The emptiest of a few tries, so a birth is not on top of
            // someone.
            let (mut best, mut score) = ((w / 2.0, h / 2.0), -1.0);
            for _ in 0..6 {
                let x = if w > r * 4.0 + 2.0 {
                    self.rng.uniform(r * 2.0 + 1.0, w - r * 2.0 - 1.0)
                } else {
                    w / 2.0
                };
                let y = if h > r * 4.0 + 2.0 {
                    self.rng.uniform(r * 2.0 + 1.0, h - r * 2.0 - 1.0)
                } else {
                    h / 2.0
                };
                let d = self
                    .orgs
                    .iter()
                    .filter(|o| !o.dead)
                    .map(|o| (o.x - x).hypot(o.y - y))
                    .fold(1e9, f64::min);
                if d > score {
                    best = (x, y);
                    score = d;
                }
            }
            best
        });
        let mass = r * r * if born { 1.0 } else { self.rng.uniform(0.6, 1.1) };
        let hue = self.new_hue(kind);
        let env = self.env(self.orgs.len());
        let id = self.id();
        let mut o = Organism::new(&mut self.rng, &self.cfg, &env, id, kind, at.0, at.1, mass,
                                  hue, 0.0, None, born);
        o.size = size;
        if !born {
            o.age = self.rng.uniform(0.0, 0.5) * o.lifespan;
        }
        self.orgs.push(o);
        self.births += 1;
        self.orgs.len() - 1
    }

    /// Halve a body along a line through it; the halves push apart.
    fn split(&mut self, idx: usize) {
        let o = self.orgs[idx].clone();
        let nh: Vec<f64> = o.tint.iter().map(|t| o.hue + t).collect();
        let ang = o.heading + PI / 2.0 + self.rng.uniform(-0.5, 0.5);
        let (ux, uy) = (ang.cos(), ang.sin());
        type Side = (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>);
        let (a, b, order): (Side, Side, bool) = if o.kind == SWIMMER {
            let cut = (o.nr.len() / 2).max(1);
            (
                (o.nx[..cut].to_vec(), o.ny[..cut].to_vec(), o.nr[..cut].to_vec(),
                 nh[..cut].to_vec()),
                (o.nx[cut..].to_vec(), o.ny[cut..].to_vec(), o.nr[cut..].to_vec(),
                 nh[cut..].to_vec()),
                true,
            )
        } else {
            // The core cannot be shared, so it becomes two half-cores a
            // little apart along the cut, and the satellites go with
            // whichever side they are on.
            let mut big = 0;
            for (i, r) in o.nr.iter().enumerate() {
                if *r > o.nr[big] {
                    big = i;
                }
            }
            let r2 = o.nr[big] / 2f64.sqrt();
            let off = o.nr[big] * 0.35;
            let mut px = o.nx.clone();
            let mut py = o.ny.clone();
            let mut pr = o.nr.clone();
            let mut ph = nh.clone();
            px.extend([o.nx[big] + ux * off, o.nx[big] - ux * off]);
            py.extend([o.ny[big] + uy * off, o.ny[big] - uy * off]);
            pr.extend([r2, r2]);
            ph.extend([nh[big], nh[big]]);
            pr[big] = 0.0;
            let m = pr.len();
            let mut side: Vec<bool> =
                (0..m).map(|i| (px[i] - o.x) * ux + (py[i] - o.y) * uy >= 0.0).collect();
            side[m - 2] = true;
            side[m - 1] = false;
            let take = |want: bool| -> Side {
                let sel: Vec<usize> = (0..m).filter(|&i| side[i] == want && pr[i] > 0.0).collect();
                (
                    sel.iter().map(|&i| px[i]).collect(),
                    sel.iter().map(|&i| py[i]).collect(),
                    sel.iter().map(|&i| pr[i]).collect(),
                    sel.iter().map(|&i| ph[i]).collect(),
                )
            };
            (take(true), take(false), false)
        };
        if a.2.is_empty() || b.2.is_empty() {
            return;
        }
        let age = o.age + SPLIT_COST * o.lifespan;
        let env = self.env(self.orgs.len());
        let mut kids = Vec::new();
        for nodes in [&a, &b] {
            let w2: Vec<f64> = nodes.2.iter().map(|r| r * r).collect();
            let tot = np_sum(&w2);
            let wx: Vec<f64> = nodes.0.iter().zip(&w2).map(|(x, w)| x * w).collect();
            let wy: Vec<f64> = nodes.1.iter().zip(&w2).map(|(y, w)| y * w).collect();
            let (cx, cy) = (np_sum(&wx) / tot, np_sum(&wy) / tot);
            let (hx, hy) = if order { (nodes.0[0], nodes.1[0]) } else { (cx, cy) };
            let hue = o.hue + self.rng.uniform(-14.0, 14.0);
            let id = self.id();
            let mut c = Organism::new(&mut self.rng, &self.cfg, &env, id, o.kind, hx, hy, 1e-3,
                                      hue, age, Some(o.lifespan), false);
            c.adopt(nodes.0.clone(), nodes.1.clone(), nodes.2.clone(), nodes.3.clone(), order);
            c.size = o.size;
            c.speed = o.speed * self.rng.uniform(0.85, 1.15);
            c.heading = o.heading;
            c.vx = o.vx;
            c.vy = o.vy;
            c.no_merge = SPLIT_REFRACTORY;
            c.no_split = SPLIT_REFRACTORY;
            // Halves part under their own momentum, not the crowd's shove,
            // so the neck between them has time to be seen.
            c.soft = 4.0;
            kids.push(c);
        }
        let mut back = kids.pop().unwrap();
        let mut front = kids.pop().unwrap();
        if order {
            front.trail = o.trail.clone();
            // The back half's nodes already lie along the path it will
            // follow; it swims on and then veers off.
            let n = b.0.len();
            back.trail = (0..n).rev().map(|i| (b.0[i], b.1[i])).collect();
            while back.trail.len() > TRAIL_LEN {
                back.trail.pop_front();
            }
            back.turn += self.rng.sign() * self.rng.uniform(1.5, 2.5);
        } else {
            let kick = 0.025 * self.scale * self.cfg.speed;
            front.vx += ux * kick;
            front.vy += uy * kick;
            back.vx -= ux * kick;
            back.vy -= uy * kick;
            front.heading = ang;
            back.heading = ang + PI;
        }
        self.orgs.remove(idx);
        self.orgs.push(front);
        self.orgs.push(back);
        self.splits += 1;
    }

    /// The lighter body flows into the heavier one; returns the index kept
    /// and the index taken.
    fn merge(&mut self, i: usize, j: usize) -> (usize, usize) {
        let (ka, kb) = if self.orgs[j].mass > self.orgs[i].mass { (j, i) } else { (i, j) };
        let b = self.orgs[kb].clone();
        let appetite = self.rng.uniform(APPETITE.0, APPETITE.1);
        let a = &mut self.orgs[ka];
        let tot = a.mass + b.mass;
        let (wa, wb) = (a.mass / tot, b.mass / tot);
        let mut hues: Vec<f64> = a.tint.iter().map(|t| a.hue + t).collect();
        hues.extend(b.tint.iter().map(|t| b.hue + t));
        // Hue mixed on the circle, weighted by mass.
        let (ha, hb) = (a.hue.to_radians(), b.hue.to_radians());
        let mx = wa * ha.cos() + wb * hb.cos();
        let my = wa * ha.sin() + wb * hb.sin();
        a.hue = pymod(my.atan2(mx).to_degrees(), 360.0);
        a.age = (wa * a.age + wb * b.age - MERGE_GAIN * a.lifespan).max(0.0);
        a.lifespan = wa * a.lifespan + wb * b.lifespan;
        a.vx = wa * a.vx + wb * b.vx;
        a.vy = wa * a.vy + wb * b.vy;
        let cat = |p: &Vec<f64>, q: &Vec<f64>| -> Vec<f64> { p.iter().chain(q).copied().collect() };
        let (nx, ny, nr) = (cat(&a.nx, &b.nx), cat(&a.ny, &b.ny), cat(&a.nr, &b.nr));
        let order = a.kind == SWIMMER;
        a.adopt(nx, ny, nr, hues, order);
        a.no_split = MERGE_REFRACTORY;
        a.no_merge = appetite;
        self.merges += 1;
        (ka, kb)
    }

    /// Pairs: join what may merge and is touching, draw together what may
    /// merge and is near, and push apart what may not.
    fn interact(&mut self) {
        let cfg = self.cfg.clone();
        for o in &mut self.orgs {
            o.fx = 0.0;
            o.fy = 0.0;
            o.partner = None;
        }
        let n = self.orgs.len();
        let mut gone = vec![false; n];
        let biggest = KINDS.iter().map(|k| k.size.1).fold(f64::MIN, f64::max)
            * self.scale * cfg.size;
        let cap = 2.0 * biggest * biggest;
        for i in 0..n {
            if gone[i] {
                continue;
            }
            for j in i + 1..n {
                if gone[j] {
                    continue;
                }
                let (a, b) = (&self.orgs[i], &self.orgs[j]);
                let (dx, dy) = (b.x - a.x, b.y - a.y);
                let d = dx.hypot(dy) + 1e-6;
                let (ra, rb) = (a.radius(), b.radius());
                let reach = ra + rb;
                if d > 2.2 * reach {
                    continue;
                }
                let can = cfg.merge && a.alive() && b.alive() && a.no_merge <= 0.0
                    && b.no_merge <= 0.0 && a.mass + b.mass <= cap;
                if can && d < 0.6 * reach {
                    let (_, taken) = self.merge(i, j);
                    gone[taken] = true;
                    if taken == i {
                        break;
                    }
                    continue;
                }
                if can {
                    let (bid, bx, by) = (b.id, b.x, b.y);
                    let (aid, ax, ay) = (a.id, a.x, a.y);
                    if self.orgs[i].partner.is_none() {
                        self.orgs[i].partner = Some((bid, bx, by));
                    }
                    if self.orgs[j].partner.is_none() {
                        self.orgs[j].partner = Some((aid, ax, ay));
                    }
                    continue;
                }
                if d < 1.5 * reach && a.soft <= 0.0 && b.soft <= 0.0 {
                    // Bodies that will not join should not look as if they
                    // had: keep their fields apart, and let neither try
                    // again for a while.
                    let (ux, uy) = (dx / d, dy / d);
                    let g = 0.12 * self.scale * cfg.speed * (1.5 * reach - d) / (1.5 * reach);
                    {
                        let a = &mut self.orgs[i];
                        a.fx -= ux * g * (rb / reach);
                        a.fy -= uy * g * (rb / reach);
                    }
                    {
                        let b = &mut self.orgs[j];
                        b.fx += ux * g * (ra / reach);
                        b.fy += uy * g * (ra / reach);
                    }
                    if d < reach {
                        for k in [i, j] {
                            if self.orgs[k].no_merge <= 0.0 {
                                self.orgs[k].no_merge = self.rng.uniform(APPETITE.0, APPETITE.1);
                            }
                        }
                    }
                }
            }
        }
        let mut k = 0;
        self.orgs.retain(|_| {
            k += 1;
            !gone[k - 1]
        });
    }

    /// Step the population by `dt` seconds, in small substeps.
    pub fn advance(&mut self, mut dt: f64) {
        while dt > 0.0 {
            let h = dt.min(1.0 / 30.0);
            dt -= h;
            self.step(h);
        }
    }

    fn step(&mut self, dt: f64) {
        self.t += dt;
        self.interact();
        let mut count = self.orgs.len();
        for i in 0..self.orgs.len() {
            let partner = self.orgs[i].partner.map(|(id, x, y)| {
                self.orgs.iter().find(|p| p.id == id).map_or((x, y), |p| (p.x, p.y))
            });
            let env = self.env(count);
            let cfg = &self.cfg;
            let o = &mut self.orgs[i];
            o.step(dt, self.t, cfg, &env, partner, &mut self.rng);
            if o.state == State::Dying && o.since >= DEATH_SECS {
                o.dead = true;
                count -= 1;
                self.deaths += 1;
            }
        }
        self.orgs.retain(|o| !o.dead);
        // Splits: a body over its size, settled, and not too old.
        if self.cfg.split {
            let ids: Vec<u64> = self.orgs.iter().map(|o| o.id).collect();
            for id in ids {
                let Some(i) = self.orgs.iter().position(|o| o.id == id) else { continue };
                let o = &self.orgs[i];
                let nominal = o.nominal_mass(self.scale, &self.cfg);
                if o.kind().splits && o.alive() && o.no_split <= 0.0 && o.mass >= 1.3 * nominal
                    && o.life() < 0.85
                {
                    let rate = 1.0 / 30.0 * (o.mass / nominal).powi(2);
                    if self.rng.random() < rate * dt {
                        self.split(i);
                    }
                }
            }
        }
        // Births: often while the wall is empty, never once it is full.
        if self.cfg.birth {
            let want = self.target();
            let n = self.orgs.len();
            let mut rate = 0.4 * (1.0 - n as f64 / want).max(0.0);
            if n < 2 {
                rate = rate.max(0.5);
            }
            self.spawn_due -= dt;
            if rate <= 0.0 {
                self.spawn_due = 0.0;
            } else if self.spawn_due <= 0.0 {
                self.spawn(None, None, true);
                self.spawn_due = self.rng.expovariate(rate);
            }
        }
    }

    /// Follow the grid to `w` x `h` pixels: every organism rescaled with
    /// it, and a population from the first frame where there is none.
    pub fn resize(&mut self, w: usize, h: usize) {
        if (w, h) == (self.w, self.h) {
            return;
        }
        let ns = nominal_scale();
        let scale = ns * (((w * h) as f64).sqrt() / ns).powf(SIZE_EXP);
        if self.w > 0 && self.h > 0 {
            let fx = w as f64 / self.w as f64;
            let fy = h as f64 / self.h as f64;
            let fm = scale / self.scale;
            for o in &mut self.orgs {
                o.rescale(fx, fy, fm);
            }
        }
        self.w = w;
        self.h = h;
        self.scale = scale;
        if self.orgs.is_empty() {
            // A wall should show a population from its first frame, not a
            // slow fill from nothing.
            let n = self.target().round_ties_even() as usize;
            for _ in 0..n {
                self.spawn(None, None, false);
            }
            self.births = 0;
        }
    }

    /// The frame for `cols` x `rows` cells, the population stepped on by
    /// the time since the last.
    pub fn frame(&mut self, cols: usize, rows: usize) -> Frame {
        let (cols, rows) = (cols.max(1), rows.max(1));
        self.resize(cols, rows * 2);
        let now = Instant::now();
        let dt = match self.last {
            Some(l) => now.duration_since(l).as_secs_f64().min(0.25),
            None => 0.0,
        };
        self.last = Some(now);
        if dt > 0.0 {
            self.advance(dt);
        }
        self.paint()
    }

    pub fn total_mass(&self) -> f64 {
        self.orgs.iter().map(|o| o.mass).sum()
    }

    fn nodes(&self) -> Vec<Node> {
        let (w, h) = (self.w, self.h);
        let mut out = Vec::new();
        for o in &self.orgs {
            let cols = o.colours();
            for k in 0..o.nr.len() {
                let r = o.nr[k];
                if r < 0.15 {
                    continue;
                }
                let (cx, cy) = (o.nx[k], o.ny[k]);
                let reach = SUPPORT * r;
                let x0 = ((cx - reach) as i64).max(0) as usize;
                let y0 = ((cy - reach) as i64).max(0) as usize;
                let x1 = ((cx + reach) as i64 + 2).clamp(0, w as i64) as usize;
                let y1 = ((cy + reach) as i64 + 2).clamp(0, h as i64) as usize;
                if x0 >= x1 || y0 >= y1 {
                    continue;
                }
                out.push(Node { cx, cy, r, c: cols[k], x0, x1, y0, y1 });
            }
        }
        out
    }

    /// The population as it stands, painted: two pixels a cell, top and
    /// bottom, as `World.arrays` in `life.py` chooses them. Black is the
    /// receiver's own background; a cell with one black half is a half
    /// block in one colour, and matching halves are a space on that colour.
    pub fn paint(&self) -> Frame {
        let (cols, rows) = (self.w, self.h / 2);
        let n = cols * rows;
        let mut ch = vec![0x20u32; n];
        let mut fg = vec![-1i32; n];
        let mut bg = vec![-1i32; n];
        let nodes = self.nodes();
        let aura = self.cfg.aura;
        ch.par_chunks_mut(cols)
            .zip(fg.par_chunks_mut(cols))
            .zip(bg.par_chunks_mut(cols))
            .enumerate()
            .for_each_init(
                || (vec![0.0f64; cols], vec![0.0f64; cols * 3], vec![0i32; cols]),
                |(f, a, top), (row, ((ch, fg), bg))| {
                    pixel_row(&nodes, row * 2, f, a, aura, top);
                    let mut bot = vec![0i32; cols];
                    pixel_row(&nodes, row * 2 + 1, f, a, aura, &mut bot);
                    for x in 0..cols {
                        let (t, b) = (top[x], bot[x]);
                        let (c, g, k) = if t == b {
                            (0x20, -1, if t == 0 { -1 } else { b })
                        } else if t == 0 {
                            (0x2584, b, -1)
                        } else {
                            (0x2580, t, if b == 0 { -1 } else { b })
                        };
                        ch[x] = c;
                        fg[x] = g;
                        bg[x] = k;
                    }
                },
            );
        Frame { cols, rows, ch, fg, bg, attrs: vec![0; n] }
    }
}

/// One row of pixels, `y`, as 0xRRGGBB: the summed field and the
/// field-weighted colour of every node reaching it, shaded by strength and
/// quantised to `STEP`, in exactly numpy's arithmetic (`life.py`, `field`
/// and `pixels`).
fn pixel_row(nodes: &[Node], y: usize, f: &mut [f64], a: &mut [f64], aura: bool,
             out: &mut [i32]) {
    f.fill(0.0);
    a.fill(0.0);
    let yf = y as f64;
    // Where the field is past SUPPORT radii it is below zero and adds
    // nothing; the span skips those pixels with a pixel to spare, and the
    // clamp below keeps the edges exact.
    let reach2 = SUPPORT * SUPPORT;
    for nd in nodes {
        if y < nd.y0 || y >= nd.y1 {
            continue;
        }
        let dy = (yf - nd.cy) / nd.r;
        let dy2 = dy * dy;
        if dy2 >= reach2 {
            continue;
        }
        let half = (reach2 - dy2).sqrt() * nd.r + 1.0;
        let x0 = nd.x0.max((nd.cx - half).max(0.0) as usize);
        let x1 = nd.x1.min((nd.cx + half + 1.0).max(0.0) as usize);
        let [c0, c1, c2] = nd.c;
        for x in x0..x1 {
            let dx = (x as f64 - nd.cx) / nd.r;
            let v = 1.0 / (dx * dx + dy2 + CORE2) - INV_FAR;
            if v > 0.0 {
                f[x] += v;
                let ax = &mut a[x * 3..x * 3 + 3];
                ax[0] += v * c0;
                ax[1] += v * c1;
                ax[2] += v * c2;
            }
        }
    }
    for x in 0..out.len() {
        let fx = f[x];
        let u = fx / THRESHOLD;
        let mut b = if u >= 1.0 { 0.45 + 0.55 * ((u - 1.0) / 1.6).clamp(0.0, 1.0) } else { 0.0 };
        if aura && u >= 0.55 && u < 1.0 {
            let s = (u - 0.55) / 0.45;
            b = 0.22 * (s * s);
        }
        let den = fx.max(1e-9);
        let w = ((u - 3.0) / 5.0).clamp(0.0, 0.5);
        let mut px = 0i32;
        for ch in 0..3 {
            let mut rgb = a[x * 3 + ch] / den;
            rgb *= b;
            rgb = rgb * (1.0 - w) + w;
            let q = (rgb.clamp(0.0, 1.0) * 255.0 / STEP as f64) as i64 * STEP;
            px = (px << 8) | (q as u8 as i32);
        }
        out[x] = px;
    }
}

// ---------------------------------------------------------------- tests

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn golden() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/golden")
    }

    /// A world holding the state `tests/golden.py` wrote, and the size it
    /// is to be painted at.
    fn load(name: &str) -> (World, usize, usize) {
        let text = std::fs::read_to_string(golden().join(format!("{name}.state"))).unwrap();
        let mut lines = text.lines();
        let head: Vec<&str> = lines.next().unwrap().split(' ').collect();
        let mut cfg = Config::default();
        cfg.aura = head[4] == "1";
        cfg.seed = Some(1);
        let mut w = World::new(cfg);
        w.w = head[1].parse().unwrap();
        w.h = head[2].parse().unwrap();
        w.scale = head[3].parse().unwrap();
        let (cols, rows) = (head[5].parse().unwrap(), head[6].parse().unwrap());
        let count: usize = lines.next().unwrap().split(' ').nth(1).unwrap().parse().unwrap();
        let floats = |l: &str| -> Vec<f64> { l.split(' ').map(|v| v.parse().unwrap()).collect() };
        for _ in 0..count {
            let p: Vec<&str> = lines.next().unwrap().split(' ').collect();
            let kind = kind_index(p[1]).unwrap();
            let v: Vec<f64> = p[3..14].iter().map(|v| v.parse().unwrap()).collect();
            let env = w.env(0);
            let id = w.id();
            let mut o = Organism::new(&mut w.rng, &w.cfg, &env, id, kind, v[4], v[5], v[8],
                                      v[3], v[1], Some(v[2]), false);
            o.state = State::parse(p[2]).unwrap();
            o.since = v[0];
            o.vx = v[6];
            o.vy = v[7];
            o.target_mass = v[9];
            o.mass_at_death = v[10];
            o.nx = floats(lines.next().unwrap());
            o.ny = floats(lines.next().unwrap());
            o.nr = floats(lines.next().unwrap());
            o.tint = floats(lines.next().unwrap());
            o.extra = lines.next().unwrap().split(' ').map(|v| v == "1").collect();
            w.orgs.push(o);
        }
        (w, cols, rows)
    }

    fn arrays(name: &str) -> (Vec<u32>, Vec<i32>, Vec<i32>) {
        let raw = std::fs::read(golden().join(format!("{name}.arrays"))).unwrap();
        let v: Vec<i32> =
            raw.chunks(4).map(|c| i32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let n = v.len() / 3;
        (v[..n].iter().map(|&c| c as u32).collect(), v[n..2 * n].to_vec(), v[2 * n..].to_vec())
    }

    #[test]
    fn paints_what_python_paints() {
        for name in ["small30", "small90", "resized", "noaura", "large", "brief"] {
            let (mut w, cols, rows) = load(name);
            w.resize(cols, rows * 2);
            let f = w.paint();
            let (ch, fg, bg) = arrays(name);
            assert_eq!((f.cols, f.rows), (cols, rows), "{name}");
            let differ = (0..ch.len())
                .filter(|&i| f.ch[i] != ch[i] || f.fg[i] != fg[i] || f.bg[i] != bg[i])
                .count();
            let lit = (0..ch.len()).filter(|&i| ch[i] != 0x20 || bg[i] != -1).count();
            eprintln!("{name}: {cols}x{rows}, {lit} lit, {differ} differ");
            assert_eq!(differ, 0, "{name}: {differ} cells differ from Python");
        }
    }

    #[test]
    fn lives() {
        let mut cfg = Config::default();
        cfg.seed = Some(9);
        let mut w = World::new(cfg);
        w.resize(150, 90);
        let start = w.orgs.len();
        w.advance(600.0);
        assert!(w.births > 0 && w.deaths > 0, "births {} deaths {}", w.births, w.deaths);
        assert!(w.splits + w.merges > 0);
        assert!(!w.orgs.is_empty() && w.orgs.len() < 40, "{} from {start}", w.orgs.len());
        for o in &w.orgs {
            assert!(o.nr.iter().all(|r| r.is_finite()) && o.mass.is_finite());
        }
        let f = w.paint();
        assert!(f.bg.iter().any(|&c| c != -1) || f.ch.iter().any(|&c| c != 0x20));
    }

    #[test]
    fn clamps() {
        let mut c = Config::default();
        c.population = 99.0;
        c.speed = f64::NAN;
        c.hue = Some(-30.0);
        c.kinds = vec!["mote".into(), "squid".into()];
        let notes = c.clamp();
        assert_eq!(c.population, 60.0);
        assert_eq!(c.speed, 1.0);
        assert_eq!(c.hue, Some(330.0));
        assert_eq!(c.kinds, vec!["mote".to_string()]);
        assert_eq!(notes.len(), 3);
    }

    /// Time a frame's painting at 1706x480 cells; `cargo test --release
    /// -- --ignored --nocapture timing`.
    #[test]
    #[ignore]
    fn timing() {
        let mut cfg = Config::default();
        cfg.seed = Some(5);
        let mut w = World::new(cfg);
        w.resize(1706, 960);
        w.advance(4.0);
        w.paint();
        let n = 20;
        let t0 = Instant::now();
        for _ in 0..n {
            std::hint::black_box(w.paint());
        }
        let ms = t0.elapsed().as_secs_f64() / n as f64 * 1000.0;
        eprintln!("paint at 1706x480: {ms:.2} ms a frame, {} organisms", w.orgs.len());
    }
}
