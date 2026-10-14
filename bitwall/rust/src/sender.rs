//! What every sender's `main` does around its picture: a log whose lines
//! start with the time, options that are never fatal, and a run that stops
//! on SIGTERM and detaches last (AGENTS.md, *The sender API*).
//!
//! ```no_run
//! use bitwall::sender::{self, Options};
//! use bitwall::log;
//!
//! let args = Options::new("dots", "the smallest thing that draws")
//!     .clamped("font", 18.0, 4.0, 400.0, "font size to ask for")
//!     .switch("grey", false, "draw in grey")
//!     .parse_env();
//! let target = args.target_or_exit();
//! sender::run(&target, sender::period(12.0), |w, _stop| {
//!     w.transition(|w| {
//!         w.set_font_size(args.number("font") as u32).ok();
//!         w.set_source(|cols: usize, rows: usize| Some(bitwall::frame::Frame::new(cols, rows)));
//!     });
//!     log!("dots at font {}", args.number("font"));
//! });
//! ```
//!
//! The options are a wall's, not a terminal's: a wall is a screen in
//! another room with nobody at the keyboard, so a number out of range is
//! clamped, one that is not a number keeps its default, an unknown flag is
//! ignored, and each is said on stderr where the operator will find it. A
//! controller passes them as `--<name> [value]` with the wall last
//! (CONTROL.md), which is the shape parsed here.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use crate::wall::{attach_with, stop_on_signals, Wall};

// ---------------------------------------------------------------- the log

/// The local time, `HH:MM:SS`, that starts every line of a sender's log.
pub fn now() -> String {
    let (h, m, s) = clock();
    format!("{h:02}:{m:02}:{s:02}")
}

#[cfg(unix)]
fn clock() -> (i32, i32, i32) {
    use std::ffi::c_long;
    #[repr(C, align(8))]
    struct Tm([i32; 32]); // tm_sec, tm_min, tm_hour first, on every libc
    unsafe extern "C" {
        fn time(t: *mut c_long) -> c_long;
        fn localtime_r(t: *const c_long, tm: *mut Tm) -> *mut Tm;
    }
    let mut tm = Tm([0; 32]);
    // SAFETY: time_t is a C long on the unixes a sender runs on, and tm is
    // larger than any libc's struct tm.
    unsafe {
        let t = time(std::ptr::null_mut());
        localtime_r(&t, &mut tm);
    }
    (tm.0[2], tm.0[1], tm.0[0])
}

#[cfg(not(unix))]
fn clock() -> (i32, i32, i32) {
    let s = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    (((s / 3600) % 24) as i32, ((s / 60) % 60) as i32, (s % 60) as i32)
}

/// Write one line of the log, the time first, and flush it. Errors are
/// dropped: a sender whose log has gone away still draws.
#[doc(hidden)]
pub fn line(err: bool, text: std::fmt::Arguments) {
    use std::io::Write;
    let stamp = now();
    if err {
        let mut o = std::io::stderr().lock();
        let _ = writeln!(o, "{stamp} {text}");
        let _ = o.flush();
    } else {
        let mut o = std::io::stdout().lock();
        let _ = writeln!(o, "{stamp} {text}");
        let _ = o.flush();
    }
}

/// A line of the sender's log on stdout: `HH:MM:SS <text>`, flushed.
#[macro_export]
macro_rules! log {
    ($($a:tt)*) => { $crate::sender::line(false, format_args!($($a)*)) };
}

/// A line of the sender's log on stderr: `HH:MM:SS <text>`, flushed.
#[macro_export]
macro_rules! warn {
    ($($a:tt)*) => { $crate::sender::line(true, format_args!($($a)*)) };
}

// ------------------------------------------------------------ the options

#[derive(Clone, Debug)]
enum Kind {
    Number { default: Option<f64>, range: Option<(f64, f64)> },
    Text { default: String },
    Choice { choices: Vec<String>, default: String },
    Switch { default: bool },
}

#[derive(Clone, Debug)]
struct Spec {
    name: String,
    kind: Kind,
    help: String,
}

/// A value an option took.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Number(Option<f64>),
    Text(String),
    Switch(bool),
}

/// The options a sender takes, declared once: they parse its arguments
/// and print its `--help`.
#[derive(Clone, Debug)]
pub struct Options {
    prog: String,
    about: String,
    specs: Vec<Spec>,
}

/// What the arguments said: every declared option's value, the wall, and
/// what was not understood.
#[derive(Clone, Debug)]
pub struct Args {
    values: HashMap<String, Value>,
    /// The wall to attach to: the last argument that is not an option.
    pub target: Option<String>,
    /// What was ignored or replaced, a line each, said by `parse_env`.
    pub warnings: Vec<String>,
    /// `-h` or `--help` was given.
    pub help: bool,
    usage: String,
    prog: String,
}

impl Options {
    pub fn new(prog: &str, about: &str) -> Options {
        Options { prog: prog.into(), about: about.into(), specs: Vec::new() }
    }

    fn add(mut self, name: &str, kind: Kind, help: impl Into<String>) -> Options {
        self.specs.retain(|s| s.name != name);
        self.specs.push(Spec { name: name.into(), kind, help: help.into() });
        self
    }

    /// `--name N`, any finite number.
    pub fn number(self, name: &str, default: f64, help: impl Into<String>) -> Options {
        self.add(name, Kind::Number { default: Some(default), range: None }, help)
    }

    /// `--name N`, held to `lo..=hi`.
    pub fn clamped(self, name: &str, default: f64, lo: f64, hi: f64, help: impl Into<String>) -> Options {
        self.add(name, Kind::Number { default: Some(default), range: Some((lo, hi)) }, help)
    }

    /// `--name N`, or nothing when it is not given.
    pub fn maybe(self, name: &str, help: impl Into<String>) -> Options {
        self.add(name, Kind::Number { default: None, range: None }, help)
    }

    /// `--name TEXT`.
    pub fn text(self, name: &str, default: &str, help: impl Into<String>) -> Options {
        self.add(name, Kind::Text { default: default.into() }, help)
    }

    /// `--name ONE`, one of `choices`.
    pub fn choice(self, name: &str, choices: &[&str], default: &str, help: impl Into<String>) -> Options {
        let choices = choices.iter().map(|c| c.to_string()).collect();
        self.add(name, Kind::Choice { choices, default: default.into() }, help)
    }

    /// `--name` and `--no-name`.
    pub fn switch(self, name: &str, default: bool, help: impl Into<String>) -> Options {
        self.add(name, Kind::Switch { default }, help)
    }

    /// The usage line and a line an option, as `--help` prints them.
    pub fn usage(&self) -> String {
        let mut short = format!("usage: {}", self.prog);
        let mut long = String::new();
        for s in &self.specs {
            let (flag, dflt) = match &s.kind {
                Kind::Number { default, range } => {
                    let d = match (default, range) {
                        (Some(d), Some((lo, hi))) => format!("default {d}, {lo}..{hi}"),
                        (Some(d), None) => format!("default {d}"),
                        _ => String::new(),
                    };
                    (format!("--{} N", s.name), d)
                }
                Kind::Text { default } => (format!("--{} TEXT", s.name), format!("default {default}")),
                Kind::Choice { choices, default } => {
                    (format!("--{} {{{}}}", s.name, choices.join("|")), format!("default {default}"))
                }
                Kind::Switch { default: true } => (format!("--no-{}", s.name), "on unless given".into()),
                Kind::Switch { default: false } => (format!("--{}", s.name), "off unless given".into()),
            };
            short += &format!(" [{flag}]");
            let help = match (s.help.is_empty(), dflt.is_empty()) {
                (_, true) => s.help.clone(),
                (true, false) => format!("({dflt})"),
                (false, false) => format!("{} ({dflt})", s.help),
            };
            long += &format!("\n  {flag:<24} {help}");
        }
        short += " wall";
        if self.about.is_empty() {
            format!("{short}\n{long}")
        } else {
            format!("{short}\n\n{}\n{long}", self.about)
        }
    }

    fn defaults(&self) -> HashMap<String, Value> {
        self.specs
            .iter()
            .map(|s| {
                let v = match &s.kind {
                    Kind::Number { default, .. } => Value::Number(*default),
                    Kind::Text { default } | Kind::Choice { default, .. } => Value::Text(default.clone()),
                    Kind::Switch { default } => Value::Switch(*default),
                };
                (s.name.clone(), v)
            })
            .collect()
    }

    /// Parse `argv` (without the program's name). Never fails: what it
    /// cannot use it says in `warnings` and leaves at its default.
    pub fn parse<I: IntoIterator<Item = String>>(&self, argv: I) -> Args {
        let mut values = self.defaults();
        let mut warnings = Vec::new();
        let mut targets: Vec<String> = Vec::new();
        let mut help = false;
        let mut only_targets = false;
        let mut it = argv.into_iter().peekable();
        while let Some(a) = it.next() {
            if only_targets || !a.starts_with('-') || a == "-" {
                targets.push(a);
                continue;
            }
            if a == "--" {
                only_targets = true;
                continue;
            }
            if a == "-h" || a == "--help" {
                help = true;
                continue;
            }
            let (flag, inline) = match a.split_once('=') {
                Some((f, v)) => (f.to_string(), Some(v.to_string())),
                None => (a.clone(), None),
            };
            let name = flag.trim_start_matches('-');
            let (spec, negated) = match self.specs.iter().find(|s| s.name == name) {
                Some(s) => (s, false),
                None => match name.strip_prefix("no-").and_then(|n| self.specs.iter().find(|s| s.name == n)) {
                    Some(s) if matches!(s.kind, Kind::Switch { .. }) => (s, true),
                    _ => {
                        warnings.push(format!("ignoring unknown argument {a:?}"));
                        continue;
                    }
                },
            };
            if !flag.starts_with("--") {
                warnings.push(format!("ignoring unknown argument {a:?}"));
                continue;
            }
            let name = spec.name.clone();
            if let Kind::Switch { default } = spec.kind {
                let on = match inline.as_deref() {
                    None => !negated,
                    Some(v) => match switch_word(v) {
                        Some(b) => b != negated,
                        None => {
                            warnings.push(format!("{flag} {v:?} is not on or off, using {}", on_off(default)));
                            default
                        }
                    },
                };
                values.insert(name, Value::Switch(on));
                continue;
            }
            // A value: after '=', or the next argument unless that is a flag.
            let value = inline.or_else(|| {
                let next = it.peek()?;
                let flag_like = next.starts_with("--") || (next.starts_with('-') && next.parse::<f64>().is_err());
                if flag_like { None } else { it.next() }
            });
            let Some(text) = value else {
                warnings.push(format!("{flag} wants a value, using the default"));
                continue;
            };
            match &spec.kind {
                Kind::Number { default, range } => {
                    let shown = default.map_or("none".to_string(), |d| d.to_string());
                    let v = match text.trim().parse::<f64>() {
                        Ok(v) if v.is_finite() => Some(v),
                        _ => {
                            warnings.push(format!("{flag} {text:?} is not a number, using {shown}"));
                            *default
                        }
                    };
                    let v = match (v, range) {
                        (Some(v), Some((lo, hi))) if v < *lo || v > *hi => {
                            let c = v.clamp(*lo, *hi);
                            warnings.push(format!("{flag} {v} is outside {lo}..{hi}, using {c}"));
                            Some(c)
                        }
                        (v, _) => v,
                    };
                    values.insert(name, Value::Number(v));
                }
                Kind::Text { .. } => {
                    values.insert(name, Value::Text(text));
                }
                Kind::Choice { choices, default } => {
                    if choices.contains(&text) {
                        values.insert(name, Value::Text(text));
                    } else {
                        warnings.push(format!(
                            "{flag} {text:?} is not one of {}, using {default}",
                            choices.join(", ")
                        ));
                    }
                }
                Kind::Switch { .. } => unreachable!(),
            }
        }
        let target = targets.pop();
        for t in targets {
            warnings.push(format!("ignoring unknown argument {t:?}"));
        }
        Args { values, target, warnings, help, usage: self.usage(), prog: self.prog.clone() }
    }

    /// Parse the process's own arguments: say each warning with `warn!`,
    /// and on `--help` print the usage and exit.
    pub fn parse_env(&self) -> Args {
        let args = self.parse(std::env::args().skip(1));
        if args.help {
            println!("{}", args.usage);
            std::process::exit(0);
        }
        for w in &args.warnings {
            crate::warn!("{}: {w}", self.prog);
        }
        args
    }
}

fn switch_word(v: &str) -> Option<bool> {
    match v.to_ascii_lowercase().as_str() {
        "1" | "on" | "yes" | "true" => Some(true),
        "0" | "off" | "no" | "false" => Some(false),
        _ => None,
    }
}

fn on_off(b: bool) -> &'static str {
    if b { "on" } else { "off" }
}

impl Args {
    /// A number option's value. Panics on a name never declared as one.
    pub fn number(&self, name: &str) -> f64 {
        self.maybe(name).unwrap_or_else(|| panic!("--{name} has no value"))
    }

    /// A number option's value, or None where it has none.
    pub fn maybe(&self, name: &str) -> Option<f64> {
        match self.values.get(name) {
            Some(Value::Number(v)) => *v,
            _ => panic!("--{name} is not a number option"),
        }
    }

    /// A text or choice option's value.
    pub fn text(&self, name: &str) -> &str {
        match self.values.get(name) {
            Some(Value::Text(v)) => v,
            _ => panic!("--{name} is not a text option"),
        }
    }

    /// A switch's value.
    pub fn on(&self, name: &str) -> bool {
        match self.values.get(name) {
            Some(Value::Switch(v)) => *v,
            _ => panic!("--{name} is not a switch"),
        }
    }

    /// The wall, or the usage line on stderr and exit 2 when none was given:
    /// the one argument a sender cannot do without.
    pub fn target_or_exit(&self) -> String {
        match &self.target {
            Some(t) => t.clone(),
            None => {
                eprintln!("{}", self.usage.lines().next().unwrap_or(""));
                crate::warn!("{}: no wall given", self.prog);
                std::process::exit(2);
            }
        }
    }
}

// ---------------------------------------------------------------- the run

/// The frame period for `fps` frames a second.
pub fn period(fps: f64) -> Duration {
    Duration::from_secs_f64(1.0 / fps.max(0.001))
}

/// Whether a sender should go on: not told to stop, and still on its wall.
pub fn going(w: &Wall, stop: &AtomicBool) -> bool {
    !stop.load(Ordering::SeqCst) && w.stopped().is_none()
}

/// Wait until the sender is told to stop or leaves its wall for good,
/// logging the area and font it has once the wall talks, each change of
/// area, losing the wall and finding it again, and why it ended.
pub fn watch(w: &Wall, stop: &AtomicBool) {
    let mut had: Option<(usize, usize)> = None;
    loop {
        if stop.load(Ordering::SeqCst) {
            crate::log!("told to stop");
            return;
        }
        if let Some(why) = w.stopped() {
            crate::log!("{why}");
            return;
        }
        let now = w.talking().then(|| w.area());
        if now != had {
            match (had, now) {
                (_, None) => crate::log!("lost the wall; attaching again when it is back"),
                (Some(_), Some((cols, rows))) => crate::log!("resized to {cols}x{rows}"),
                (None, Some((cols, rows))) => match w.font_size() {
                    Some(f) => crate::log!("on the wall, {cols}x{rows} at font {f}"),
                    None => crate::log!("on the wall, {cols}x{rows}"),
                },
            }
            had = now;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// A sender's whole life on one wall: catch SIGINT and SIGTERM, attach to
/// `target` (exiting 1 when it is no target), log it, hand the wall to `body`
/// to set up its picture (or to run its own loop, while `going`), then
/// `watch` until it is time to go, and detach last.
pub fn run<F: FnOnce(&Wall, &AtomicBool)>(target: &str, min_frame: Duration, body: F) {
    let stop = stop_on_signals();
    let w = match attach_with(target, min_frame) {
        Ok(w) => w,
        Err(e) => {
            crate::warn!("{target}: {e}");
            std::process::exit(1);
        }
    };
    crate::log!("attaching to {target}");
    body(&w, &stop);
    watch(&w, &stop);
    w.detach();
    crate::log!("detached");
}
