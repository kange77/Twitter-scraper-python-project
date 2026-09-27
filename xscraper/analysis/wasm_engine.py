"""Runs the Rust analytics core (xscraper_core.wasm) through wasmtime.

The module is compiled once per process and, when possible, the compiled
machine code is cached on disk so later runs skip compilation entirely.
Texts cross the boundary in batches to amortise the call overhead.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from pathlib import Path

WASM_PATH = Path(__file__).with_name("xscraper_core.wasm")
ABI_VERSION = 2
# Upper bound on bytes sent per call; keeps guest memory growth bounded.
_BATCH_BYTES = 4 * 1024 * 1024
_U64 = (1 << 64) - 1


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "xscraper"


def _load_module(engine, wasmtime, wasm_bytes: bytes):
    """Compile the module, reusing a serialized artifact when one matches."""
    version = getattr(wasmtime, "__version__", "unknown")
    key = hashlib.sha256(wasm_bytes + version.encode()).hexdigest()[:24]
    cached = _cache_dir() / f"core-{key}.cwasm"
    if cached.exists():
        try:
            return wasmtime.Module.deserialize_file(engine, str(cached))
        except Exception:
            cached.unlink(missing_ok=True)
    module = wasmtime.Module(engine, wasm_bytes)
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(module.serialize())
        tmp.replace(cached)
    except Exception:
        pass  # caching is an optimisation only
    return module


class WasmEngine:
    name = "wasm"

    def __init__(self, wasm_path: Path | str = WASM_PATH, cache: bool = True):
        import wasmtime  # optional dependency; ImportError tells the caller to fall back

        wasm_bytes = Path(wasm_path).read_bytes()
        self._engine = wasmtime.Engine()
        if cache:
            module = _load_module(self._engine, wasmtime, wasm_bytes)
        else:
            module = wasmtime.Module(self._engine, wasm_bytes)
        self._store = wasmtime.Store(self._engine)
        exports = wasmtime.Instance(self._store, module, []).exports(self._store)
        self._memory = exports["memory"]
        self._alloc = exports["alloc"]
        self._dealloc = exports["dealloc"]
        self._analyze = exports["analyze"]
        abi = exports["abi_version"](self._store)
        if abi != ABI_VERSION:
            raise RuntimeError(f"xscraper_core.wasm ABI {abi} != expected {ABI_VERSION}")
        # A wasmtime Store must not be used from two threads at once.
        self._lock = threading.Lock()

    def _call(self, payload: bytes) -> list[dict]:
        store = self._store
        with self._lock:
            in_ptr = self._alloc(store, len(payload))
            try:
                self._memory.write(store, payload, in_ptr)
                packed = self._analyze(store, in_ptr, len(payload)) & _U64
            finally:
                self._dealloc(store, in_ptr, len(payload))
            out_ptr, out_len = packed >> 32, packed & 0xFFFFFFFF
            try:
                raw = bytes(self._memory.read(store, out_ptr, out_ptr + out_len))
            finally:
                self._dealloc(store, out_ptr, out_len)
        return json.loads(raw)

    def analyze(self, texts: list[str]) -> list[dict]:
        results: list[dict] = []
        chunk: list[bytes] = []
        size = 0
        for text in texts:
            data = text.encode("utf-8", "replace")
            record = struct.pack("<I", len(data)) + data
            if chunk and size + len(record) > _BATCH_BYTES:
                results.extend(self._call(b"".join(chunk)))
                chunk, size = [], 0
            chunk.append(record)
            size += len(record)
        if chunk:
            results.extend(self._call(b"".join(chunk)))
        return results
