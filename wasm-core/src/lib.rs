//! xscraper-core: the text-analytics engine behind `xscraper`, compiled to
//! `wasm32-unknown-unknown` and loaded from Python through wasmtime.
//!
//! For every tweet text it extracts entities (hashtags, mentions, cashtags,
//! URLs), scores sentiment with a lexicon model, and computes a 64-bit
//! SimHash fingerprint for near-duplicate detection.
//!
//! The pure-Python fallback in `xscraper/analysis/python_engine.py`
//! implements the same algorithms step for step. If you change one, change
//! the other, and run the parity tests.
//!
//! ABI (all integers little-endian, memory is the module's exported memory):
//!   alloc(len) -> ptr              allocate `len` bytes for the host to write
//!   dealloc(ptr, len)              free a buffer from `alloc` or `analyze`
//!   analyze(ptr, len) -> u64       input: repeated [u32 byte_len][utf-8 text]
//!                                  output: (out_ptr << 32) | out_len, a UTF-8
//!                                  JSON array with one object per text
//!   abi_version() -> u32

use std::collections::HashSet;

mod unicode_tables;
pub use unicode_tables::{UNICODE_VERSION, WORD_RANGES};

pub const ABI_VERSION: u32 = 2;

// ---------------------------------------------------------------------------
// Character classes
// ---------------------------------------------------------------------------

/// Unicode White_Space, spelled out so the Python side can match it exactly.
pub fn is_ws(c: char) -> bool {
    matches!(
        c,
        '\u{09}'..='\u{0D}'
            | '\u{20}'
            | '\u{85}'
            | '\u{A0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200A}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202F}'
            | '\u{205F}'
            | '\u{3000}'
    )
}

/// Letters, numbers, marks and `_`. Non-ASCII classes come from the generated
/// table rather than `char::is_alphanumeric`, so they include combining marks
/// (Devanagari vowel signs, Thai tone marks, ...) and match the Python engine
/// exactly regardless of either toolchain's Unicode version.
pub fn is_word(c: char) -> bool {
    if c.is_ascii() {
        return c == '_' || c.is_ascii_alphanumeric();
    }
    let cp = c as u32;
    WORD_RANGES
        .binary_search_by(|&(start, end)| {
            if end < cp {
                std::cmp::Ordering::Less
            } else if start > cp {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

fn is_ascii_word(c: char) -> bool {
    c == '_' || c.is_ascii_alphanumeric()
}

// ---------------------------------------------------------------------------
// Entity extraction
// ---------------------------------------------------------------------------

#[derive(Debug, Default, PartialEq)]
pub struct Entities {
    pub hashtags: Vec<String>,
    pub mentions: Vec<String>,
    pub cashtags: Vec<String>,
    pub urls: Vec<String>,
}

fn push_unique(list: &mut Vec<String>, seen: &mut HashSet<String>, value: String, key: String) {
    if seen.insert(key) {
        list.push(value);
    }
}

fn starts_with_at(chars: &[char], i: usize, pat: &str) -> bool {
    let mut k = i;
    for p in pat.chars() {
        if k >= chars.len() || chars[k].to_ascii_lowercase() != p {
            return false;
        }
        k += 1;
    }
    true
}

fn trim_url(url: &mut Vec<char>) {
    loop {
        let last = match url.last() {
            Some(&c) => c,
            None => return,
        };
        let strip = match last {
            '.' | ',' | ';' | ':' | '!' | '?' | '\'' | '"' => true,
            ')' | ']' | '}' => {
                let open = match last {
                    ')' => '(',
                    ']' => '[',
                    _ => '{',
                };
                let opens = url.iter().filter(|&&c| c == open).count();
                let closes = url.iter().filter(|&&c| c == last).count();
                closes > opens
            }
            _ => false,
        };
        if !strip {
            return;
        }
        url.pop();
    }
}

pub fn extract_entities(text: &str) -> Entities {
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out = Entities::default();
    let (mut seen_h, mut seen_m, mut seen_c, mut seen_u) =
        (HashSet::new(), HashSet::new(), HashSet::new(), HashSet::new());

    let mut i = 0;
    while i < n {
        let c = chars[i];
        let prev_ok = i == 0 || !(is_word(chars[i - 1]) || chars[i - 1] == '&');

        if (c == '#' || c == '\u{FF03}') && prev_ok {
            let mut j = i + 1;
            while j < n && is_word(chars[j]) {
                j += 1;
            }
            let tag: String = chars[i + 1..j].iter().collect();
            if tag.chars().any(|t| !t.is_ascii_digit()) {
                let key = tag.to_lowercase();
                push_unique(&mut out.hashtags, &mut seen_h, tag, key);
            }
            i = j.max(i + 1);
            continue;
        }

        if (c == '@' || c == '\u{FF20}') && prev_ok {
            let mut j = i + 1;
            while j < n && is_ascii_word(chars[j]) {
                j += 1;
            }
            let len = j - (i + 1);
            let next_ok = j >= n || !(is_word(chars[j]) || chars[j] == '@');
            if (1..=15).contains(&len) && next_ok {
                let name: String = chars[i + 1..j].iter().collect();
                let key = name.to_lowercase();
                push_unique(&mut out.mentions, &mut seen_m, name, key);
            }
            i = j.max(i + 1);
            continue;
        }

        if c == '$' && prev_ok {
            let mut j = i + 1;
            while j < n && chars[j].is_ascii_alphabetic() {
                j += 1;
            }
            let len = j - (i + 1);
            let next_ok = j >= n || !is_word(chars[j]);
            if (1..=6).contains(&len) && next_ok {
                let sym: String = chars[i + 1..j].iter().collect::<String>().to_ascii_uppercase();
                push_unique(&mut out.cashtags, &mut seen_c, sym.clone(), sym);
            }
            i = j.max(i + 1);
            continue;
        }

        if (c == 'h' || c == 'H')
            && (i == 0 || !is_word(chars[i - 1]))
            && (starts_with_at(&chars, i, "https://") || starts_with_at(&chars, i, "http://"))
        {
            let mut j = i;
            while j < n && !is_ws(chars[j]) && !matches!(chars[j], '<' | '>' | '"') {
                j += 1;
            }
            let mut url: Vec<char> = chars[i..j].to_vec();
            trim_url(&mut url);
            let scheme_len = if starts_with_at(&chars, i, "https://") { 8 } else { 7 };
            if url.len() > scheme_len {
                let s: String = url.into_iter().collect();
                push_unique(&mut out.urls, &mut seen_u, s.clone(), s);
            }
            i = j.max(i + 1);
            continue;
        }

        i += 1;
    }
    out
}

// ---------------------------------------------------------------------------
// Tokenisation
// ---------------------------------------------------------------------------

/// Lower-cased word tokens. URLs are skipped; apostrophes stay inside words
/// ("don't") but are trimmed from the edges.
pub fn tokenize(text: &str) -> Vec<String> {
    let lower = text.to_lowercase();
    let mut tokens = Vec::new();
    for chunk in lower.split(is_ws) {
        if chunk.is_empty() || chunk.starts_with("http://") || chunk.starts_with("https://") {
            continue;
        }
        let mut cur = String::new();
        for c in chunk.chars().chain(std::iter::once(' ')) {
            if is_word(c) || c == '\'' {
                cur.push(c);
            } else if !cur.is_empty() {
                let t = cur.trim_matches('\'');
                if !t.is_empty() {
                    tokens.push(t.to_string());
                }
                cur.clear();
            }
        }
    }
    tokens
}

// ---------------------------------------------------------------------------
// Sentiment
// ---------------------------------------------------------------------------

/// Compact AFINN-style lexicon. `xscraper/analysis/lexicon.py` holds the
/// identical table; the parity tests fail if the two drift apart.
pub static LEXICON: &[(&str, i32)] = &[
    ("abandon", -2), ("abuse", -3), ("accomplished", 2), ("amazing", 4),
    ("angry", -3), ("annoying", -2), ("anxious", -2), ("appreciate", 2),
    ("awesome", 4), ("awful", -3), ("bad", -3), ("beautiful", 3),
    ("best", 3), ("better", 2), ("bless", 2), ("boring", -3),
    ("brilliant", 4), ("broken", -1), ("bug", -2), ("bullish", 2),
    ("bearish", -2), ("calm", 2), ("celebrate", 3), ("cheer", 2),
    ("confused", -2), ("congrats", 2), ("congratulations", 2), ("cool", 1),
    ("crash", -2), ("crisis", -3), ("cry", -1), ("damn", -2),
    ("dead", -3), ("delighted", 3), ("disappointed", -2), ("disaster", -2),
    ("disgusting", -3), ("dumb", -3), ("easy", 1), ("enjoy", 2),
    ("error", -2), ("excellent", 3), ("excited", 3), ("fail", -2),
    ("failed", -2), ("failure", -2), ("fake", -3), ("fantastic", 4),
    ("fear", -2), ("fine", 2), ("fix", 1), ("fixed", 2),
    ("fraud", -4), ("free", 1), ("fun", 4), ("glad", 3),
    ("good", 3), ("gorgeous", 3), ("great", 3), ("grateful", 3),
    ("happy", 3), ("hate", -3), ("hope", 2), ("horrible", -3),
    ("hurt", -2), ("impressive", 3), ("incredible", 4), ("innovative", 2),
    ("inspiring", 3), ("interesting", 2), ("kill", -3), ("lame", -2),
    ("launch", 1), ("lol", 3), ("lose", -3), ("loss", -3),
    ("love", 3), ("loved", 3), ("lovely", 3), ("mad", -3),
    ("mess", -2), ("nice", 3), ("outage", -2), ("pain", -2),
    ("perfect", 3), ("pleased", 3), ("poor", -2), ("problem", -2),
    ("proud", 2), ("rage", -2), ("recommend", 2), ("sad", -2),
    ("scam", -2), ("scary", -2), ("shame", -2), ("sick", -2),
    ("slow", -2), ("sorry", -1), ("stupid", -2), ("success", 2),
    ("successful", 3), ("super", 3), ("terrible", -3), ("thank", 2),
    ("thanks", 2), ("thrilled", 5), ("ugly", -3), ("upset", -2),
    ("useless", -2), ("win", 4), ("winning", 4), ("wonderful", 4),
    ("worried", -3), ("worse", -3), ("worst", -3), ("wow", 4),
    ("wrong", -2), ("yay", 2),
];

pub static NEGATORS: &[&str] = &[
    "ain't", "aint", "can't", "cannot", "cant", "didn't", "didnt", "doesn't",
    "doesnt", "don't", "dont", "isn't", "isnt", "neither", "never", "no",
    "nobody", "nor", "not", "nothing", "wasn't", "wasnt", "without", "won't",
    "wont",
];

pub static BOOSTERS: &[&str] = &[
    "absolutely", "extremely", "incredibly", "really", "so", "super",
    "totally", "very",
];

pub fn lexicon_score(word: &str) -> Option<i32> {
    LEXICON.iter().find(|(w, _)| *w == word).map(|(_, s)| *s)
}

/// Compound score in [-1, 1] (VADER-style normalisation of the summed valence).
pub fn sentiment(tokens: &[String]) -> f64 {
    let mut total = 0.0f64;
    let mut negate_left = 0u32;
    let mut boost = false;
    for tok in tokens {
        let t = tok.as_str();
        if NEGATORS.contains(&t) {
            negate_left = 3;
            continue;
        }
        if BOOSTERS.contains(&t) && lexicon_score(t).is_none() {
            boost = true;
            continue;
        }
        if let Some(v) = lexicon_score(t) {
            let mut v = v as f64;
            if boost {
                v *= 1.5;
            }
            if negate_left > 0 {
                v *= -0.75;
                negate_left = 0;
            }
            total += v;
        } else if negate_left > 0 {
            negate_left -= 1;
        }
        boost = false;
    }
    total / (total * total + 15.0).sqrt()
}

// ---------------------------------------------------------------------------
// SimHash
// ---------------------------------------------------------------------------

pub fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

/// 64-bit SimHash over word unigrams and bigrams.
pub fn simhash(tokens: &[String]) -> u64 {
    let mut v = [0i64; 64];
    let mut add = |feature: &str| {
        let h = fnv1a64(feature.as_bytes());
        for (bit, slot) in v.iter_mut().enumerate() {
            if (h >> bit) & 1 == 1 {
                *slot += 1;
            } else {
                *slot -= 1;
            }
        }
    };
    for t in tokens {
        add(t);
    }
    for pair in tokens.windows(2) {
        add(&format!("{} {}", pair[0], pair[1]));
    }
    if tokens.is_empty() {
        return 0;
    }
    let mut out = 0u64;
    for (bit, &slot) in v.iter().enumerate() {
        if slot > 0 {
            out |= 1 << bit;
        }
    }
    out
}

// ---------------------------------------------------------------------------
// JSON output
// ---------------------------------------------------------------------------

fn json_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn json_list(out: &mut String, key: &str, items: &[String]) {
    json_str(out, key);
    out.push_str(":[");
    for (k, item) in items.iter().enumerate() {
        if k > 0 {
            out.push(',');
        }
        json_str(out, item);
    }
    out.push(']');
}

pub fn analyze_one(text: &str, out: &mut String) {
    let e = extract_entities(text);
    let tokens = tokenize(text);
    out.push('{');
    json_list(out, "hashtags", &e.hashtags);
    out.push(',');
    json_list(out, "mentions", &e.mentions);
    out.push(',');
    json_list(out, "cashtags", &e.cashtags);
    out.push(',');
    json_list(out, "urls", &e.urls);
    out.push_str(&format!(
        ",\"tokens\":{},\"sentiment\":{:?},\"simhash\":{}}}",
        tokens.len(),
        sentiment(&tokens),
        simhash(&tokens)
    ));
}

/// Decode the length-prefixed batch and analyse every record.
pub fn analyze_batch(input: &[u8]) -> String {
    let mut out = String::with_capacity(input.len() * 2 + 2);
    out.push('[');
    let mut pos = 0;
    let mut first = true;
    while pos + 4 <= input.len() {
        let len = u32::from_le_bytes([input[pos], input[pos + 1], input[pos + 2], input[pos + 3]]) as usize;
        pos += 4;
        let end = (pos + len).min(input.len());
        let text = String::from_utf8_lossy(&input[pos..end]);
        pos = end;
        if !first {
            out.push(',');
        }
        first = false;
        analyze_one(&text, &mut out);
    }
    out.push(']');
    out
}

// ---------------------------------------------------------------------------
// WebAssembly exports
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn alloc(len: usize) -> *mut u8 {
    let mut buf: Vec<u8> = Vec::with_capacity(len.max(1));
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

/// # Safety
/// `ptr`/`len` must come from `alloc(len)` or from an `analyze` result.
#[no_mangle]
pub unsafe extern "C" fn dealloc(ptr: *mut u8, len: usize) {
    drop(Vec::from_raw_parts(ptr, 0, len.max(1)));
}

/// # Safety
/// `ptr`/`len` must describe a buffer obtained from `alloc(len)`.
#[no_mangle]
pub unsafe extern "C" fn analyze(ptr: *const u8, len: usize) -> u64 {
    let input = std::slice::from_raw_parts(ptr, len);
    let json = analyze_batch(input).into_bytes().into_boxed_slice();
    let out_len = json.len();
    let out_ptr = Box::into_raw(json) as *mut u8;
    ((out_ptr as u64) << 32) | out_len as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn entities() {
        let e = extract_entities("RT @NASA: #Artemis launch! $TSLA https://x.com/a/b). me@mail.com #123 #1st");
        assert_eq!(e.mentions, vec!["NASA"]);
        assert_eq!(e.hashtags, vec!["Artemis", "1st"]);
        assert_eq!(e.cashtags, vec!["TSLA"]);
        assert_eq!(e.urls, vec!["https://x.com/a/b"]);
    }

    #[test]
    fn hashtags_keep_combining_marks() {
        let e = extract_entities("#नमस्ते #தமிழ் #ภาษาไทย");
        assert_eq!(e.hashtags, vec!["नमस्ते", "தமிழ்", "ภาษาไทย"]);
    }

    #[test]
    fn sentiment_sign() {
        assert!(sentiment(&tokenize("I love this, it's really great")) > 0.5);
        assert!(sentiment(&tokenize("this is not good at all")) < 0.0);
        assert_eq!(sentiment(&tokenize("the sky")), 0.0);
    }

    #[test]
    fn simhash_near_duplicates() {
        let a = simhash(&tokenize("NASA launches the Artemis rocket to the moon today"));
        let b = simhash(&tokenize("NASA launches the Artemis rocket to the moon today!!"));
        let c = simhash(&tokenize("my cat refuses to eat breakfast again"));
        assert_eq!(a, b);
        assert!((a ^ c).count_ones() > 10);
    }
}
