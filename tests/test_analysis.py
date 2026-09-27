import random
import re
from pathlib import Path

import pytest

from xscraper.analysis import Analyzer, hamming, near_duplicate_groups
from xscraper.analysis import lexicon
from xscraper.analysis.python_engine import analyze_one, extract_entities, tokenize

RUST_SRC = (Path(__file__).parents[1] / "wasm-core" / "src" / "lib.rs").read_text()

try:
    WASM = Analyzer("wasm")
except RuntimeError:
    WASM = None
needs_wasm = pytest.mark.skipif(WASM is None, reason="wasmtime not installed")


def test_lexicon_matches_rust_source():
    table = RUST_SRC.split("pub static LEXICON")[1].split("];")[0]
    assert dict((w, int(v)) for w, v in re.findall(r'\("([a-z\']+)", (-?\d+)\)', table)) == lexicon.LEXICON
    for name in ("NEGATORS", "BOOSTERS"):
        block = RUST_SRC.split(f"pub static {name}")[1].split("];")[0]
        assert frozenset(re.findall(r'"([a-z\']+)"', block)) == getattr(lexicon, name)


@pytest.mark.parametrize("text,kind,expected", [
    ("RT @NASA: hi @nasa", "mentions", ["NASA"]),
    ("mail me@example.com", "mentions", []),
    ("@toolongusername_abc", "mentions", []),
    ("#Artemis #artemis #123 #1st", "hashtags", ["Artemis", "1st"]),
    ("#café and #日本語", "hashtags", ["café", "日本語"]),
    ("&#39; is not a tag", "hashtags", []),
    ("$tsla up, $TOOLONG no, 5$ no", "cashtags", ["TSLA"]),
    ("see https://en.wikipedia.org/wiki/Foo_(bar)).", "urls", ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    ("(link: http://x.co/a)", "urls", ["http://x.co/a"]),
    ("bare https:// only", "urls", []),
])
def test_entities(text, kind, expected):
    assert extract_entities(text)[kind] == expected


def test_tokenize_skips_urls_and_trims_quotes():
    assert tokenize("Don't 'panic' https://t.co/x NOW") == ["don't", "panic", "now"]


@pytest.mark.parametrize("text,sign", [
    ("I love this, it's really great", 1),
    ("this is not good", -1),
    ("worst outage ever, total disaster", -1),
    ("the sky is blue", 0),
])
def test_sentiment_sign(text, sign):
    s = analyze_one(text)["sentiment"]
    assert -1 <= s <= 1
    assert (s > 0) - (s < 0) == sign


def test_booster_amplifies():
    assert analyze_one("very good")["sentiment"] > analyze_one("good")["sentiment"]


def test_simhash_near_duplicates():
    a = analyze_one("NASA launches the Artemis rocket to the moon today")["simhash"]
    b = analyze_one("NASA launches the Artemis rocket to the moon today!!")["simhash"]
    c = analyze_one("my cat refuses to eat breakfast again")["simhash"]
    assert a == b and hamming(a, c) > 10
    assert analyze_one("")["simhash"] == 0


def test_near_duplicate_groups():
    hashes = [0b1111, 0b1110, 0xFFFF_0000_0000_0000, 0b1111 ^ (1 << 40), 0, 0]
    groups = near_duplicate_groups(hashes, max_distance=1)
    assert sorted(map(sorted, groups)) == [[0, 1, 3]]
    with pytest.raises(ValueError):
        near_duplicate_groups(hashes, max_distance=64)


def test_near_duplicate_groups_matches_brute_force():
    rng = random.Random(1)
    base = [rng.getrandbits(64) for _ in range(40)]
    hashes = [b ^ (1 << rng.randrange(64)) if rng.random() < 0.5 else b for b in base for _ in range(3)]
    hashes = [h or 1 for h in hashes]
    groups = near_duplicate_groups(hashes, max_distance=3)
    grouped = {i: frozenset(g) for g in groups for i in g}
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if hamming(hashes[i], hashes[j]) <= 3:
                assert grouped[i] == grouped[j]


def _corpus(n, seed=7):
    rng = random.Random(seed)
    alphabet = "abcdeé日😀 #@$_'\"().,!?:/\\\n\t　 hHtps0123-&<>ÅΣ"
    words = ["http://x.co/a", "https://t.co/x)", "#tag", "@who", "$AAPL", "love", "not",
             "very", "bad", "great", "the", "moon"]
    out = ["", "a\x00b\x1fc\\\"d", "ΣΑΣ ΟΔΟΣ İstanbul", "lone \ud800 surrogate"]
    for _ in range(n):
        out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))))
        out.append(" ".join(rng.choice(words) for _ in range(rng.randint(0, 15))))
    return out


@needs_wasm
def test_wasm_matches_python():
    corpus = _corpus(3000)
    assert WASM.engine_name == "wasm"
    assert WASM.analyze(corpus) == Analyzer("python").analyze(corpus)


@needs_wasm
def test_wasm_large_batches_are_split():
    from xscraper.analysis import wasm_engine

    engine = wasm_engine.WasmEngine(cache=False)
    old = wasm_engine._BATCH_BYTES
    wasm_engine._BATCH_BYTES = 64
    try:
        corpus = _corpus(50)
        assert engine.analyze(corpus) == Analyzer("python").analyze(corpus)
    finally:
        wasm_engine._BATCH_BYTES = old


def test_auto_falls_back_without_wasmtime(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "wasmtime":
            raise ImportError("no wasmtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert Analyzer("auto").engine_name == "python"
    with pytest.raises(RuntimeError):
        Analyzer("wasm")


@pytest.mark.parametrize("engine", ["python", pytest.param("wasm", marks=needs_wasm)])
def test_hashtags_keep_combining_marks(engine):
    [r] = Analyzer(engine).analyze(["#नमस्ते #தமிழ் #ภาษาไทย #ﾃｽﾄ"])
    assert r["hashtags"] == ["नमस्ते", "தமிழ்", "ภาษาไทย", "ﾃｽﾄ"]


def test_unicode_table_is_sorted_and_disjoint():
    from xscraper.analysis._unicode_tables import WORD_ENDS, WORD_STARTS

    assert len(WORD_STARTS) == len(WORD_ENDS)
    for i, (a, b) in enumerate(zip(WORD_STARTS, WORD_ENDS)):
        assert 0x80 <= a <= b
        if i:
            assert a > WORD_ENDS[i - 1] + 1  # adjacent ranges would have been merged


def test_rust_and_python_tables_match():
    from xscraper.analysis._unicode_tables import WORD_ENDS, WORD_STARTS

    rust = (Path(__file__).parents[1] / "wasm-core" / "src" / "unicode_tables.rs").read_text()
    pairs = [(int(a, 16), int(b, 16)) for a, b in re.findall(r"\(0x([0-9A-F]+), 0x([0-9A-F]+)\)", rust)]
    assert pairs == list(zip(WORD_STARTS, WORD_ENDS))


@needs_wasm
def test_wasm_matches_python_across_unicode():
    # Both sides of every letter/number/mark range boundary, one text each.
    # Code points this Python doesn't know yet are skipped: Rust's newer
    # Unicode data can lowercase them, which is the one known difference.
    import unicodedata

    from xscraper.analysis._unicode_tables import WORD_ENDS, WORD_STARTS

    points = sorted({p for a, b in zip(WORD_STARTS, WORD_ENDS) for p in (a - 1, a, b, b + 1)
                     if 0x80 <= p <= 0x10FFFF and not 0xD800 <= p <= 0xDFFF
                     and unicodedata.category(chr(p)) != "Cn"})
    texts = [f"#a{chr(p)}b {chr(p)}love x{chr(p)}" for p in points]
    assert WASM.analyze(texts) == Analyzer("python").analyze(texts)
