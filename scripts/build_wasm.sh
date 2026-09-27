#!/usr/bin/env bash
# Rebuild xscraper/analysis/xscraper_core.wasm from wasm-core/.
set -euo pipefail
cd "$(dirname "$0")/.."
rustup target add wasm32-unknown-unknown >/dev/null
cargo test --manifest-path wasm-core/Cargo.toml --release
cargo build --manifest-path wasm-core/Cargo.toml --release --target wasm32-unknown-unknown
cp wasm-core/target/wasm32-unknown-unknown/release/xscraper_core.wasm xscraper/analysis/
ls -l xscraper/analysis/xscraper_core.wasm
