//! The protocol's words: a command as it arrives, and a value as one word,
//! as bitwall/protocol.py has them.

/// The most an APC command may be, its body after `bitwall `.
pub const MAX_MESSAGE: usize = 4096;

/// A session's name, an id or a `from`: one to thirty-two of these.
pub fn is_name(s: &str) -> bool {
    !s.is_empty() && s.len() <= 32 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// An `instance`: one to sixty-four lower case letters, digits and `-`,
/// starting with a letter or a digit.
pub fn is_instance(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && !s.starts_with('-')
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// A meta key: one to thirty-two of `[a-z0-9._-]`.
pub fn is_meta_key(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.' || b == b'_' || b == b'-')
}

/// One word of a message: no whitespace, control or `=`, at most 256.
fn is_token(s: &str) -> bool {
    !s.is_empty() && s.chars().count() <= 256 && !s.chars().any(|c| c.is_whitespace() || c.is_control() || c == '=')
}

/// A number as this grammar writes one, and nothing else.
pub fn number(s: &str) -> Option<i64> {
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || digits.len() > 19 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// A value as one word: what a token cannot hold, percent-encoded.
pub fn word(value: &str) -> String {
    let mut out = String::new();
    for c in value.chars() {
        if c.is_whitespace() || c.is_control() || c == '=' || c == '%' {
            let mut b = [0u8; 4];
            for byte in c.encode_utf8(&mut b).bytes() {
                out.push_str(&format!("%{byte:02X}"));
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// A word as the value it carries: each whole run of %XX that is UTF-8.
pub fn unword(text: &str) -> String {
    if !text.contains('%') {
        return text.to_string();
    }
    let b = text.as_bytes();
    let hex = |c: u8| (c as char).to_digit(16).map(|d| d as u8);
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        let mut run = Vec::new();
        let mut j = i;
        while j + 2 < b.len() && b[j] == b'%' {
            match (hex(b[j + 1]), hex(b[j + 2])) {
                (Some(h), Some(l)) => {
                    run.push(h * 16 + l);
                    j += 3;
                }
                _ => break,
            }
        }
        if !run.is_empty() {
            match String::from_utf8(run) {
                Ok(s) => out.push_str(&s),
                Err(_) => out.push_str(&text[i..j]),
            }
            i = j;
        } else {
            let c = text[i..].chars().next().unwrap();
            out.push(c);
            i += c.len_utf8();
        }
    }
    out
}

/// One command as it arrived: its name, the words after it, its
/// `key=value` options decoded, and the words as they came.
#[derive(Debug, Clone)]
pub struct Message {
    pub name: String,
    pub args: Vec<String>,
    pub opts: Vec<(String, String)>,
    pub raw: String,
}

impl Message {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.opts.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
    }

    /// The body of an APC after `bitwall `, or None where one bad word
    /// spoils it.
    pub fn parse(body: &str) -> Option<Message> {
        let line = body.trim();
        if line.is_empty() || line.len() > 4096 {
            return None;
        }
        let words: Vec<&str> = line.split_whitespace().collect();
        if words.len() > 64 || !is_token(words[0]) {
            return None;
        }
        let mut args = Vec::new();
        let mut opts = Vec::new();
        for w in &words[1..] {
            if let Some((k, v)) = w.split_once('=') {
                if is_token(k) && is_token(v) {
                    opts.retain(|(x, _): &(String, String)| x != k);
                    opts.push((k.to_string(), unword(v)));
                    continue;
                }
                return None;
            } else if is_token(w) {
                args.push(unword(w));
            } else {
                return None;
            }
        }
        Some(Message { name: words[0].to_string(), args, opts, raw: words.join(" ") })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_round_trip() {
        assert_eq!(word("Back room"), "Back%20room");
        assert_eq!(unword("Back%20room"), "Back room");
        assert_eq!(unword("50%"), "50%");
        assert_eq!(unword("a%3Db"), "a=b");
    }

    #[test]
    fn parses_a_command() {
        let m = Message::parse("stats 1 at=ne show=fps,in id=k3").unwrap();
        assert_eq!(m.name, "stats");
        assert_eq!(m.args, vec!["1"]);
        assert_eq!(m.get("show"), Some("fps,in"));
        assert_eq!(m.get("id"), Some("k3"));
        assert!(Message::parse("x a==b").is_none());
    }
}
