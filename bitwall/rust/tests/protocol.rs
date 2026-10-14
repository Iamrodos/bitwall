//! The protocol module held to what `bitwall/protocol.py` made of the same
//! inputs, written to `golden/protocol.json` by `golden/protocol.py`.

use bitwall::protocol::{self, Lines, Message};
use serde_json::Value;

fn golden() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/golden/protocol.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("golden file")).expect("json")
}

fn s(v: &Value) -> &str {
    v.as_str().expect("string")
}

fn strings(v: &Value) -> Vec<&str> {
    v.as_array().expect("array").iter().map(s).collect()
}

fn pairs(v: &Value) -> Vec<(&str, &str)> {
    v.as_array().expect("array").iter().map(|p| (s(&p[0]), s(&p[1]))).collect()
}

fn message(v: &Value) -> Option<Message> {
    if v.is_null() {
        return None;
    }
    Some(Message {
        name: s(&v["name"]).to_string(),
        args: strings(&v["args"]).into_iter().map(String::from).collect(),
        opts: pairs(&v["opts"]).into_iter().map(|(k, v)| (k.into(), v.into())).collect(),
    })
}

/// Python's answer as an i64, None where it was None or does not fit.
fn int(v: &Value) -> Option<i64> {
    v.as_i64()
}

fn cases<'a>(g: &'a Value, key: &str) -> &'a Vec<Value> {
    let all = g[key].as_array().expect(key);
    assert!(!all.is_empty(), "{key}: no cases");
    all
}

#[test]
fn constants() {
    let g = golden();
    let c = &g["constants"];
    assert_eq!(s(&c["APC"]), protocol::APC);
    assert_eq!(s(&c["ST"]), protocol::ST);
    assert_eq!(s(&c["MARK"]), protocol::MARK);
    assert_eq!(c["VERSION"].as_i64(), Some(protocol::VERSION));
    assert_eq!(s(&c["GRAMMAR"]), protocol::GRAMMAR);
    assert_eq!(s(&c["TAG"]), protocol::TAG);
    assert_eq!(c["MAX_MESSAGE"].as_u64(), Some(protocol::MAX_MESSAGE as u64));
    assert_eq!(c["MAX_TOKENS"].as_u64(), Some(protocol::MAX_TOKENS as u64));
    assert_eq!(c["MAX_RANGES"].as_u64(), Some(protocol::MAX_RANGES as u64));
    assert_eq!(s(&c["hello"]), protocol::hello());
    assert_eq!(s(&c["announce"]), protocol::announce("area", &["174x42", "14"], &[]).unwrap());
    assert_eq!(strings(&c["IMAGES"]), protocol::IMAGES.to_vec());
}

#[test]
fn whitespace_is_pythons() {
    let g = golden();
    let spaces: Vec<u32> = cases(&g, "spaces").iter().map(|v| v.as_u64().unwrap() as u32).collect();
    let ours: Vec<u32> = (0..=0x10FFFFu32)
        .filter_map(char::from_u32)
        .filter(|&c| protocol::is_space(c))
        .map(|c| c as u32)
        .collect();
    assert_eq!(ours, spaces);
}

#[test]
fn parse() {
    let g = golden();
    for case in cases(&g, "parse") {
        let line = s(&case["in"]);
        assert_eq!(protocol::parse(line), message(&case["out"]), "parse {line:?}");
    }
}

#[test]
fn number_and_int() {
    let g = golden();
    for case in cases(&g, "number") {
        let text = s(&case["in"]);
        assert_eq!(protocol::number(text), int(&case["out"]), "number {text:?}");
    }
    for case in cases(&g, "int") {
        let said = protocol::parse(s(&case["line"])).expect("parses");
        let (low, high) = (case["low"].as_i64().unwrap(), case["high"].as_i64().unwrap());
        assert_eq!(said.int(s(&case["key"]), low, high), int(&case["out"]), "{case}");
    }
}

#[test]
fn word_and_unword() {
    let g = golden();
    for case in cases(&g, "word") {
        let value = s(&case["in"]);
        let w = protocol::word(value);
        assert_eq!(w, s(&case["word"]), "word {value:?}");
        assert_eq!(protocol::unword(&w), s(&case["back"]), "back {value:?}");
        assert_eq!(protocol::unword(&w), value, "round trip {value:?}");
    }
    for case in cases(&g, "unword") {
        let text = s(&case["in"]);
        assert_eq!(protocol::unword(text), s(&case["out"]), "unword {text:?}");
    }
}

#[test]
fn render_and_command() {
    let g = golden();
    for case in cases(&g, "render") {
        let (name, args, opts) = (s(&case["name"]), strings(&case["args"]), pairs(&case["opts"]));
        for (what, got) in [
            ("render", protocol::render(name, &args, &opts)),
            ("command", protocol::command(name, &args, &opts)),
        ] {
            let want = &case[what];
            match want.get("ok") {
                Some(ok) => assert_eq!(got.as_deref(), Ok(s(ok)), "{what} {name:?} {args:?} {opts:?}"),
                None => assert!(got.is_err(), "{what} {name:?} should be refused, got {got:?}"),
            }
        }
    }
}

#[test]
fn lines() {
    let g = golden();
    for case in cases(&g, "lines") {
        let pieces = strings(&case["pieces"]);
        let mut raw = Lines::default();
        let got: Vec<String> = pieces.iter().flat_map(|p| raw.feed(p)).collect();
        assert_eq!(got, strings(&case["lines"]), "lines of {} pieces", pieces.len());
        let mut said = Lines::default();
        let got: Vec<Message> = pieces.iter().flat_map(|p| said.feed_messages(p)).collect();
        let want: Vec<Message> = case["messages"].as_array().unwrap().iter().filter_map(message).collect();
        assert_eq!(got, want, "messages of {} pieces", pieces.len());
    }
}

#[test]
fn ranges_and_covers() {
    let g = golden();
    for case in cases(&g, "ranges") {
        let text = s(&case["in"]);
        let want: Vec<(u32, u32)> = case["out"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r[0].as_u64().unwrap() as u32, r[1].as_u64().unwrap() as u32))
            .collect();
        assert_eq!(protocol::ranges(text), want, "ranges {text:?}");
    }
    for case in cases(&g, "covers") {
        let spans: Vec<(u32, u32)> = case["spans"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r[0].as_u64().unwrap() as u32, r[1].as_u64().unwrap() as u32))
            .collect();
        let text = s(&case["text"]);
        assert_eq!(protocol::covers(&spans, text), case["out"].as_bool().unwrap(), "covers {text:?}");
    }
}

#[test]
fn version_of() {
    let g = golden();
    for case in cases(&g, "version_of") {
        let line = s(&case["in"]);
        let said = protocol::parse(line);
        assert_eq!(protocol::version_of(said.as_ref()), int(&case["out"]), "{line:?}");
    }
    assert_eq!(protocol::version_of(None), None);
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len()).step_by(2).map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap()).collect()
}

#[test]
fn images() {
    let g = golden();
    for case in cases(&g, "images") {
        let data = unhex(s(&case["hex"]));
        assert_eq!(protocol::image_of(&data), case["of"].as_str(), "image_of {}", s(&case["hex"]));
        for (codec, want) in case["size"].as_object().unwrap() {
            let want = want.as_array().map(|wh| (wh[0].as_u64().unwrap() as u32, wh[1].as_u64().unwrap() as u32));
            assert_eq!(protocol::image_size(codec, &data), want, "{codec} {}", s(&case["hex"]));
        }
    }
}

/// Untrusted input never panics: every short line of a hostile alphabet,
/// and long ones cut anywhere, parse or are None.
#[test]
fn never_panics() {
    let alphabet = ["a", "=", "%", "%4", "%E2", " ", "\n", "\x00", "\x1c", "é", "😀", "\u{3000}", "-", "9"];
    let mut seed: u64 = 7;
    let mut next = || {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (seed >> 33) as usize
    };
    let mut lines = Lines::default();
    for _ in 0..20000 {
        let n = next() % 40;
        let text: String = (0..n).map(|_| alphabet[next() % alphabet.len()]).collect();
        let _ = protocol::parse(&text);
        let _ = protocol::number(&text);
        let _ = protocol::unword(&text);
        let _ = protocol::ranges(&text);
        let _ = lines.feed_messages(&text);
        let _ = protocol::render(&text, &[&text], &[(&text, &text)]);
        let bytes = text.as_bytes();
        for codec in ["qoi", "png", "jpeg"] {
            let _ = protocol::image_size(codec, bytes);
        }
    }
    let flood = "x".repeat(protocol::MAX_MESSAGE * 40);
    assert!(lines.feed(&flood).is_empty());
    assert_eq!(lines.feed("\n").len(), 1);
}
