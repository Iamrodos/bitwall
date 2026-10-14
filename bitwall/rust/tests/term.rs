//! `term`: the depth a terminal is given, and frames to bytes through a
//! writer that keeps what it was given, a write at a time.

use bitwall::frame::{serialise, Depth, Frame, DEFAULT};
use bitwall::sender::Options;
use bitwall::term::{self, depth_from, Painter, CLEAR, ENTER, LEAVE};
use std::io::{self, Write};

/// Every `write_all` kept apart, so a frame is seen to go in one.
#[derive(Default)]
struct Kept(Vec<Vec<u8>>);

impl Write for Kept {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.push(buf.to_vec());
        Ok(buf.len())
    }
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        self.0.push(buf.to_vec());
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn picture(cols: usize, rows: usize, at: usize) -> Frame {
    let mut f = Frame::new(cols, rows);
    for y in 0..rows {
        f.text(0, y, &"·".repeat(cols), 0x444444, DEFAULT, 0);
    }
    if at < cols * rows {
        f.ch[at] = 'X' as u32;
        f.fg[at] = 0xff8800;
    }
    f
}

#[test]
fn depth_from_the_environment() {
    assert_eq!(depth_from(Some("truecolor"), Some("xterm-256color")), Depth::True);
    assert_eq!(depth_from(Some("24bit"), Some("xterm")), Depth::True);
    assert_eq!(depth_from(Some("TrueColor"), None), Depth::True);
    assert_eq!(depth_from(None, Some("xterm-direct")), Depth::True);
    assert_eq!(depth_from(None, Some("xterm-256color")), Depth::Xterm256);
    assert_eq!(depth_from(Some(""), Some("screen-256color")), Depth::Xterm256);
    assert_eq!(depth_from(Some("yes"), Some("tmux-256color")), Depth::Xterm256);
    assert_eq!(depth_from(None, Some("xterm")), Depth::Ansi16);
    assert_eq!(depth_from(None, Some("vt100")), Depth::Ansi16);
    assert_eq!(depth_from(None, None), Depth::Ansi16);
}

#[test]
fn first_frame_is_full_after_a_clear_in_one_write() {
    let mut p = Painter::new(Kept::default(), Depth::True);
    let mut src = |c: usize, r: usize| Some(picture(c, r, 5));
    let n = p.paint(&mut src, 20, 4, false).unwrap();
    let w = &p.out().0;
    assert_eq!(w.len(), 1, "one write a frame");
    assert_eq!(w[0].len(), n);
    let mut want = CLEAR.to_vec();
    want.extend(serialise(&picture(20, 4, 5), None, true, Depth::True));
    assert_eq!(w[0], want);
}

#[test]
fn next_frame_is_the_diff() {
    let mut p = Painter::new(Kept::default(), Depth::Xterm256);
    let mut at = 5;
    let mut src = |c: usize, r: usize| {
        let f = picture(c, r, at);
        at += 1;
        Some(f)
    };
    p.paint(&mut src, 20, 4, false).unwrap();
    p.paint(&mut src, 20, 4, false).unwrap();
    let w = &p.out().0;
    assert_eq!(w.len(), 2);
    let want = serialise(&picture(20, 4, 6), Some(&picture(20, 4, 5)), false, Depth::Xterm256);
    assert_eq!(w[1], want);
    assert!(w[1].len() < w[0].len() / 4, "a diff, not the whole frame");
}

#[test]
fn nothing_changed_writes_nothing() {
    let mut p = Painter::new(Kept::default(), Depth::True);
    let mut src = |c: usize, r: usize| Some(picture(c, r, 0));
    p.paint(&mut src, 10, 3, false).unwrap();
    assert_eq!(p.paint(&mut src, 10, 3, false).unwrap(), 0);
    let mut none = |_: usize, _: usize| None;
    assert_eq!(p.paint(&mut none, 10, 3, false).unwrap(), 0);
    assert_eq!(p.out().0.len(), 1);
}

#[test]
fn resize_or_asked_is_full_again() {
    let mut p = Painter::new(Kept::default(), Depth::True);
    let mut src = |c: usize, r: usize| Some(picture(c, r, 0));
    p.paint(&mut src, 10, 3, false).unwrap();
    p.paint(&mut src, 12, 5, false).unwrap();
    p.paint(&mut src, 12, 5, true).unwrap();
    let w = &p.out().0;
    assert_eq!(w.len(), 3);
    let full = [CLEAR, &serialise(&picture(12, 5, 0), None, true, Depth::True)].concat();
    assert_eq!(w[1], full);
    assert_eq!(w[2], full);
}

#[test]
fn a_source_with_nothing_new_is_repainted_from_the_last_on_resize() {
    let mut p = Painter::new(Kept::default(), Depth::True);
    let mut once = Some(picture(10, 3, 0));
    let mut src = |_: usize, _: usize| once.take();
    p.paint(&mut src, 10, 3, false).unwrap();
    p.paint(&mut src, 8, 2, false).unwrap();
    let w = &p.out().0;
    assert_eq!(w.len(), 2);
    assert_eq!(w[1], [CLEAR, &serialise(&picture(8, 2, 0), None, true, Depth::True)].concat());
}

#[test]
fn a_frame_of_the_wrong_size_is_fitted() {
    let mut p = Painter::new(Kept::default(), Depth::True);
    let mut src = |_: usize, _: usize| Some(picture(30, 10, 0));
    p.paint(&mut src, 10, 3, false).unwrap();
    assert_eq!(p.out().0[0], [CLEAR, &serialise(&picture(10, 3, 0), None, true, Depth::True)].concat());
}

#[test]
fn the_term_flag() {
    let opts = term::option(Options::new("t", ""));
    assert!(term::wanted(&opts.parse(["--term".to_string()])));
    let args = opts.parse(["pi.local".to_string()]);
    assert!(!term::wanted(&args));
    assert_eq!(args.target.as_deref(), Some("pi.local"));
    assert!(opts.usage().contains("--term"));
}

#[test]
fn enter_and_leave_undo_each_other() {
    let pairs: [(&[u8], &[u8]); 3] =
        [(b"\x1b[?1049h", b"\x1b[?1049l"), (b"\x1b[?25l", b"\x1b[?25h"), (b"\x1b[?7l", b"\x1b[?7h")];
    for (on, off) in pairs {
        assert!(ENTER.windows(on.len()).any(|w| w == on));
        assert!(LEAVE.windows(off.len()).any(|w| w == off));
    }
}
