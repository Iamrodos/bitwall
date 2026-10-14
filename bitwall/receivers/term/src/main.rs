//! bitwall-term: a wall on the terminal it runs in, on Windows, a Mac or
//! Linux. The stream is read into a grid rather than copied to the
//! terminal, so the wall can stack cells layers, answer `picture`,
//! `mirror`, `stats` and `measures` from what it holds, and pass the
//! terminal's keys and clicks to the sender as `input`. bitwall-shell and
//! bitwall-ps are the least a receiver can be; this is a terminal done
//! properly.
//!
//!     bitwall-term [--font <n>] [wall]        wall 0 unless named
//!
//! BITWALL_RUN puts the walls elsewhere. The font is the size the area is
//! said to be drawn at, 14 unless given, since a terminal cannot say.
//! Ctrl+C ends it.

mod screen;

use bitwall_receiver::{pipe, proto, sys, wall};

use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers, MouseEventKind};
use std::time::{Duration, Instant};

/// A wall's name: one to thirty-two lower case letters, digits, `_` and
/// `-`, starting with a letter or a digit.
fn wall_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && !s.starts_with(['_', '-'])
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

/// A key as what was pressed, never as bytes, named as the web page names
/// them; None for a modifier alone.
fn key_name(code: KeyCode) -> Option<String> {
    Some(match code {
        KeyCode::Enter => "Return".into(),
        KeyCode::Backspace => "BackSpace".into(),
        KeyCode::Char(' ') => "space".into(),
        KeyCode::Up => "Up".into(),
        KeyCode::Down => "Down".into(),
        KeyCode::Left => "Left".into(),
        KeyCode::Right => "Right".into(),
        KeyCode::Delete => "Delete".into(),
        KeyCode::Esc => "Escape".into(),
        KeyCode::Tab | KeyCode::BackTab => "Tab".into(),
        KeyCode::Home => "Home".into(),
        KeyCode::End => "End".into(),
        KeyCode::PageUp => "PageUp".into(),
        KeyCode::PageDown => "PageDown".into(),
        KeyCode::Insert => "Insert".into(),
        KeyCode::F(n) => format!("F{n}"),
        KeyCode::Char(c) => c.to_string(),
        _ => return None,
    })
}

fn fail(msg: &str) -> ! {
    eprintln!("bitwall-term: {msg}");
    std::process::exit(1);
}

fn main() {
    let mut font: u32 = std::env::var("BITWALL_FONT").ok().and_then(|v| v.parse().ok()).unwrap_or(14);
    let mut name = "0".to_string();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--font" => font = args.next().and_then(|v| v.parse().ok()).unwrap_or_else(|| fail("--font wants a size")),
            "-h" | "--help" => {
                println!("usage: bitwall-term [--font <n>] [wall]");
                return;
            }
            _ if a.starts_with('-') => fail(&format!("{a}: unknown option")),
            _ => name = a,
        }
    }
    if !wall_name(&name) {
        fail(&format!("{name}: not a wall's name"));
    }
    let run = std::env::var("BITWALL_RUN").ok().filter(|v| !v.is_empty()).unwrap_or_else(pipe::run_dir);
    let (cols, rows) = match crossterm::terminal::size() {
        Ok((c, r)) if c > 0 && r > 0 => (c as usize, r as usize),
        _ => fail("no terminal"),
    };
    let kitty = screen::ask_kitty();
    let (dir, door) = pipe::claim(&run, &name).unwrap_or_else(|e| fail(&e));
    let receiver = format!(
        "receiver bitwall-term version={} started={}{}",
        env!("BITWALL_VERSION"),
        sys::unix_now(),
        sys::booted().map(|b| format!(" booted={b}")).unwrap_or_default()
    );
    let mut glass = screen::Screen::new(cols, rows, font, kitty, sys::hostname(), sys::address());
    let mut wall = wall::Wall::new(&name, dir, door, &glass, font, receiver);

    glass.enter();
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = crossterm::terminal::disable_raw_mode();
        print!("\x1b[0m\x1b[?25h\x1b[?1049l");
        default_hook(info);
    }));

    // A size the terminal has held for 300 ms, so a drag is one change.
    let mut pending: Option<((usize, usize), Instant)> = None;
    let mut looked = Instant::now();
    let mut last_draw = Instant::now() - Duration::from_secs(1);
    let mut minute = sys::clock(false);
    'serve: loop {
        pipe::wait(Duration::from_millis(15));
        let now = Instant::now();
        while event::poll(Duration::ZERO).unwrap_or(false) {
            match event::read() {
                Ok(Event::Key(k)) if k.kind != KeyEventKind::Release => {
                    if k.code == KeyCode::Char('c') && k.modifiers.contains(KeyModifiers::CONTROL) {
                        break 'serve;
                    }
                    // Everything belongs to the base once it has asked for
                    // the keyboard; until then the bare keys are the
                    // viewer's, as on the web receiver's page.
                    let bare = !k.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT);
                    if bare && !wall.wants()[1] {
                        match k.code {
                            KeyCode::Char('?') => glass.helping = !glass.helping,
                            KeyCode::Esc if glass.helping => glass.helping = false,
                            KeyCode::Char('s') => wall.toggle_stats(),
                            _ => {}
                        }
                        wall.changed = true;
                        continue;
                    }
                    let Some(key) = key_name(k.code) else { continue };
                    let mut mods = Vec::new();
                    if k.modifiers.contains(KeyModifiers::CONTROL) {
                        mods.push("ctrl");
                    }
                    if k.modifiers.contains(KeyModifiers::ALT) {
                        mods.push("alt");
                    }
                    if (k.modifiers.contains(KeyModifiers::SHIFT) || k.code == KeyCode::BackTab) && key.chars().count() > 1 {
                        mods.push("shift");
                    }
                    let mut opts = format!("key={}", proto::word(&key));
                    if !mods.is_empty() {
                        opts += &format!(" mods={}", mods.join(","));
                    }
                    wall.press("key", &opts);
                }
                Ok(Event::Mouse(m)) => {
                    if let MouseEventKind::Down(_) = m.kind {
                        let (x, y) = (m.column as usize, m.row as usize);
                        if x < wall.cols && y < wall.rows {
                            wall.press("tap", &format!("cell={x},{y} area={},{}", wall.cols, wall.rows));
                        }
                    }
                }
                Ok(Event::Resize(c, r)) => pending = Some(((c as usize, r as usize), now)),
                Err(_) => break 'serve,
                _ => {}
            }
        }
        if now.duration_since(looked) >= Duration::from_millis(250) {
            looked = now;
            if let Ok((c, r)) = crossterm::terminal::size() {
                let size = (c as usize, r as usize);
                if size != (wall.cols, wall.rows) && pending.map(|p| p.0 != size).unwrap_or(true) {
                    pending = Some((size, now));
                }
            }
        }
        if let Some((size, since)) = pending {
            if now.duration_since(since) >= Duration::from_millis(300) {
                pending = None;
                if size != (wall.cols, wall.rows) && size.0 > 0 && size.1 > 0 {
                    glass.resize(size.0, size.1);
                    wall.glass_changed(&mut glass);
                }
            }
        }

        wall.serve(now, &mut glass);
        glass.set_mouse(wall.wants()[0]);

        // Paint between frames, as the painter does; a sender that stops
        // mid-frame is drawn anyway a tenth of a second on.
        let touched = wall.sessions.iter().any(|s| s.grid.touched);
        let clock = sys::clock(false);
        if clock != minute {
            minute = clock;
            wall.changed = true;
        }
        let due = wall.frame_closed
            || wall.changed
            || !wall.kitty_out.is_empty()
            || (touched && now.duration_since(last_draw) >= Duration::from_millis(100));
        if due {
            wall.frame_closed = false;
            wall.changed = false;
            for s in wall.sessions.iter_mut() {
                s.grid.touched = false;
            }
            let ms = glass.draw(&mut wall);
            wall.draw_ms = Some(wall.draw_ms.map(|v| v * 0.8 + ms * 0.2).unwrap_or(ms));
            last_draw = now;
        }
    }
    glass.leave();
    wall.stop();
}
