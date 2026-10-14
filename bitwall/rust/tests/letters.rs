//! `letters` held to what Python's `bitwall.letters` draws for the same
//! calls (`tests/golden/letters.py` makes `letters.json`).

use bitwall::frame::{Depth, Frame};
use bitwall::letters::{self, Align, Cells, Edges, Font, Spacing, Style, Turn, WallInfo};
use serde_json::Value;

fn colour(v: &Value) -> Option<i32> {
    v.as_i64().map(|c| c as i32)
}

fn style(v: &Value) -> Style {
    let font = |name: &str| Font::from_name(name).unwrap_or_else(|| panic!("no font {name}"));
    let pair = |v: &Value| v.as_array().map(|a| (a[0].as_u64().unwrap() as u32, a[1].as_u64().unwrap() as u32));
    Style {
        font: font(v["font"].as_str().unwrap()),
        fg: colour(&v["fg"]).unwrap(),
        bg: colour(&v["bg"]),
        scale: v["scale"].as_u64().unwrap() as u32,
        height: v["height"].as_u64().map(|h| h as usize),
        spacing: if v["spacing"] == "mono" { Spacing::Mono } else { Spacing::Proportional },
        turn: v["turn"].as_str().map(|t| if t == "up" { Turn::Up } else { Turn::Down }),
        align: v["align"].as_str().map(align),
        smooth: v["smooth"].as_bool().unwrap(),
        depth: v["depth"].as_str().map(Depth::from_name),
        cell: v["cell"].as_f64(),
        ghost: colour(&v["ghost"]),
        edges: v["edges"].as_str().map(|e| if e == "cut" { Edges::Cut } else { Edges::Square }),
        px: pair(&v["px"]),
    }
}

fn align(a: &str) -> Align {
    match a {
        "right" => Align::Right,
        "centre" | "center" => Align::Centre,
        _ => Align::Left,
    }
}

fn wall(v: &Value) -> Option<WallInfo> {
    if v.is_null() {
        return None;
    }
    Some(WallInfo {
        glyphs: v["glyphs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r[0].as_u64().unwrap() as u32, r[1].as_u64().unwrap() as u32))
            .collect(),
        depth: Depth::from_name(v["depth"].as_str().unwrap()),
        cell: v["cell"].as_array().map(|a| (a[0].as_u64().unwrap() as u32, a[1].as_u64().unwrap() as u32)),
        font_size: v["font_size"].as_u64().map(|n| n as u32),
        kitty: v["kitty"].as_bool().unwrap(),
    })
}

/// Drawn cells as the golden file has them, a palette and a string a row.
fn cells(v: &Value) -> Cells {
    let pal: Vec<Option<letters::Piece>> = v["pal"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| {
            if p.is_null() {
                return None;
            }
            Some(letters::Piece {
                ch: char::from_u32(p[0].as_u64().unwrap() as u32).unwrap(),
                fg: colour(&p[1]),
                bg: colour(&p[2]),
                faint: p[3].as_bool().unwrap(),
            })
        })
        .collect();
    v["grid"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row.as_str().unwrap().chars().map(|c| pal[c as usize - 0x30]).collect())
        .collect()
}

const BGS: [i32; 4] = [0x000000, 0x203040, -1, 0x806020];

fn frame_of(cols: usize, rows: usize) -> Frame {
    let mut f = Frame::new(cols, rows);
    for y in 0..rows {
        for x in 0..cols {
            let i = y * cols + x;
            f.ch[i] = '.' as u32;
            f.fg[i] = 0x808080;
            f.bg[i] = BGS[(x + 3 * y) % BGS.len()];
        }
    }
    f
}

fn changed(f: &Frame, want: &Value) -> Frame {
    let mut g = frame_of(f.cols, f.rows);
    for c in want.as_array().unwrap() {
        let i = c[0].as_u64().unwrap() as usize;
        g.ch[i] = c[1].as_u64().unwrap() as u32;
        g.fg[i] = c[2].as_i64().unwrap() as i32;
        g.bg[i] = c[3].as_i64().unwrap() as i32;
        g.attrs[i] = c[4].as_u64().unwrap() as u8;
    }
    g
}

fn fitted(got: &Option<letters::Fitted>) -> Value {
    match got {
        None => Value::Null,
        Some(g) => serde_json::json!({
            "text": g.text,
            "font": g.style.font.name(),
            "height": g.style.height,
            "px": g.style.px.map(|(a, b)| vec![a, b]),
            "depth": g.style.depth.map(Depth::name),
            "align": g.style.align.map(|a| match a {
                Align::Left => "left",
                Align::Centre => "centre",
                Align::Right => "right",
            }),
        }),
    }
}

fn show(c: &Cells) -> String {
    c.iter()
        .map(|row| {
            row.iter()
                .map(|p| match p {
                    None => '·',
                    Some(p) if p.ch == ' ' => if p.bg.is_some() { '#' } else { '_' },
                    Some(p) => p.ch,
                })
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn golden() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/letters.json");
    let data = std::fs::read_to_string(path).expect("letters.json: run tests/golden/letters.py");
    let cases: Vec<Value> = serde_json::from_str(&data).unwrap();
    let mut failed: Vec<String> = Vec::new();
    let mut counts = std::collections::BTreeMap::new();
    for (k, c) in cases.iter().enumerate() {
        let op = c["op"].as_str().unwrap();
        *counts.entry(op).or_insert(0) += 1;
        let text = c["text"].as_str().unwrap();
        let what = || format!("#{k} {op} {text:?} {}", c["style"]);
        match op {
            "plain" => {
                let font = Font::from_name(c["font"].as_str().unwrap()).unwrap();
                let got = letters::plain(text, font);
                if got != c["want"].as_str().unwrap() {
                    failed.push(format!("#{k} plain {} {:?}: {:?} != {:?}", font.name(), text.chars().take(40).collect::<String>(),
                        got.chars().take(80).collect::<String>(), c["want"].as_str().unwrap().chars().take(80).collect::<String>()));
                }
            }
            "rows" => {
                let st = style(&c["style"]);
                let w = wall(&c["wall"]);
                let got = letters::rows(text, &st, w.as_ref());
                let want = cells(&c["want"]);
                if got != want {
                    failed.push(format!("{} wall {}\ngot\n{}\nwant\n{}", what(), c["wall"], show(&got), show(&want)));
                }
                let sz = letters::size(text, &st, w.as_ref());
                let want_sz = (c["size"][0].as_u64().unwrap() as usize, c["size"][1].as_u64().unwrap() as usize);
                if sz != want_sz {
                    failed.push(format!("{} size {sz:?} != {want_sz:?}", what()));
                }
            }
            "draw" => {
                let st = style(&c["style"]);
                let w = wall(&c["wall"]);
                let (cols, rows) = (c["cols"].as_u64().unwrap() as usize, c["rows"].as_u64().unwrap() as usize);
                let mut f = frame_of(cols, rows);
                let sz = letters::draw(&mut f, c["x"].as_i64().unwrap(), c["y"].as_i64().unwrap(), text, &st, w.as_ref());
                let want = changed(&f, &c["want"]);
                let want_sz = (c["size"][0].as_u64().unwrap() as usize, c["size"][1].as_u64().unwrap() as usize);
                if f != want || sz != want_sz {
                    failed.push(format!("{} wall {} at {},{} in {cols}x{rows}: size {sz:?} want {want_sz:?}", what(), c["wall"], c["x"], c["y"]));
                }
            }
            "fit" => {
                let st = style(&c["style"]);
                let w = wall(&c["wall"]);
                let fonts: Vec<Font> = c["fonts"].as_array().unwrap().iter().map(|f| Font::from_name(f.as_str().unwrap()).unwrap()).collect();
                let got = letters::fit(text, c["cols"].as_u64().unwrap() as usize, c["rows"].as_u64().unwrap() as usize,
                                       &fonts, w.as_ref(), c["wrap"].as_bool().unwrap(), &st);
                if fitted(&got) != c["want"] {
                    failed.push(format!("{} fit {:?} {}x{} wall {}: {} != {}", what(), c["fonts"], c["cols"], c["rows"], c["wall"], fitted(&got), c["want"]));
                } else if let Some(g) = &got {
                    let drawn = letters::rows(&g.text, &g.style, None);
                    let want = cells(&c["drawn"]);
                    if drawn != want {
                        failed.push(format!("{} fitted, drawn\ngot\n{}\nwant\n{}", what(), show(&drawn), show(&want)));
                    }
                }
            }
            "centred" => {
                let st = style(&c["style"]);
                let w = wall(&c["wall"]);
                let fonts: Vec<Font> = c["fonts"].as_array().unwrap().iter().map(|f| Font::from_name(f.as_str().unwrap()).unwrap()).collect();
                let (cols, rows) = (c["cols"].as_u64().unwrap() as usize, c["rows"].as_u64().unwrap() as usize);
                let mut f = frame_of(cols, rows);
                let got = letters::centred(&mut f, text, &fonts, w.as_ref(), c["margin"].as_u64().unwrap() as usize,
                                           c["wrap"].as_bool().unwrap(), &st);
                let want = changed(&f, &c["want"]);
                if fitted(&got) != c["fitted"] || f != want {
                    failed.push(format!("{} centred {:?} {cols}x{rows}: {} want {}", what(), c["fonts"], fitted(&got), c["fitted"]));
                }
            }
            _ => panic!("no op {op}"),
        }
    }
    eprintln!("{} cases: {counts:?}, {} failed", cases.len(), failed.len());
    for f in failed.iter().take(8) {
        eprintln!("{f}\n");
    }
    assert!(failed.is_empty(), "{} of {} cases differ from Python", failed.len(), cases.len());
}

#[test]
fn pictured_fonts_are_not_usable() {
    let w = WallInfo { kitty: true, ..WallInfo::default() };
    for f in [Font::Tile, Font::WhiteRabbit, Font::Bytes256, Font::Vt323] {
        assert!(!letters::usable(f, Some(&w)));
        assert_eq!(letters::size("HI", &Style::new(f), Some(&w)), (0, 0));
    }
    let got = letters::fit("HI", 40, 10, &[Font::Tile, Font::Half], Some(&w), false, &Style::default()).unwrap();
    assert_eq!(got.style.font, Font::Half);
}
