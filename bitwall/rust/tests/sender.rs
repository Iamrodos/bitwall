//! `bitwall::sender`: options that are never fatal, and the log's time.

use bitwall::sender::{now, Options};

fn opts() -> Options {
    Options::new("t", "a test")
        .clamped("font", 18.0, 4.0, 400.0, "font size")
        .number("speed", 1.0, "")
        .maybe("hue", "a hue")
        .text("kinds", "a,b", "kinds")
        .choice("fps", &["12", "30"], "12", "rate")
        .switch("split", true, "splitting")
        .switch("grey", false, "grey")
}

fn parse(a: &[&str]) -> bitwall::sender::Args {
    opts().parse(a.iter().map(|s| s.to_string()))
}

#[test]
fn defaults() {
    let a = parse(&["pi.local"]);
    assert_eq!(a.number("font"), 18.0);
    assert_eq!(a.maybe("hue"), None);
    assert_eq!(a.text("kinds"), "a,b");
    assert_eq!(a.text("fps"), "12");
    assert!(a.on("split") && !a.on("grey"));
    assert_eq!(a.target.as_deref(), Some("pi.local"));
    assert!(a.warnings.is_empty() && !a.help);
}

#[test]
fn values_spaced_and_inline() {
    let a = parse(&["--font", "20", "--speed=2.5", "--hue", "-30", "--kinds=b", "--fps", "30",
                    "--no-split", "--grey", "pi.local:1"]);
    assert_eq!(a.number("font"), 20.0);
    assert_eq!(a.number("speed"), 2.5);
    assert_eq!(a.maybe("hue"), Some(-30.0));
    assert_eq!(a.text("kinds"), "b");
    assert_eq!(a.text("fps"), "30");
    assert!(!a.on("split") && a.on("grey"));
    assert_eq!(a.target.as_deref(), Some("pi.local:1"));
    assert!(a.warnings.is_empty(), "{:?}", a.warnings);
}

#[test]
fn never_fatal() {
    let a = parse(&["--font", "999", "--speed", "fast", "--fps", "7", "--bogus", "--grey=maybe",
                    "--hue", "--split", "first", "last"]);
    assert_eq!(a.number("font"), 400.0);
    assert_eq!(a.number("speed"), 1.0);
    assert_eq!(a.text("fps"), "12");
    assert!(!a.on("grey"));
    assert_eq!(a.maybe("hue"), None);
    assert!(a.on("split"));
    assert_eq!(a.target.as_deref(), Some("last"));
    let w = a.warnings.join("\n");
    for bit in ["outside 4..400", "\"fast\" is not a number", "not one of 12, 30", "\"--bogus\"",
                "not on or off", "--hue wants a value", "\"first\""] {
        assert!(w.contains(bit), "{bit} not in\n{w}");
    }
    assert_eq!(a.warnings.len(), 7, "{w}");
}

#[test]
fn switch_words_and_no_target() {
    let a = parse(&["--split=off", "--grey=yes"]);
    assert!(!a.on("split") && a.on("grey"));
    assert_eq!(a.target, None);
    let a = parse(&["--no-font", "-x", "w"]);
    assert_eq!(a.warnings.len(), 2, "{:?}", a.warnings);
}

#[test]
fn help_from_declarations() {
    let a = parse(&["--help", "w"]);
    assert!(a.help);
    let u = opts().usage();
    assert!(u.starts_with("usage: t [--font N] [--speed N] [--hue N] [--kinds TEXT] [--fps {12|30}] [--no-split] [--grey] wall"), "{u}");
    assert!(u.contains("font size (default 18, 4..400)"), "{u}");
    assert!(u.contains("a test"), "{u}");
}

#[test]
fn time_of_day() {
    let t = now();
    let b = t.as_bytes();
    assert_eq!(t.len(), 8, "{t}");
    assert!(b[2] == b':' && b[5] == b':', "{t}");
    let h: u32 = t[0..2].parse().unwrap();
    let m: u32 = t[3..5].parse().unwrap();
    assert!(h < 24 && m < 60, "{t}");
}
