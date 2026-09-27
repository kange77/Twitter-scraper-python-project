"""Pure-Python port of wasm-core/src/lib.rs.

Used when wasmtime isn't installed, and as the reference the WASM engine is
tested against. Every function mirrors its Rust counterpart step for step.
"""
from __future__ import annotations

import math
from bisect import bisect_right

from ._unicode_tables import WORD_ENDS, WORD_STARTS
from .lexicon import BOOSTERS, LEXICON, NEGATORS

_WS = frozenset(
    [*map(chr, range(0x09, 0x0E)), " ", "\x85", "\xa0", " ",
     *map(chr, range(0x2000, 0x200B)), " ", " ", " ", " ", "　"]
)
_URL_TRAILING = frozenset(".,;:!?'\"")
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = (1 << 64) - 1


def is_word(c: str) -> bool:
    """Letters, numbers, marks and ``_``, using the table shared with the Rust core."""
    if c < "\x80":
        return c == "_" or c.isalnum()
    cp = ord(c)
    i = bisect_right(WORD_STARTS, cp) - 1
    return i >= 0 and cp <= WORD_ENDS[i]


def _is_ascii_word(c: str) -> bool:
    return c == "_" or (c.isascii() and c.isalnum())


def _starts_with_at(chars: str, i: int, pat: str) -> bool:
    seg = chars[i:i + len(pat)]
    return seg.isascii() and seg.lower() == pat


def _trim_url(url: list[str]) -> None:
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            url.pop()
        elif last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last]):
            url.pop()
        else:
            return


def extract_entities(text: str) -> dict[str, list[str]]:
    chars = text
    n = len(chars)
    out: dict[str, list[str]] = {"hashtags": [], "mentions": [], "cashtags": [], "urls": []}
    seen: dict[str, set[str]] = {k: set() for k in out}

    def push(kind: str, value: str, key: str) -> None:
        if key not in seen[kind]:
            seen[kind].add(key)
            out[kind].append(value)

    i = 0
    while i < n:
        c = chars[i]
        prev_ok = i == 0 or not (is_word(chars[i - 1]) or chars[i - 1] == "&")

        if c in "#＃" and prev_ok:
            j = i + 1
            while j < n and is_word(chars[j]):
                j += 1
            tag = chars[i + 1:j]
            if any(not ("0" <= t <= "9") for t in tag):
                push("hashtags", tag, tag.lower())
            i = max(j, i + 1)
            continue

        if c in "@＠" and prev_ok:
            j = i + 1
            while j < n and _is_ascii_word(chars[j]):
                j += 1
            length = j - (i + 1)
            next_ok = j >= n or not (is_word(chars[j]) or chars[j] == "@")
            if 1 <= length <= 15 and next_ok:
                name = chars[i + 1:j]
                push("mentions", name, name.lower())
            i = max(j, i + 1)
            continue

        if c == "$" and prev_ok:
            j = i + 1
            while j < n and chars[j].isascii() and chars[j].isalpha():
                j += 1
            length = j - (i + 1)
            next_ok = j >= n or not is_word(chars[j])
            if 1 <= length <= 6 and next_ok:
                sym = chars[i + 1:j].upper()
                push("cashtags", sym, sym)
            i = max(j, i + 1)
            continue

        if c in "hH" and (i == 0 or not is_word(chars[i - 1])):
            https = _starts_with_at(chars, i, "https://")
            if https or _starts_with_at(chars, i, "http://"):
                j = i
                while j < n and chars[j] not in _WS and chars[j] not in '<>"':
                    j += 1
                url = list(chars[i:j])
                _trim_url(url)
                if len(url) > (8 if https else 7):
                    s = "".join(url)
                    push("urls", s, s)
                i = max(j, i + 1)
                continue

        i += 1
    return out


def _split_ws(text: str) -> list[str]:
    chunks, cur = [], []
    for c in text:
        if c in _WS:
            chunks.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    chunks.append("".join(cur))
    return chunks


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in _split_ws(text.lower()):
        if not chunk or chunk.startswith(("http://", "https://")):
            continue
        cur: list[str] = []
        for c in chunk + " ":
            if is_word(c) or c == "'":
                cur.append(c)
            elif cur:
                t = "".join(cur).strip("'")
                if t:
                    tokens.append(t)
                cur = []
    return tokens


def sentiment(tokens: list[str]) -> float:
    total = 0.0
    negate_left = 0
    boost = False
    for t in tokens:
        if t in NEGATORS:
            negate_left = 3
            continue
        if t in BOOSTERS and t not in LEXICON:
            boost = True
            continue
        score = LEXICON.get(t)
        if score is not None:
            v = float(score)
            if boost:
                v *= 1.5
            if negate_left > 0:
                v *= -0.75
                negate_left = 0
            total += v
        elif negate_left > 0:
            negate_left -= 1
        boost = False
    return total / math.sqrt(total * total + 15.0)


def fnv1a64(data: bytes) -> int:
    h = _FNV_OFFSET
    for b in data:
        h = ((h ^ b) * _FNV_PRIME) & _MASK64
    return h


def simhash(tokens: list[str]) -> int:
    if not tokens:
        return 0
    v = [0] * 64
    features = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    for feature in features:
        h = fnv1a64(feature.encode("utf-8"))
        for bit in range(64):
            v[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit, slot in enumerate(v):
        if slot > 0:
            out |= 1 << bit
    return out


def analyze_one(text: str) -> dict:
    tokens = tokenize(text)
    result: dict = extract_entities(text)
    result["tokens"] = len(tokens)
    result["sentiment"] = sentiment(tokens)
    result["simhash"] = simhash(tokens)
    return result


class PythonEngine:
    name = "python"

    def analyze(self, texts: list[str]) -> list[dict]:
        return [analyze_one(t) for t in texts]
