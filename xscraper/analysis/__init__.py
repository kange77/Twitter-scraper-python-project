"""Tweet text analytics: entities, sentiment and SimHash near-duplicate detection.

``Analyzer(engine="auto")`` uses the WebAssembly core when wasmtime is
installed and falls back to the pure-Python port otherwise. Both engines give
identical results.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable, Sequence

from .python_engine import PythonEngine

log = logging.getLogger(__name__)

ENGINES = ("auto", "wasm", "python")


def load_engine(name: str = "auto"):
    if name not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}")
    if name == "python":
        return PythonEngine()
    try:
        from .wasm_engine import WasmEngine

        return WasmEngine()
    except Exception as exc:  # ImportError (no wasmtime) or a broken module
        if name == "wasm":
            raise RuntimeError(f"WASM engine unavailable: {exc}") from exc
        log.info("WASM engine unavailable (%s); using the Python engine", exc)
        return PythonEngine()


class Analyzer:
    def __init__(self, engine: str = "auto"):
        self.engine = load_engine(engine)

    @property
    def engine_name(self) -> str:
        return self.engine.name

    def analyze(self, texts: Sequence[str]) -> list[dict]:
        # Lone surrogates can't cross into WASM as UTF-8; normalise them the
        # same way for both engines so results stay identical.
        clean = [t.encode("utf-8", "replace").decode("utf-8") for t in texts]
        return self.engine.analyze(clean)

    def annotate(self, tweets: Iterable) -> list:
        """Attach an ``analysis`` dict to each Tweet; returns the tweets."""
        tweets = list(tweets)
        for tweet, result in zip(tweets, self.analyze([t.text for t in tweets])):
            tweet.analysis = result
        return tweets


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def near_duplicate_groups(hashes: Sequence[int], max_distance: int = 3) -> list[list[int]]:
    """Group indices whose SimHashes are within ``max_distance`` bits.

    Splits each hash into ``max_distance + 1`` bands: by the pigeonhole
    principle, two hashes within the distance agree on at least one band, so
    only same-band pairs need comparing instead of all n^2 pairs.
    Returns groups of two or more indices; zero hashes (empty text) are ignored.
    """
    if not 0 <= max_distance < 32:
        raise ValueError("max_distance must be between 0 and 31")
    bands = max_distance + 1
    width = 64 // bands
    parent = list(range(len(hashes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for band in range(bands):
        shift = band * width
        mask = ((1 << (64 - shift)) - 1) if band == bands - 1 else ((1 << width) - 1)
        buckets: dict[int, list[int]] = defaultdict(list)
        for i, h in enumerate(hashes):
            if h:
                buckets[(h >> shift) & mask].append(i)
        for members in buckets.values():
            for a_pos, a in enumerate(members):
                for b in members[a_pos + 1:]:
                    if find(a) != find(b) and hamming(hashes[a], hashes[b]) <= max_distance:
                        parent[find(b)] = find(a)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(hashes)):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) > 1]


__all__ = ["Analyzer", "ENGINES", "hamming", "load_engine", "near_duplicate_groups"]
