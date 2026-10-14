//! The words a wall and its callers say (`bitwall/protocol.py`).
//!
//! A session's two pipes. Frames and requests go down its `in`; everything
//! a wall has to say to its caller comes back up its `out`. Both are text a
//! person can read: commands travel in the frame stream as APC strings,
//! which a terminal ignores, so the stream stays paintable; what comes back
//! is lines that `grep` and `awk` are enough to read.
//!
//! ```text
//! down (in)   ESC _ bitwall font 14 ESC \
//! up   (out)  area 174x42 14
//! ```
//!
//! Requests are advisory and announcements are the truth: a sender asks for
//! a font size and learns what happened by being told the new area.
//!
//! What a wall says is untrusted, since a pipe is fed by whoever can write
//! to it. Nothing here panics on any input: a line that is not a message is
//! None, and the bounds below hold however much arrives.
//!
//! Python's idea of whitespace is the grammar's, since `\s`, `strip` and
//! `split` there decide what a word is: Unicode's White_Space and the four
//! information separators U+001C-001F as well ([`is_space`]). Lengths are
//! counted in characters, as Python counts them.

/// An Application Programming Command string. A terminal ignores one it
/// does not recognise, so commands can travel in the frame stream without
/// a terminal trying to draw them.
pub const APC: &str = "\x1b_";
pub const ST: &str = "\x1b\\";

/// Every command carries this, so that ours are told apart from anyone
/// else's APC strings in the same stream.
pub const MARK: &str = "bitwall";

/// The version of the framing, and the grammar it is framed in: the first
/// line a wall sends, and the one thing whose encoding can never change.
pub const VERSION: i64 = 1;
pub const GRAMMAR: &str = "line";

/// The version as it is written on the wire, prefixed so it reads as one.
pub const TAG: &str = "v1";

/// The longest message, in characters. A pipe is fed by whoever can write
/// to it.
pub const MAX_MESSAGE: usize = 4096;

/// The most words a message may have, its name among them.
pub const MAX_TOKENS: usize = 64;

/// The longest word, in characters.
pub const MAX_WORD: usize = 256;

/// How many ranges a wall may claim: long enough for any real font's
/// interesting parts, short enough that the claim cannot be a payload.
pub const MAX_RANGES: usize = 32;

/// What a picture on a video layer's `in` may be, as `frame <codec>` names
/// it: QOI, which every wall that takes video draws, then PNG and JPEG
/// where its `caps` says them in `images=`.
pub const IMAGES: [&str; 3] = ["qoi", "png", "jpeg"];

/// Whether Python calls `c` whitespace: Unicode's White_Space, and the
/// information separators U+001C-001F, which Python counts and Rust does
/// not. A word is what lies between runs of these.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// Whether a word is a token: 1 to 256 characters, no whitespace, no
/// controls, no `=`. So a message can never contain its own terminator,
/// and a reader can split on whitespace without a parser.
fn token(text: &str) -> bool {
    let mut n = 0;
    for c in text.chars() {
        n += 1;
        if n > MAX_WORD || is_space(c) || c < '\x20' || c == '\x7f' || c == '=' {
            return false;
        }
    }
    n > 0
}

/// Whether `text` is one word as the grammar has it, `TOKEN` in Python.
fn token_matched(text: &str) -> bool {
    token(text)
}

/// Whether a character is written percent-encoded in a value: whitespace,
/// the controls, `=`, and `%` itself.
fn encoded(c: char) -> bool {
    is_space(c) || c < '\x20' || c == '\x7f' || c == '=' || c == '%'
}

/// One thing said, in either direction: `name`, positional `args`, and
/// `key=value` `opts` in the order they came. Decoded (`%20` is a space).
///
/// A key said twice keeps its first place and its last value, as a Python
/// dict does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    pub name: String,
    pub args: Vec<String>,
    pub opts: Vec<(String, String)>,
}

impl Message {
    /// The value of an option, or None where it was not said.
    pub fn get(&self, key: &str) -> Option<&str> {
        self.opts.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
    }

    /// An option as an integer in `low..=high`, else None.
    pub fn int(&self, key: &str, low: i64, high: i64) -> Option<i64> {
        number(self.get(key)?).filter(|v| (low..=high).contains(v))
    }

    fn set(&mut self, key: &str, value: String) {
        match self.opts.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => self.opts.push((key.to_string(), value)),
        }
    }
}

/// `text` as an integer, or None: an optional `-` and 1 to 19 ASCII
/// digits, and nothing else.
///
/// The one test, so that deciding a word is a number and turning it into
/// one cannot disagree. A number too big for an i64 is None: it is no
/// count a wall could mean.
pub fn number(text: &str) -> Option<i64> {
    let digits = text.strip_prefix('-').unwrap_or(text);
    if digits.is_empty() || digits.len() > 19 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

/// Code point ranges as `[(low, high)]`, or none where the text says none.
///
/// CSS's `unicode-range` spelling, `U+2588` or `U+2500-257F`, comma
/// separated, since declaring which code points a font covers is what that
/// exists for. Only the first 32 parts are read; one that is not a range,
/// is backwards or passes U+10FFFF is passed over. Unordered and
/// overlapping are both fine.
pub fn ranges(text: &str) -> Vec<(u32, u32)> {
    let mut out = Vec::new();
    if text.is_empty() {
        return out;
    }
    for part in text.split(',').take(MAX_RANGES) {
        if let Some((low, high)) = range(part.trim_matches(is_space)) {
            if low <= high && high <= 0x10FFFF {
                out.push((low, high));
            }
        }
    }
    out
}

fn range(part: &str) -> Option<(u32, u32)> {
    let rest = part.strip_prefix(['u', 'U'])?.strip_prefix('+')?;
    let hex = |s: &str| {
        if (1..=6).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_hexdigit()) {
            u32::from_str_radix(s, 16).ok()
        } else {
            None
        }
    };
    match rest.split_once('-') {
        Some((low, high)) => Some((hex(low)?, hex(high)?)),
        None => hex(rest).map(|low| (low, low)),
    }
}

/// Whether every character of `text` falls inside `spans`.
///
/// True for no spans: a wall that has not said what it can draw has not
/// said it cannot draw this.
pub fn covers(spans: &[(u32, u32)], text: &str) -> bool {
    spans.is_empty()
        || text.chars().all(|c| spans.iter().any(|&(low, high)| (low..=high).contains(&(c as u32))))
}

/// A value as one word: what a token cannot hold, percent-encoded, `%XX`
/// for each of its UTF-8 bytes. `word("my value")` is `my%20value`.
///
/// A command's name and an option's key are not encoded: they are
/// vocabulary, and one that is not a word is refused.
pub fn word(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        if encoded(c) {
            let mut buf = [0; 4];
            for b in c.encode_utf8(&mut buf).bytes() {
                out.push_str(&format!("%{b:02X}"));
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// A word as the value it carries: each run of `%XX` decoded, leniently.
///
/// Only whole runs that are UTF-8 are decoded, and a run that is not stays
/// as it came, every escape of it, so a `%` that is no escape, as in `50%`,
/// stays.
pub fn unword(text: &str) -> String {
    if !text.contains('%') {
        return text.to_string();
    }
    let b = text.as_bytes();
    let escape = |i: usize| {
        i + 2 < b.len() && b[i] == b'%' && b[i + 1].is_ascii_hexdigit() && b[i + 2].is_ascii_hexdigit()
    };
    let mut out = String::with_capacity(text.len());
    let (mut i, mut kept) = (0, 0);
    while i < b.len() {
        if !escape(i) {
            i += 1;
            continue;
        }
        let start = i;
        let mut raw = Vec::new();
        while escape(i) {
            let hex = &text[i + 1..i + 3];
            raw.push(u8::from_str_radix(hex, 16).unwrap_or(0));
            i += 3;
        }
        if let Ok(decoded) = String::from_utf8(raw) {
            out.push_str(&text[kept..start]);
            out.push_str(&decoded);
            kept = i;
        }
    }
    out.push_str(&text[kept..]);
    out
}

/// A line of text as a Message, or None where it is not one.
///
/// Its arguments and values decoded: `title=my%20value` is `my value`.
/// None rather than an error: this reads a pipe anyone can write to, and a
/// malformed line is a thing to drop. One bad word spoils the message.
pub fn parse(line: &str) -> Option<Message> {
    let line = line.trim_matches(is_space);
    if line.is_empty() || line.chars().nth(MAX_MESSAGE).is_some() {
        return None;
    }
    let mut words = line.split(is_space).filter(|w| !w.is_empty());
    let name = words.next()?;
    if !token(name) {
        return None;
    }
    let mut said = Message { name: name.to_string(), args: Vec::new(), opts: Vec::new() };
    for (n, each) in words.enumerate() {
        if n + 2 > MAX_TOKENS {
            return None;
        }
        match each.split_once('=') {
            Some((key, value)) if token(key) && token(value) => said.set(key, unword(value)),
            None if token(each) => said.args.push(unword(each)),
            _ => return None,
        }
    }
    Some(said)
}

/// A message as a line, without a terminator; opts sorted by key as
/// Python sorts them. Err where a word cannot be sent or it is too long.
///
/// Refused rather than escaped where a name or a key is not a word: that
/// is a sign the wrong thing is being sent, and mangling it quietly would
/// hide it until somebody read a wall and did not believe it.
pub fn render(name: &str, args: &[&str], opts: &[(&str, &str)]) -> Result<String, String> {
    fn one(text: String) -> Result<String, String> {
        if token_matched(&text) { Ok(text) } else { Err(format!("not sendable as one word: {text:?}")) }
    }
    let mut words = vec![one(name.to_string())?];
    for a in args {
        words.push(one(word(a))?);
    }
    let mut opts = opts.to_vec();
    opts.sort();
    for (k, v) in opts {
        words.push(format!("{}={}", one(k.to_string())?, one(word(v))?));
    }
    if words.len() > MAX_TOKENS {
        return Err(format!("{} words is too many for one message", words.len()));
    }
    let line = words.join(" ");
    let n = line.chars().count();
    if n > MAX_MESSAGE {
        return Err(format!("{n} characters is too long for one message"));
    }
    Ok(line)
}

/// A request wrapped as APC to travel in the frame stream.
pub fn command(name: &str, args: &[&str], opts: &[(&str, &str)]) -> Result<String, String> {
    Ok(format!("{APC}{MARK} {}{ST}", render(name, args, opts)?))
}

/// One thing a wall has to say, as a line.
pub fn announce(name: &str, args: &[&str], opts: &[(&str, &str)]) -> Result<String, String> {
    Ok(render(name, args, opts)? + "\n")
}

/// The first line a wall sends, and the one that cannot change.
pub fn hello() -> String {
    format!("hello {TAG} {GRAMMAR}")
}

/// The version a `hello` claims, as a number, or None. Tolerant of the
/// `v` being absent, as a wall written against an earlier reading may
/// leave it out.
pub fn version_of(said: Option<&Message>) -> Option<i64> {
    let said = said.filter(|m| m.name == "hello")?;
    let text = said.args.first()?;
    number(text.strip_prefix(['v', 'V']).unwrap_or(text))
}

/// What a wall says on `out`, fed in whatever pieces it arrived in, as
/// whole lines.
///
/// A wall writing without newlines is not let grow the buffer without
/// bound: past sixteen messages' worth, only the last message's worth is
/// kept.
#[derive(Default)]
pub struct Lines {
    rest: String,
}

impl Lines {
    /// The lines this piece completes, every one of them, empty or not.
    pub fn feed(&mut self, text: &str) -> Vec<String> {
        let mut text = std::mem::take(&mut self.rest) + text;
        if text.chars().nth(MAX_MESSAGE * 16).is_some() {
            if let Some((at, _)) = text.char_indices().rev().nth(MAX_MESSAGE - 1) {
                text.drain(..at);
            }
        }
        let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
        self.rest = lines.pop().unwrap_or_default();
        lines
    }

    /// The messages this piece completes, a line that is none dropped.
    pub fn feed_messages(&mut self, text: &str) -> Vec<Message> {
        self.feed(text).iter().filter_map(|line| parse(line)).collect()
    }
}

/// The codec a picture's first bytes say it is, or None.
pub fn image_of(data: &[u8]) -> Option<&'static str> {
    if data.starts_with(b"qoif") {
        Some("qoi")
    } else if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("png")
    } else if data.starts_with(b"\xff\xd8\xff") {
        Some("jpeg")
    } else {
        None
    }
}

fn be16(d: &[u8], at: usize) -> u32 {
    u32::from(d[at]) << 8 | u32::from(d[at + 1])
}

fn be32(d: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([d[at], d[at + 1], d[at + 2], d[at + 3]])
}

/// `(width, height)` a picture's own header says, or None where `data` is
/// not a whole picture of `codec` a wall draws: its header and its end are
/// checked, not its pixels. A JPEG in four components, CMYK, is None: no
/// wall draws one.
pub fn image_size(codec: &str, data: &[u8]) -> Option<(u32, u32)> {
    let n = data.len();
    match codec {
        "qoi" => {
            if n < 22 || !data.starts_with(b"qoif") || !data.ends_with(b"\0\0\0\0\0\0\0\x01") {
                return None;
            }
            Some((be32(data, 4), be32(data, 8)))
        }
        "png" => {
            if n < 45
                || !data.starts_with(b"\x89PNG\r\n\x1a\n")
                || &data[12..16] != b"IHDR"
                || &data[n - 8..n - 4] != b"IEND"
            {
                return None;
            }
            Some((be32(data, 16), be32(data, 20)))
        }
        "jpeg" => {
            if n < 4 || !data.starts_with(b"\xff\xd8") || !data.ends_with(b"\xff\xd9") {
                return None;
            }
            let mut p = 2;
            while p + 4 <= n {
                if data[p] != 0xFF {
                    return None;
                }
                let marker = data[p + 1];
                if marker == 0xFF {
                    p += 1;
                    continue;
                }
                if marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
                    p += 2;
                    continue;
                }
                let length = be16(data, p + 2) as usize;
                // SOF0-SOF15, less DHT (C4), JPG (C8) and DAC (CC), which
                // share the range: the markers that carry a frame's size.
                if (0xC0..=0xCF).contains(&marker) && !matches!(marker, 0xC4 | 0xC8 | 0xCC) {
                    if length < 8 || p + 10 > n || !matches!(data[p + 9], 1 | 3) {
                        return None;
                    }
                    let (h, w) = (be16(data, p + 5), be16(data, p + 7));
                    return if w != 0 && h != 0 { Some((w, h)) } else { None };
                }
                if marker == 0xDA || length < 2 {
                    return None;
                }
                p += 2 + length;
            }
            None
        }
        _ => None,
    }
}

#[cfg(test)]
mod version {
    /// The crate's major version is the protocol's, as the Python
    /// package's is: a sender pinned to a major release speaks its walls'.
    #[test]
    fn the_crates_major_is_the_protocols_version() {
        let major: i64 = env!("CARGO_PKG_VERSION_MAJOR").parse().unwrap();
        assert_eq!(major, super::VERSION);
        assert_eq!(super::TAG, format!("v{}", super::VERSION));
    }
}
