//! A controller session on a wall on another host, through ssh: asks only
//! `report` and `sessions`, so it takes nothing from whoever holds the wall.
//! Run by hand, naming the wall:
//!
//!     BITWALL_RS_SSH=pi.local:0 cargo test --test session_ssh -- --ignored --nocapture

use bitwall::session::{Controller, Target};
use std::time::Duration;

#[test]
#[ignore]
fn controller_over_ssh() {
    let target = std::env::var("BITWALL_RS_SSH").expect("BITWALL_RS_SSH=<host:wall>");
    let mut c = Controller::open(&Target::parse(&target).unwrap(), None, None).unwrap();
    assert!(c.report.last().is_some_and(|l| l.starts_with("area ")), "{:?}", c.report);
    let sessions = c.ask("sessions", Duration::from_secs(5));
    eprintln!("{sessions:?}");
    assert!(sessions.iter().any(|l| l.contains(&c.name)));
    c.close();
}
