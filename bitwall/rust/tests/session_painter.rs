//! Sessions held to a real painter: one started here, with no screen, in a
//! run directory of its own, so no wall anybody is using is touched.
//! Skipped where the painter has not been built (`make PLATFORM=mac` in
//! `bitwall/receivers/pi`).

use bitwall::session::{apc, session_name, Controller, Link, OpenError, Session, State, Target};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const PAINTER: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../receivers/pi/bitwall-paint");

/// The painter, killed when the test is over however it ends.
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

fn painter() -> Option<Painter> {
    if !Path::new(PAINTER).exists() {
        eprintln!("no painter at {PAINTER}: skipped");
        return None;
    }
    let run = std::env::temp_dir().join(format!("bitwall-rs-session-{}", std::process::id()));
    std::fs::create_dir_all(&run).unwrap();
    let child = Command::new(PAINTER)
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

/// Lines until one starting `verb`, or a panic after five seconds.
fn until(s: &Session, verb: &str) -> Vec<String> {
    let mut got = Vec::new();
    let end = Instant::now() + Duration::from_secs(5);
    loop {
        let left = end.saturating_duration_since(Instant::now());
        let line = s.lines.recv_timeout(left).unwrap_or_else(|_| panic!("no {verb:?} in {got:?}"));
        let done = line.split_whitespace().next() == Some(verb);
        got.push(line);
        if done {
            return got;
        }
    }
}

#[test]
fn sessions_on_a_painter() {
    let Some(p) = painter() else { return };
    let run = p.run.to_str().unwrap().to_string();
    // The host's run directory from BITWALL_RUN, as the Python library
    // takes it; nothing else in this test binary reads the environment.
    unsafe { std::env::set_var("BITWALL_RUN", &run) };
    let here = Target::parse("localhost").unwrap();

    // A cells base, taken.
    let name = session_name("rs-test", None);
    let mut s = Session::open(&here, &name, "layer take=1 type=cells").unwrap();
    assert_eq!(s.state(), State::Open);
    let opening = until(&s, "opened");
    assert_eq!(opening[0], "hello v1 line");
    assert_eq!(opening[1], format!("opened {name} z=0"));

    // A base that connects is told its area and asked to repaint; the
    // report it asked for follows, and ends at its own `area`.
    s.write(&apc("report")).unwrap();
    let mut report = until(&s, "caps");
    report.extend(until(&s, "area"));
    eprintln!("report: {report:?}");

    // A frame, then a command after it on the same `in`.
    s.write(b"\x1b[0m\x1b[H\x1b[38;2;255;200;0mrust\x1b[0m").unwrap();
    s.write(&apc("ping")).unwrap();
    until(&s, "pong");

    // The name is in use while it is open: dropped, not answered.
    match Session::open(&here, &name, "layer take=1 type=cells") {
        Err(OpenError::Dropped(_)) => {}
        other => panic!("in use: {:?}", other.err()),
    }
    // A base held, and no take: refused.
    let other = session_name("rs-test", Some("2"));
    match Session::open(&here, &other, "layer type=cells") {
        Err(OpenError::Refused(why)) => assert!(why.ends_with("busy"), "{why}"),
        x => panic!("busy: {:?}", x.err()),
    }
    // No wall by that name.
    let nowhere = Target::parse("localhost:nowall").unwrap();
    match Session::open(&nowhere, &other, "layer take=1") {
        Err(OpenError::NoWall(_)) => {}
        x => panic!("no wall: {:?}", x.err()),
    }

    // A controller sees the layer among the sessions.
    let mut c = Controller::open(&here, None, Some("rs-test")).unwrap();
    eprintln!("controller {} report: {} lines", c.name, c.report.len());
    assert!(c.report.last().is_some_and(|l| l.starts_with("area ")));
    let sessions = c.ask("sessions", Duration::from_secs(3));
    eprintln!("sessions: {sessions:?}");
    assert!(sessions.iter().any(|l| l.contains(&name)));
    assert!(sessions.iter().any(|l| l.contains(&c.name) && l.contains("instance=rs-test")));

    // The layer leaves, and says so last.
    let t0 = Instant::now();
    s.close(Some(&apc("detach")));
    eprintln!("closed in {:?}", t0.elapsed());
    let sessions = c.ask("sessions", Duration::from_secs(3));
    assert!(!sessions.iter().any(|l| l.contains(&name)), "{sessions:?}");

    // Through a Link given the wall's directory, the same wall again, and
    // the wall's own end of a session seen as `Closed`.
    let link = Link::new(&here, None, Some(&format!("{run}/bitwall/0")));
    let again = session_name("rs-test", Some("3"));
    let mut s = Session::open_on(&link, &again, "layer take=1 type=cells").unwrap();
    s.write(&apc("close")).unwrap();
    until(&s, "closed");
    assert_eq!(s.state(), State::Closed("asked".into()));
    s.close(None);

    c.close();
    drop(p);
}
